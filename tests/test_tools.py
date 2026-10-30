"""工具层测试：注册表调度 + Tavily 响应格式化（MockTransport，不联网）。"""

import asyncio

import httpx
import pytest
from pydantic import BaseModel

from src.agent.tools.base import Tool, ToolRegistry
from src.agent.tools.search import WebSearchArgs, build_search_tool


class CalcArgs(BaseModel):
    a: int
    b: int = 1


def calc_tool() -> Tool:
    async def handler(args: CalcArgs) -> str:
        return f"sum={args.a + args.b}"

    return Tool(name="calc", description="加法", args_model=CalcArgs, handler=handler)


def run(coro):
    return asyncio.run(coro)


class TestRegistry:
    def test_duplicate_name_rejected(self):
        r = ToolRegistry()
        r.register(calc_tool())
        with pytest.raises(ValueError, match="重名"):
            r.register(calc_tool())

    def test_openai_specs_shape(self):
        r = ToolRegistry()
        r.register(calc_tool())
        spec = r.openai_specs()[0]
        assert spec["type"] == "function"
        assert spec["function"]["name"] == "calc"
        props = spec["function"]["parameters"]["properties"]
        assert set(props) == {"a", "b"}

    def test_dispatch_happy(self):
        r = ToolRegistry()
        r.register(calc_tool())
        out = run(r.dispatch("calc", '{"a": 2, "b": 3}'))
        assert out.content == "sum=5" and not out.is_error

    def test_dispatch_unknown_tool(self):
        r = ToolRegistry()
        out = run(r.dispatch("nope", "{}"))
        assert out.is_error and "未知工具" in out.content

    def test_dispatch_bad_json(self):
        r = ToolRegistry()
        r.register(calc_tool())
        out = run(r.dispatch("calc", '{"a": '))
        assert out.is_error

    def test_dispatch_schema_violation(self):
        r = ToolRegistry()
        r.register(calc_tool())
        out = run(r.dispatch("calc", '{"b": 2}'))  # 缺必填的 a
        assert out.is_error and "参数无效" in out.content

    def test_dispatch_handler_exception(self):
        async def boom(args: CalcArgs) -> str:
            raise RuntimeError("网络抖动")

        r = ToolRegistry()
        r.register(Tool("boom", "d", CalcArgs, boom))
        out = run(r.dispatch("boom", '{"a": 1}'))
        assert out.is_error and "RuntimeError" in out.content


class TestTavilySearch:
    @staticmethod
    def mocked_client(payload: dict, status: int = 200) -> httpx.AsyncClient:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(status, json=payload)

        return httpx.AsyncClient(transport=httpx.MockTransport(handler))

    def test_formats_results(self):
        payload = {
            "results": [
                {"title": "Rust 1.85", "url": "https://blog.rust-lang.org/1.85", "content": "2024 edition stable"},
                {"title": "Release notes", "url": "https://github.com/...", "content": "async closures"},
            ]
        }
        tool = build_search_tool("k", client=self.mocked_client(payload))
        out = run(tool.handler(WebSearchArgs(query="rust 1.85", max_results=2)))
        assert "Rust 1.85" in out and "https://blog.rust-lang.org/1.85" in out
        assert "async closures" in out

    def test_empty_results_is_message_not_error(self):
        tool = build_search_tool("k", client=self.mocked_client({"results": []}))
        out = run(tool.handler(WebSearchArgs(query="zzz")))
        assert "无搜索结果" in out

    def test_http_error_propagates_as_tool_error_via_dispatch(self):
        tool = build_search_tool("k", client=self.mocked_client({}, status=500))

        async def body():
            r = ToolRegistry()
            r.register(tool)
            return await r.dispatch("web_search", '{"query": "x"}')

        out = run(body())
        assert out.is_error and "TOOL_ERROR" in out.content

    def test_error_body_snippet_in_tool_message(self):
        """第十六轮：Tavily 配额耗尽的判别信息在响应 body 里（HTTP 432 +
        detail.error）——raise_for_status 只有状态行，必须带 body 片段。"""

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                432, json={"detail": {"error": "This request exceeds your plan's set usage limit."}})

        registry = ToolRegistry()
        registry.register(build_search_tool("k", client=httpx.AsyncClient(
            transport=httpx.MockTransport(handler))))
        out = run(registry.dispatch("web_search", '{"query": "x"}'))
        assert out.is_error
        assert "432" in out.content
        assert "exceeds your plan" in out.content  # 厂商原文进错误消息
