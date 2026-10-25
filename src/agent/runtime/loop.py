"""原生 tool-calling loop —— 整个系统的心脏。

流程：messages → LLM → 有 tool_calls？→ 并行执行 → 结果（含错误）回喂 → 再调 LLM，
直到模型给出不带工具调用的最终回答，或触发护栏。

三条设计原则（面试考点，代码刻意为它们服务）：
1. 错误是数据不是异常：工具失败/参数错误都变成字符串回喂，模型有机会自纠；
2. 护栏先行：max_turns 防无限循环，"连续整轮全错"防烧钱空转；
3. messages 是唯一事实：完整保留（含工具结果），W3 落库与 trace 直接复用。
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field

from src.agent.llm import LLMClient, LLMResult
from src.agent.tools.base import ToolRegistry

MAX_TURNS_DEFAULT = 8
MAX_ERROR_STREAK_DEFAULT = 3

STOP_FINAL = "final_answer"
STOP_MAX_TURNS = "max_turns"
STOP_ERROR_STREAK = "tool_failure_streak"


@dataclass
class LoopResult:
    answer: str | None
    stop_reason: str  # STOP_* 之一
    turns: int = 0
    tool_calls_total: int = 0
    tool_errors_total: int = 0
    usage_in: int = 0
    usage_out: int = 0
    messages: list[dict] = field(default_factory=list)  # 完整对话，落库/trace 原料


class AgentLoop:
    def __init__(
        self,
        llm: LLMClient,
        registry: ToolRegistry,
        max_turns: int = MAX_TURNS_DEFAULT,
        max_error_streak: int = MAX_ERROR_STREAK_DEFAULT,
    ):
        self._llm = llm
        self._registry = registry
        self._max_turns = max_turns
        self._max_error_streak = max_error_streak

    async def run(self, system_prompt: str, user_message: str) -> LoopResult:
        messages: list[dict] = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_message},
        ]
        result = LoopResult(answer=None, stop_reason=STOP_MAX_TURNS, messages=messages)
        streak = 0

        for turn in range(1, self._max_turns + 1):
            result.turns = turn
            llm_out: LLMResult = await self._llm.complete(
                messages, self._registry.openai_specs() or None
            )
            result.usage_in += llm_out.usage_in
            result.usage_out += llm_out.usage_out

            messages.append(self._assistant_message(llm_out))

            if not llm_out.tool_calls:
                result.answer = llm_out.content
                result.stop_reason = STOP_FINAL
                return result

            # 同一轮的多个工具调用并行执行（asyncio.gather 保持与调用顺序对齐）
            outcomes = await asyncio.gather(
                *(self._registry.dispatch(c.name, c.arguments_raw) for c in llm_out.tool_calls)
            )
            for call, outcome in zip(llm_out.tool_calls, outcomes):
                messages.append(
                    {"role": "tool", "tool_call_id": call.id, "content": outcome.content}
                )

            result.tool_calls_total += len(outcomes)
            errors = sum(o.is_error for o in outcomes)
            result.tool_errors_total += errors

            # 只有"整轮全错"才累计：单工具失败可能是模型参数笔误，下一轮通常能自纠
            streak = streak + 1 if errors == len(outcomes) else 0
            if streak >= self._max_error_streak:
                result.stop_reason = STOP_ERROR_STREAK
                return result

        return result

    @staticmethod
    def _assistant_message(llm_out: LLMResult) -> dict:
        """把模型回复原样写回 messages（含 tool_calls 原始结构），保证下一轮
        请求的上下文与厂商 API 约定完全一致。"""
        msg: dict = {"role": "assistant", "content": llm_out.content or ""}
        # DeepSeek 思考模式 + tools：后续请求必须完整回传 reasoning_content，缺失 400。
        # is not None 而非真值判断：厂商返回空串时字段仍在协议意义上"存在"，丢弃可能 400
        if llm_out.reasoning_content is not None:
            msg["reasoning_content"] = llm_out.reasoning_content
        if llm_out.tool_calls:
            msg["tool_calls"] = [
                {
                    "id": c.id,
                    "type": "function",
                    "function": {"name": c.name, "arguments": c.arguments_raw},
                }
                for c in llm_out.tool_calls
            ]
        return msg
