"""LLMClient 测试：异常分类 + 指数退避。用鸭子类型 FakeClient，不联网。"""

import asyncio
from types import SimpleNamespace

import httpx
import openai
import pytest

from src.agent.config import Settings
from src.agent.llm import FatalLLMError, LLMClient, RetryableLLMError


def make_err(exc_cls, status: int) -> Exception:
    """openai SDK 异常需要带 response 才能构造。"""
    req = httpx.Request("POST", "https://api.test/v1/chat")
    resp = httpx.Response(status, request=req)
    return exc_cls("test error", response=resp, body=None)


class FakeCompletions:
    def __init__(self, script):
        self.script = list(script)
        self.calls = 0

    async def create(self, **kwargs):
        self.calls += 1
        item = self.script.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


def fake_client(script) -> SimpleNamespace:
    return SimpleNamespace(chat=SimpleNamespace(completions=FakeCompletions(script)))


def ok_response() -> SimpleNamespace:
    """鸭子类型的 chat completion 响应。"""
    return SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content="好的", tool_calls=None))],
        usage=SimpleNamespace(prompt_tokens=11, completion_tokens=7),
        model="fake-model",
    )


def make_llm(script, sleep=None) -> LLMClient:
    delays = []

    async def rec_sleep(seconds: float) -> None:
        delays.append(seconds)

    client = LLMClient(
        Settings(llm_api_key="k", llm_model="fake"),
        client=fake_client(script),
        sleep=sleep or rec_sleep,
    )
    return client, delays


class TestRetryClassification:
    def test_rate_limit_retries_with_backoff(self):
        client, delays = make_llm([
            make_err(openai.RateLimitError, 429),
            make_err(openai.RateLimitError, 429),
            ok_response(),
        ])
        result = asyncio.run(client.complete([{"role": "user", "content": "hi"}]))
        assert result.content == "好的"
        assert result.usage_in == 11 and result.usage_out == 7
        assert len(delays) == 2
        assert delays[1] > delays[0]  # 指数退避：第二次等待更久

    def test_auth_error_is_fatal_immediately(self):
        client, delays = make_llm([make_err(openai.AuthenticationError, 401)])
        with pytest.raises(FatalLLMError):
            asyncio.run(client.complete([{"role": "user", "content": "hi"}]))
        assert delays == []  # 不重试：重试解决不了鉴权问题

    def test_retry_exhausted_raises(self):
        client, _ = make_llm([make_err(openai.InternalServerError, 500)] * 5)
        with pytest.raises(RetryableLLMError):
            asyncio.run(client.complete([{"role": "user", "content": "hi"}], max_retries=3))

    def test_tool_calls_parsed(self):
        msg = SimpleNamespace(
            content=None,
            tool_calls=[SimpleNamespace(id="t9", function=SimpleNamespace(
                name="web_search", arguments='{"query": "mcp"}'))],
        )
        resp = SimpleNamespace(
            choices=[SimpleNamespace(message=msg)],
            usage=SimpleNamespace(prompt_tokens=5, completion_tokens=3),
            model="fake-model",
        )
        client, _ = make_llm([resp])
        result = asyncio.run(client.complete([{"role": "user", "content": "hi"}]))
        assert result.tool_calls[0].name == "web_search"
        assert result.tool_calls[0].arguments_raw == '{"query": "mcp"}'
        assert result.tool_calls[0].id == "t9"


class TestThinkingModeParams:
    """思考模式相关的透传行为：deepseek-v4-flash 默认开思考，
    tool-call 多轮缺 reasoning_content 回传会 400，思考模式下 temperature 静默失效。"""

    @staticmethod
    def capturing_llm(settings_kwargs=None):
        captured = []

        class C:
            async def create(self, **kw):
                captured.append(kw)
                return ok_response()

        from openai import NOT_GIVEN as NG  # 与实现同源的哨兵比较

        client = LLMClient(
            Settings(llm_api_key="k", llm_model="fake", **(settings_kwargs or {})),
            client=SimpleNamespace(chat=SimpleNamespace(completions=C())),
        )
        return client, captured, NG

    def test_thinking_disabled_sent_by_default(self):
        client, captured, _ = self.capturing_llm()
        asyncio.run(client.complete([{"role": "user", "content": "hi"}]))
        assert captured[0]["extra_body"] == {"thinking": {"type": "disabled"}}

    def test_thinking_auto_omits_extra_body(self):
        client, captured, _ = self.capturing_llm({"llm_thinking": "auto"})
        asyncio.run(client.complete([{"role": "user", "content": "hi"}]))
        # None 的语义 = SDK 省略该参数（extra_json 只判 None；NOT_GIVEN 会炸，见线体测试）
        assert captured[0]["extra_body"] is None

    def test_temperature_passthrough_and_default(self):
        client, captured, NG = self.capturing_llm()
        asyncio.run(client.complete([{"role": "user", "content": "hi"}], temperature=0.0))
        assert captured[0]["temperature"] == 0.0
        asyncio.run(client.complete([{"role": "user", "content": "hi"}]))
        assert captured[1]["temperature"] is NG

    def test_extra_body_override_beats_settings(self):
        client, captured, _ = self.capturing_llm()
        asyncio.run(
            client.complete([{"role": "user", "content": "hi"}], extra_body={"foo": 1})
        )
        assert captured[0]["extra_body"] == {"foo": 1}

    def test_reasoning_content_captured(self):
        msg = SimpleNamespace(content="答", tool_calls=None, reasoning_content="思考中")
        resp = SimpleNamespace(
            choices=[SimpleNamespace(message=msg)],
            usage=SimpleNamespace(prompt_tokens=1, completion_tokens=2),
            model="fake",
        )
        client, _ = make_llm([resp])
        result = asyncio.run(client.complete([{"role": "user", "content": "hi"}]))
        assert result.reasoning_content == "思考中"


def sdk_wire_client(bodies: list) -> "openai.AsyncOpenAI":
    """真实 openai SDK + MockTransport：截获真正发出的 HTTP 请求体。

    假 completions 对象只看得到 kwargs，进不了 SDK 的 body 组装——
    auto bug（NOT_GIVEN 走 extra_json 不经 strip_not_given → TypeError）
    正是被假对象盖住的。这层测试专门钉死"参数拼装 × 真实 SDK"接触面。
    """
    import json as jsonlib

    def handler(request: httpx.Request) -> httpx.Response:
        bodies.append(jsonlib.loads(request.content.decode("utf-8")))
        return httpx.Response(200, json={
            "id": "x", "object": "chat.completion", "created": 0, "model": "fake",
            "choices": [{"index": 0, "finish_reason": "stop",
                         "message": {"role": "assistant", "content": "ok"}}],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
        })

    return openai.AsyncOpenAI(
        api_key="k", base_url="https://api.test/v1", max_retries=0,
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )


class TestWireBodyAssembly:
    """经真实 SDK 组装出的请求体（线上字节），不是我们以为会发出的参数。"""

    def _run(self, settings_kwargs=None, **complete_kw):
        bodies: list = []
        client = LLMClient(
            Settings(llm_api_key="k", llm_model="fake", **(settings_kwargs or {})),
            client=sdk_wire_client(bodies),
        )
        result = asyncio.run(client.complete([{"role": "user", "content": "hi"}], **complete_kw))
        return bodies[0], result

    def test_disabled_adds_thinking_off(self):
        body, result = self._run()
        assert body["thinking"] == {"type": "disabled"}
        assert result.content == "ok"

    def test_auto_omits_thinking_entirely(self):
        # 修复前：thinking_body 返回 None → `None or NOT_GIVEN` → extra_body=NOT_GIVEN
        # → SDK 的 extra_json 只判 None 不判 NOT_GIVEN → TypeError（真实调用必炸）
        body, _ = self._run({"llm_thinking": "auto"})
        assert "thinking" not in body

    def test_enabled(self):
        body, _ = self._run({"llm_thinking": "enabled"})
        assert body["thinking"] == {"type": "enabled"}

    def test_extra_body_override_and_temperature_reach_the_wire(self):
        body, _ = self._run(temperature=0.0, extra_body={"foo": 1})
        assert body["foo"] == 1
        assert "thinking" not in body
        assert body["temperature"] == 0.0

    def test_temperature_absent_by_default_on_the_wire(self):
        # temperature 是类型化参数：NOT_GIVEN 会被 strip_not_given 正确剥掉
        # （与 extra_body 的不对称正是 auto bug 的根源）
        body, _ = self._run()
        assert "temperature" not in body
