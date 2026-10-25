"""Planner 测试：解析、校验、回喂重试、id 由代码分配。"""

import json

import pytest

from src.agent.agents.planner import PlanError, make_plan, parse_plan
from tests.conftest import SmartFakeLLM, final

PLAN_3 = json.dumps([
    {"question": "MCP 是什么", "search_hints": ["mcp protocol"]},
    {"question": "MCP 解决什么问题", "search_hints": ["mcp problem"]},
    {"question": "MCP 现状如何", "search_hints": ["mcp adoption"]},
], ensure_ascii=False)


class TestParsePlan:
    def test_fenced_json_tolerated(self):
        items = parse_plan(f"```json\n{PLAN_3}\n```")
        assert len(items) == 3

    def test_count_out_of_range_rejected(self):
        two = json.dumps([{"question": "a"}, {"question": "b"}])
        with pytest.raises(ValueError, match="数量"):
            parse_plan(two)

    def test_bad_item_reported_with_index(self):
        with pytest.raises(ValueError, match="第 1 项"):
            parse_plan('[' + json.dumps({"question": "a"}) + ',{"question": ""}]')

    def test_no_array_rejected(self):
        with pytest.raises(ValueError, match="JSON 数组"):
            parse_plan("我认为应该分为三个方面：……")


class TestMakePlan:
    def test_ids_assigned_by_code(self):
        plan = make_plan_ok(PLAN_3)
        assert [s.sub_query_id for s in plan] == ["SQ1", "SQ2", "SQ3"]
        assert plan[0].search_hints == ["mcp protocol"]

    def test_invalid_then_valid_retries(self):
        llm = SmartFakeLLM([final("抱歉，我需要先说明……"), final(PLAN_3)])
        plan = make_plan_sync(llm, "研究 MCP")
        assert len(plan) == 3
        # 第二次调用收到了带错误说明的修正提示
        assert "不符合要求" in llm.calls[1][-1]["content"]

    def test_exhausted_raises_plan_error(self):
        llm = SmartFakeLLM([final("不是 JSON")] * 3)
        with pytest.raises(PlanError):
            make_plan_sync(llm, "研究 MCP")


def make_plan_ok(plan_json: str):
    return make_plan_sync(SmartFakeLLM([final(plan_json)]), "研究 MCP")


def make_plan_sync(llm, question):
    import asyncio
    return asyncio.run(make_plan(llm, question))
