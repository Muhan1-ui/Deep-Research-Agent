"""AgentLoop 行为测试：脚本化 FakeLLM 回放，不联网不调 API。

async 测试体用 asyncio.run 包装（不引入 pytest-asyncio 依赖）。
"""

import asyncio

from pydantic import BaseModel

from src.agent.llm import LLMResult, ToolCallRequest
from src.agent.runtime.loop import (
    STOP_ERROR_STREAK,
    STOP_FINAL,
    STOP_MAX_TURNS,
    AgentLoop,
)
from src.agent.tools.base import Tool, ToolRegistry


class FakeLLM:
    """按脚本回放的假 LLM：序列里每个元素是一次响应或一个异常。"""

    def __init__(self, script):
        self.script = list(script)
        self.calls = []

    async def complete(self, messages, tools=None):
        self.calls.append({"messages": [dict(m) for m in messages], "tools": tools})
        item = self.script.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


def tool_call(cid: str, name: str = "echo", args: str = "{}") -> ToolCallRequest:
    return ToolCallRequest(id=cid, name=name, arguments_raw=args)


def final(text: str) -> LLMResult:
    return LLMResult(content=text)


def make_registry(*tools: Tool) -> ToolRegistry:
    r = ToolRegistry()
    for t in tools:
        r.register(t)
    return r


class EchoArgs(BaseModel):
    text: str = "ok"


def echo_tool() -> Tool:
    async def handler(args: EchoArgs) -> str:
        return f"echo:{args.text}"

    return Tool(name="echo", description="回声", args_model=EchoArgs, handler=handler)


def broken_tool() -> Tool:
    async def handler(args: EchoArgs) -> str:
        raise ValueError("模拟的工具内部故障")

    return Tool(name="broken", description="必炸", args_model=EchoArgs, handler=handler)


def run(coro):
    return asyncio.run(coro)


class TestHappyPath:
    def test_tool_then_answer(self):
        llm = FakeLLM([
            LLMResult(content=None, tool_calls=[tool_call("t1", "echo", '{"text": "hi"}')]),
            final("答案是 hi"),
        ])
        loop = AgentLoop(llm, make_registry(echo_tool()))
        result = run(loop.run("sys", "问 echo hi"))
        assert result.answer == "答案是 hi"
        assert result.stop_reason == STOP_FINAL
        assert result.turns == 2
        assert result.tool_calls_total == 1
        assert result.tool_errors_total == 0
        # 工具结果确实作为 role=tool 消息进入了下一轮上下文
        tool_msgs = [m for m in result.messages if m["role"] == "tool"]
        assert tool_msgs and tool_msgs[0]["content"] == "echo:hi"
        assert tool_msgs[0]["tool_call_id"] == "t1"


class TestParallelToolCalls:
    def test_same_turn_calls_run_concurrently(self):
        """两个 handler 互等对方启动：串行执行会超时，只有并行才过得去。"""
        e1, e2 = asyncio.Event(), asyncio.Event()

        class _A(BaseModel):
            x: int = 0

        async def h1(args):
            e1.set()
            await asyncio.wait_for(e2.wait(), 1.0)
            return "one"

        async def h2(args):
            e2.set()
            await asyncio.wait_for(e1.wait(), 1.0)
            return "two"

        r = ToolRegistry()
        r.register(Tool("a", "d", _A, h1))
        r.register(Tool("b", "d", _A, h2))

        llm = FakeLLM([
            LLMResult(content=None, tool_calls=[tool_call("t1", "a"), tool_call("t2", "b")]),
            final("both done"),
        ])
        result = run(AgentLoop(llm, r, max_turns=4).run("s", "u"))
        contents = [m["content"] for m in result.messages if m["role"] == "tool"]
        assert sorted(contents) == ["one", "two"]
        assert result.tool_errors_total == 0


class TestErrorFeedback:
    def test_tool_exception_becomes_feedback_not_crash(self):
        llm = FakeLLM([
            LLMResult(content=None, tool_calls=[tool_call("t1", "broken")]),
            final("工具坏了，我换个说法回答"),
        ])
        result = run(AgentLoop(llm, make_registry(broken_tool())).run("s", "u"))
        assert result.answer is not None
        assert result.tool_errors_total == 1
        # 第二轮 LLM 调用看到了回喂的错误信息
        seen = llm.calls[1]["messages"]
        assert any("[TOOL_ERROR]" in m.get("content", "") for m in seen if m["role"] == "tool")

    def test_invalid_json_args_fed_back(self):
        llm = FakeLLM([
            LLMResult(content=None, tool_calls=[tool_call("t1", "echo", '{"text": ')]),  # 坏 JSON
            final("参数格式错了，我重试"),
        ])
        result = run(AgentLoop(llm, make_registry(echo_tool())).run("s", "u"))
        assert result.tool_errors_total == 1
        err_msg = [m for m in result.messages if m["role"] == "tool"][0]["content"]
        assert "TOOL_ERROR" in err_msg

    def test_unknown_tool_fed_back(self):
        llm = FakeLLM([
            LLMResult(content=None, tool_calls=[tool_call("t1", "ghost_tool")]),
            final("没有这个工具"),
        ])
        result = run(AgentLoop(llm, make_registry(echo_tool())).run("s", "u"))
        assert result.tool_errors_total == 1


class TestGuardrails:
    def test_error_streak_aborts(self):
        """连续 3 轮整轮全错 → 停止，防烧钱空转。"""
        script = [
            LLMResult(content=None, tool_calls=[tool_call(f"t{i}", "broken")]) for i in range(3)
        ]
        result = run(AgentLoop(FakeLLM(script), make_registry(broken_tool())).run("s", "u"))
        assert result.stop_reason == STOP_ERROR_STREAK
        assert result.turns == 3
        assert result.answer is None

    def test_partial_errors_do_not_count_streak(self):
        """同轮一成一败 → streak 清零：单工具失败常是模型笔误，应允许自纠。"""
        e1, e2 = asyncio.Event(), asyncio.Event()

        class _A(BaseModel):
            x: int = 0

        async def ok_handler(args):
            return "fine"

        async def bad_handler(args):
            raise RuntimeError("bad")

        r = ToolRegistry()
        r.register(Tool("ok", "d", _A, ok_handler))
        r.register(Tool("bad", "d", _A, bad_handler))

        script = [
            LLMResult(content=None, tool_calls=[tool_call("t1", "ok"), tool_call("t2", "bad")]),
            LLMResult(content=None, tool_calls=[tool_call("t3", "ok"), tool_call("t4", "bad")]),
            LLMResult(content=None, tool_calls=[tool_call("t5", "ok"), tool_call("t6", "bad")]),
            final("三轮混错后仍给出回答"),
        ]
        result = run(AgentLoop(FakeLLM(script), r, max_turns=6, max_error_streak=3).run("s", "u"))
        assert result.stop_reason == STOP_FINAL
        assert result.tool_errors_total == 3

    def test_max_turns_bounds_infinite_loops(self):
        script = [
            LLMResult(content=None, tool_calls=[tool_call(f"t{i}", "echo")]) for i in range(3)
        ]
        result = run(AgentLoop(FakeLLM(script), make_registry(echo_tool()), max_turns=3).run("s", "u"))
        assert result.stop_reason == STOP_MAX_TURNS
        assert result.answer is None
        assert result.turns == 3
