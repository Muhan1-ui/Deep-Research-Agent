"""Researcher 测试：完整离线链路 loop→工具→claims 解析→修复轮。

关键手法：SmartFakeLLM 的可调用脚本项**读取工具返回的真实内容**动态生成
下一轮（从 fetch_page 结果里抄 snippet_id / 引文）——和真模型行为同构。
"""

import asyncio
import json
import re

from pydantic import BaseModel

from src.agent.agents.planner import SubQuery
from src.agent.agents.researcher import parse_raw_claims, research_sub_query
from src.agent.llm import LLMResult, ToolCallRequest
from src.agent.runtime.loop import AgentLoop  # noqa: F401  （确认导入链可用）
from src.agent.schemas import RunStatus
from src.agent.tools.base import Tool, ToolRegistry
from src.agent.tools.fetch import PageFetcher
from src.agent.tools.pages import FetchPageArgs
from tests.conftest import SmartFakeLLM
from tests.test_fetch import HTML, URL, mocked_client


def run(coro):
    return asyncio.run(coro)


def tc(cid: str, name: str, args: str) -> ToolCallRequest:
    return ToolCallRequest(id=cid, name=name, arguments_raw=args)


def claims_from_last_tool_message(messages):
    """模拟真实模型：从最后一条工具返回里抄 snippet_id 和逐字引文。"""
    tool_msg = [m for m in messages if m["role"] == "tool"][-1]["content"]
    sid = re.search(r"id=([^\]]+)\]", tool_msg).group(1)
    passage = tool_msg.split("]", 1)[1].strip()
    quote = passage[:40]
    return LLMResult(content=json.dumps(
        [{"text": "MCP 是开放标准", "quote": quote, "snippet_id": sid}],
        ensure_ascii=False), usage_in=10, usage_out=1)


def build_registry(tmp_path, fetcher: PageFetcher) -> ToolRegistry:
    class SearchArgs(BaseModel):
        query: str

    async def fake_search(args: SearchArgs) -> str:
        return f"搜索结果 for {args.query}: 1. MCP 指南 {URL}"

    registry = ToolRegistry()
    registry.register(Tool("web_search", "搜索", SearchArgs, fake_search))
    registry.register(Tool(  # 与 resolver 共享同一个 fetcher（生产同款接线）
        "fetch_page", "读网页", FetchPageArgs, fetcher))
    return registry


class TestParseRawClaims:
    def test_empty_answer(self):
        raws, failures = parse_raw_claims(None)
        assert not raws and failures[0].reason == "invalid_shape"

    def test_bad_json(self):
        raws, failures = parse_raw_claims("我认为……")
        assert failures[0].reason == "invalid_shape"

    def test_item_missing_field_keeps_index(self):
        raws, failures = parse_raw_claims(
            '[{"text": "t", "quote": "q", "snippet_id": "s"}, {"text": "缺字段"}]')
        assert len(raws) == 1 and failures[0].index == 1


class TestResearchSubQuery:
    def test_full_offline_chain(self, tmp_path):
        """search → fetch → 最终论断 JSON → Claim（句级锚定、代码统计用量）。"""
        fetcher = PageFetcher(tmp_path, client=mocked_client())
        registry = build_registry(tmp_path, fetcher)
        llm = SmartFakeLLM([
            LLMResult(content=None, tool_calls=[tc("t1", "web_search", '{"query": "MCP 架构"}')]),
            LLMResult(content=None, tool_calls=[
                tc("t2", "fetch_page", json.dumps({"url": URL, "query": "MCP"}))]),
            claims_from_last_tool_message,
        ])
        sub = SubQuery(sub_query_id="SQ1", question="MCP 是什么", search_hints=[])
        finding = run(research_sub_query(
            llm, registry, "runX", sub, resolver=fetcher, extracted_by="fake-model"))

        assert finding.status is RunStatus.OK, finding.error
        assert len(finding.claims) == 1
        claim = finding.claims[0]
        assert claim.claim_id == "runX:SQ1:001"
        assert claim.quote_locator.startswith(str(tmp_path))
        assert claim.extracted_by == "fake-model"
        # 用量由代码从对话统计，模型从不自报
        assert finding.search_queries == ["MCP 架构"]
        assert finding.pages_fetched == 1

    def test_repair_round_recovers_bad_output(self, tmp_path):
        """首轮引文编造 → 修复轮重新走工具拿真片段 → 全量重建，claim_id 稳定。"""
        fetcher = PageFetcher(tmp_path, client=mocked_client())
        registry = build_registry(tmp_path, fetcher)
        bad = json.dumps([{"text": "编造", "quote": "不存在的引文", "snippet_id": "ghost:1-2"}])
        llm = SmartFakeLLM([
            LLMResult(content=bad),                       # 首轮：直接给坏论断
            LLMResult(content=None, tool_calls=[          # 修复轮：重新检索
                tc("r1", "web_search", '{"query": "MCP"}')]),
            LLMResult(content=None, tool_calls=[
                tc("r2", "fetch_page", json.dumps({"url": URL, "query": "MCP"}))]),
            claims_from_last_tool_message,                # 修复轮：给好论断
        ])
        sub = SubQuery(sub_query_id="SQ1", question="MCP 是什么")
        finding = run(research_sub_query(
            llm, registry, "runX", sub, resolver=fetcher, extracted_by="m"))

        assert finding.status is RunStatus.OK, finding.error
        assert len(finding.claims) == 1
        assert finding.claims[0].claim_id == "runX:SQ1:001"  # 位置稳定，不因修复错位

    def test_tokens_and_usage_accumulate_across_repair(self, tmp_path):
        """修复轮是全新 AgentLoop（检索可能重做）——用量与工具统计必须
        跨全部轮次累计，只统计胜出轮会系统性低估成本（第五轮 P3）。"""
        fetcher = PageFetcher(tmp_path, client=mocked_client())
        registry = build_registry(tmp_path, fetcher)
        bad = json.dumps([{"text": "编造", "quote": "不存在的引文", "snippet_id": "ghost:1-2"}])
        llm = SmartFakeLLM([
            LLMResult(content=bad, usage_in=50, usage_out=5),                # 首轮：坏输出
            LLMResult(content=None, usage_in=30, usage_out=3,                # 修复轮：重新检索
                      tool_calls=[tc("r1", "web_search", '{"query": "MCP"}')]),
            LLMResult(content=None, usage_in=20, usage_out=2,
                      tool_calls=[tc("r2", "fetch_page", json.dumps({"url": URL, "query": "MCP"}))]),
            claims_from_last_tool_message,                                   # usage 10/1
        ])
        sub = SubQuery(sub_query_id="SQ1", question="MCP 是什么")
        finding = run(research_sub_query(
            llm, registry, "runX", sub, resolver=fetcher, extracted_by="m"))

        assert finding.status is RunStatus.OK, finding.error
        assert finding.tokens_in == 50 + 30 + 20 + 10
        assert finding.tokens_out == 5 + 3 + 2 + 1
        assert finding.search_queries == ["MCP"]
        assert finding.pages_fetched == 1

    def test_all_failed_gives_failed_finding(self, tmp_path):
        fetcher = PageFetcher(tmp_path, client=mocked_client())
        registry = build_registry(tmp_path, fetcher)
        llm = SmartFakeLLM([
            LLMResult(content=None, tool_calls=[tc("t1", "web_search", '{"query": "x"}')]),
            LLMResult(content="[]"),   # 合法但空
            LLMResult(content="[]"),   # 修复轮仍空
        ])
        sub = SubQuery(sub_query_id="SQ2", question="q")
        finding = run(research_sub_query(
            llm, registry, "runX", sub, resolver=fetcher, extracted_by="m"))
        assert finding.status is RunStatus.FAILED
        assert not finding.claims
