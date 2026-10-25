"""Orchestrator 测试：规划→并行→落库→trace 全链路（离线，FakeLLM 工厂注入）。"""

import asyncio
import json

import pytest

from src.agent.config import Settings
from src.agent.orchestrator.run import _aggregate_status, run_research
from src.agent.schemas import Claim, Finding, RunStatus
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


# ------------------ 失败矩阵（第五轮审计：盲区在"不成功的执行"里） ------------------ #


def _claim(sub: str) -> Claim:
    from datetime import date
    return Claim(
        claim_id=f"runX:{sub}:001", sub_query_id=sub, text="论断", quote="引文",
        url="https://e.com", as_of_date=date(2026, 11, 2), extracted_by="m",
    )


def ok_finding(sub: str) -> Finding:
    return Finding(sub_query_id=sub, question="q", status=RunStatus.OK,
                   claims=[_claim(sub)])


def failed_finding(sub: str) -> Finding:
    return Finding(sub_query_id=sub, question="q", status=RunStatus.FAILED,
                   error="模拟失败")


class TestAggregateStatus:
    """P1-a 回归：FAILED 不进等式 = 成功率虚高。"""

    def test_one_failed_among_ok_is_degraded(self):
        findings = [ok_finding("SQ1"), failed_finding("SQ2"), ok_finding("SQ3")]
        assert _aggregate_status(findings) is RunStatus.DEGRADED  # 修复前是 OK

    def test_all_ok(self):
        assert _aggregate_status([ok_finding("SQ1"), ok_finding("SQ2")]) is RunStatus.OK

    def test_all_failed_without_claims(self):
        assert _aggregate_status([failed_finding("SQ1"), failed_finding("SQ2")]) is RunStatus.FAILED


def run_row(db_path, run_id):
    db = Database(db_path)
    row = db._conn.execute(
        "SELECT status, finished_at, config, error FROM runs WHERE run_id=?",
        (run_id,)).fetchone()
    db.close()
    return row


class TestFailureMatrix:
    """四条失败路径各一个用例：1 挂 / 全挂 / 中途异常 / 被杀后重开。"""

    def test_all_failed_marks_run_failed_in_db(self, tmp_path):
        settings = make_settings(tmp_path)
        result = run(run_research("q", settings=settings, llm_factory=planner_then_empty()))
        row = run_row(settings.database_path, result.run_id)
        assert row["status"] == "failed" and row["finished_at"]  # 终态 + 完成时间

    def test_planner_crash_marks_run_failed_and_reraises(self, tmp_path):
        settings = make_settings(tmp_path)
        # 抓 run_id：trace 文件名就是 run_id（进程内拿不到返回值——异常直接抛出）
        with pytest.raises(RuntimeError):
            run(run_research("q", settings=settings,
                             llm_factory=lambda: SmartFakeLLM([RuntimeError("planner 崩")])))
        trace_file = next((tmp_path / "traces").glob("*.jsonl"))
        run_id = trace_file.stem
        row = run_row(settings.database_path, run_id)
        assert row["status"] == "failed"
        assert "RuntimeError" in row["error"]

    def test_killed_process_leaves_running_not_ok(self, tmp_path):
        """被杀（SIGKILL/OOM）模拟：save_run 之后进程消失 → 行停在 running，
        finished_at 为 NULL——不再伪装成功；聚合必须过滤 finished_at IS NULL。"""
        db = Database(tmp_path / "t.db")
        run(db.save_run("runKilled", "q", "A1"))
        db.close()  # 进程"死"了
        db2 = Database(tmp_path / "t.db")  # 事后审计者重开
        row = db2._conn.execute(
            "SELECT status, finished_at FROM runs WHERE run_id='runKilled'").fetchone()
        db2.close()
        assert row["status"] == "running"
        assert row["finished_at"] is None

    def test_config_stored_not_hardcoded(self, tmp_path):
        settings = make_settings(tmp_path)
        result = run(run_research("q", settings=settings,
                                  llm_factory=planner_then_empty(), config="A1"))
        row = run_row(settings.database_path, result.run_id)
        assert row["config"] == "A1"

    def test_unknown_config_rejected(self, tmp_path):
        settings = make_settings(tmp_path)
        with pytest.raises(SystemExit):
            run(run_research("q", settings=settings, config="X9"))


def planner_then_empty():
    def factory():
        state = getattr(factory, "state", None)
        if state is None:
            state = factory.state = {"n": 0}
        state["n"] += 1
        if state["n"] == 1:
            return SmartFakeLLM([final(PLAN_3)])
        return SmartFakeLLM([final(EMPTY_CLAIMS)])
    return factory
