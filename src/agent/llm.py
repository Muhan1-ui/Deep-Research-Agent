"""LLM 客户端封装：厂商无关（OpenAI 兼容 base_url）+ 分类重试。

重试策略只在传输层（限流/超时/5xx/断连）生效——这些错误重试有意义；
鉴权/参数错误立刻失败，重试只是浪费钱。这个分类决策本身是面试考点。
"""

from __future__ import annotations

import asyncio
import random
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field

import openai
import httpx
from openai import NOT_GIVEN

from src.agent.config import Settings

# 传输层异常：退避后重试值得；漏掉一种 = 偶发崩溃，多包一种通常无害
RETRYABLE_EXCEPTIONS = (
    openai.RateLimitError,        # 429
    openai.APIConnectionError,    # 断连/DNS
    openai.APITimeoutError,
    openai.InternalServerError,   # 5xx
)


def thinking_body(mode: str | None) -> dict | None:
    """思考开关 → extra_body 映射（当前语法 = DeepSeek，已用 /models 接口核对型号）。

    ⚠ 不要假设厂商同构：GLM-5.3 没有 disabled 档（只有 low/high/max 效率档），
    W5 judge 接 GLM 时这里必须改成按厂商的映射表。None/"auto" = 不干预。
    注意只能返回 None 或 dict——extra_json 路径只判 None，NOT_GIVEN 会 TypeError。
    """
    if mode in (None, "", "auto"):
        return None
    return {"thinking": {"type": mode}}


class RetryableLLMError(Exception):
    """重试耗尽后仍然失败。"""


class FatalLLMError(Exception):
    """鉴权/请求参数错误：应修配置或代码，重试无意义。"""


@dataclass
class ToolCallRequest:
    """模型发起的一次工具调用。

    arguments 保留原始字符串，JSON 解析放在 ToolRegistry 里做——
    解析失败要变成回喂给模型的错误信息，而不是让进程崩溃。
    """

    id: str
    name: str
    arguments_raw: str


@dataclass
class LLMResult:
    content: str | None = None
    # DeepSeek/GLM 思考模式的思考过程（非流式挂在 message.reasoning_content）。
    # 思考 + tools 时后续请求必须原样回传，缺失直接 400——loop 负责带回。
    reasoning_content: str | None = None
    tool_calls: list[ToolCallRequest] = field(default_factory=list)
    usage_in: int = 0
    usage_out: int = 0
    model: str = ""


class LLMClient:
    """薄封装：create 调用 + 异常分类 + 指数退避。不掺任何业务语义。"""

    def __init__(
        self,
        settings: Settings,
        client: openai.AsyncOpenAI | None = None,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ):
        self._s = settings
        self._owns_client = client is None  # 注入的 client 归调用方管（测试）
        self._client = client or openai.AsyncOpenAI(
            api_key=settings.llm_api_key,
            base_url=settings.llm_base_url,
            # 重试所有权在分类退避层：关掉 SDK 内建重试（默认 2），
            # 否则最坏 (1+4)×(1+2)=15 次隐藏 HTTP 尝试，W5 批跑会失控
            max_retries=0,
            # SDK 默认 timeout=600s：单个僵死连接能挂住整个批跑；
            # read 给足长思考生成的余量，connect 短平快失败进退避
            timeout=httpx.Timeout(settings.llm_timeout_s, connect=10.0),
        )
        self._sleep = sleep  # 注入以便测试瞬时回放退避序列

    async def aclose(self) -> None:
        """关闭自建的底层连接池；注入的 client 不归我们管。批跑防 fd 耗尽。
        注意 openai SDK 的方法是 close()（不是 httpx 风格的 aclose()）。"""
        if self._owns_client:
            await self._client.close()

    async def complete(
        self,
        messages: list[dict],
        tools: list[dict] | None = None,
        max_retries: int = 4,
        temperature: float | None = None,
        extra_body: dict | None = None,
    ) -> LLMResult:
        """temperature 透传：注意思考模式下 DeepSeek 静默忽略该参数（不报错但不生效），
        需要 temp=0 的调用（如评测 judge）必须同时关思考。"""
        resp = None
        for attempt in range(max_retries + 1):
            try:
                resp = await self._client.chat.completions.create(
                    model=self._s.llm_model,
                    messages=messages,
                    tools=tools if tools else NOT_GIVEN,
                    temperature=temperature if temperature is not None else NOT_GIVEN,
                    extra_body=extra_body if extra_body is not None
                    else thinking_body(self._s.llm_thinking),
                )
                break
            except RETRYABLE_EXCEPTIONS as e:
                if attempt == max_retries:
                    raise RetryableLLMError(f"重试 {max_retries} 次后仍失败: {e}") from e
                await self._sleep(min(2**attempt + random.random(), 30.0))
            except openai.AuthenticationError as e:
                raise FatalLLMError(f"API key 无效: {e}") from e
            except openai.BadRequestError as e:
                raise FatalLLMError(f"请求被拒（检查模型名/参数）: {e}") from e

        msg = resp.choices[0].message
        usage = resp.usage
        return LLMResult(
            content=msg.content,
            reasoning_content=getattr(msg, "reasoning_content", None),
            tool_calls=[
                ToolCallRequest(
                    id=c.id, name=c.function.name, arguments_raw=c.function.arguments or "{}"
                )
                for c in (msg.tool_calls or [])
            ],
            usage_in=getattr(usage, "prompt_tokens", 0) or 0,
            usage_out=getattr(usage, "completion_tokens", 0) or 0,
            model=resp.model or self._s.llm_model,
        )
