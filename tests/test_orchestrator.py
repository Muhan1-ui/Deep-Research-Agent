"""Orchestrator 测试：规划→并行→落库→trace 全链路（离线，FakeLLM 工厂注入）。"""

import asyncio
import json

from src.agent.config import Settings
from src.agent.orchestrator.run import run_research
from src.agent.schemas import RunStatus
from src.agent.storage.db import Database
from tests.conftest import SmartFakeLLM, final

PLAN_3 = json.dumps([
    {"question": "子问题一", "search_hints": []},
    {"question": "子问题二", "search_hints": []},
    {"question": "子问题三", "search_hints": []},
], ensure_ascii=False)

EMPTY_CLAIMS = "[]"


def run(coro):
    return asyncio.run(coro)


def make_settings(tmp_path) -> Settings:
    return Settings(
        llm_api_key="test-key", llm_model="fake",
        database_path=str(tmp_path / "agent.db"),
        trace_dir=str(tmp_path / "traces"),
        pages_dir=str(tmp_path / "pages"),
        tavily_api_key="",   # 不注册 web_search：researcher 直接给最终输出
    )


class CrashingLLM:
    """complete 即崩：验证单子任务失败不拖垮 run。"""

    async def complete(self, messages, tools=None, **kw):
        raise RuntimeError("模拟 researcher 内部崩溃")


class TestRunResearch:
    def test_plan_parallel_persist_trace(self, tmp_path):
        settings = make_settings(tmp_path)
        # 第 1 个 llm 给 planner，之后每个 researcher 一个：都返回合法空论断
        counter = {"n": 0}

        def factory():
            counter["n"] += 1
            if counter["n"] == 1:
                return SmartFakeLLM([final(PLAN_3)])
            return SmartFakeLLM([final(EMPTY_CLAIMS), final(EMPTY_CLAIMS)])

        result = run(run_research("研究 MCP", settings=settings, llm_factory=factory))

        assert len(result.plan) == 3
        assert len(result.findings) == 3
        # 空论断 → FAILED（合法降级：run 不崩、状态如实）
        assert all(f.status is RunStatus.FAILED for f in result.findings)
        assert result.total_claims == 0

        db = Database(settings.database_path)
        persisted = db.load_findings(result.run_id)
        db.close()
        assert len(persisted) == 3  # 落库可查（DoD：全程状态可查）

        trace_lines = (tmp_path / "traces" / f"{result.run_id}.jsonl").read_text(
            encoding="utf-8").strip().splitlines()
        events = [json.loads(x)["event"] for x in trace_lines]
        assert events[0] == "run_start" and events[1] == "plan"
        assert events.count("sub_done") == 3 and events[-1] == "run_done"

    def test_researcher_crash_degrades_not_kills(self, tmp_path):
        settings = make_settings(tmp_path)

        def factory():
            factory.n = getattr(factory, "n", 0) + 1
            if factory.n == 1:
                return SmartFakeLLM([final(PLAN_3)])
            return CrashingLLM()

        result = run(run_research("研究 MCP", settings=settings, llm_factory=factory))
        assert all(f.status is RunStatus.FAILED for f in result.findings)
        assert all("RuntimeError" in (f.error or "") for f in result.findings)

    def test_run_id_unique_across_runs(self, tmp_path):
        """两次 run 的 id 必不相同（claim_id 已带 run_id，这里是源头保证）。"""
        settings = make_settings(tmp_path)

        def factory():
            state = getattr(factory, "state", None)
            if state is None:
                state = factory.state = {"n": 0}
            state["n"] += 1
            if state["n"] == 1:
                return SmartFakeLLM([final(PLAN_3)])
            return SmartFakeLLM([final(EMPTY_CLAIMS)])

        r1 = run(run_research("q", settings=settings, llm_factory=factory))
        factory.state = None
        r2 = run(run_research("q", settings=settings, llm_factory=factory))
        assert r1.run_id != r2.run_id
