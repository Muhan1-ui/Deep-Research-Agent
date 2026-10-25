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
from openai import NOT_GIVEN

from src.agent.config import Settings

# 传输层异常：退避后重试值得；漏掉一种 = 偶发崩溃，多包一种通常无害
RETRYABLE_EXCEPTIONS = (
    openai.RateLimitError,        # 429
    openai.APIConnectionError,    # 断连/DNS
    openai.APITimeoutError,
    openai.InternalServerError,   # 5xx
)


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
        self._client = client or openai.AsyncOpenAI(
            api_key=settings.llm_api_key, base_url=settings.llm_base_url
        )
        self._sleep = sleep  # 注入以便测试瞬时回放退避序列

    async def complete(
        self,
        messages: list[dict],
        tools: list[dict] | None = None,
        max_retries: int = 4,
    ) -> LLMResult:
        resp = None
        for attempt in range(max_retries + 1):
            try:
                resp = await self._client.chat.completions.create(
                    model=self._s.llm_model,
                    messages=messages,
                    tools=tools if tools else NOT_GIVEN,
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
