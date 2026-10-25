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
