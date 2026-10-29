"""eval.runner 测试：A0 基线、A2 全链路组装、汇总表、落库往返（全部离线）。"""

import asyncio
import json
import re

import pytest

from eval.runner import (
    build_record,
    estimate_cost,
    git_version,
    load_dataset,
    render_report,
    run_batch,
    vendor_of,
)
from src.agent.llm import LLMResult, ToolCallRequest
from src.agent.schemas import RunStatus
from src.agent.storage.db import Database
from tests.conftest import SmartFakeLLM, final

PLAN_3 = json.dumps([
    {"question": "子问题一", "search_hints": []},
    {"question": "子问题二", "search_hints": []},
    {"question": "子问题三", "search_hints": []},
], ensure_ascii=False)

DATASET_1 = """- question_id: t001
  question: "测试研究问题？"
  type: fact_check
  difficulty: 2
  as_of_date: 2026-11-01
  key_points:
    - "要点一"
    - "要点二"
    - "要点三"
"""


def run(coro):
    return asyncio.run(coro)


def make_settings(tmp_path):
    from src.agent.config import Settings
    return Settings(
        llm_api_key="test-key", llm_model="fake-model",
        database_path=str(tmp_path / "agent.db"),
        trace_dir=str(tmp_path / "traces"),
        pages_dir=str(tmp_path / "pages"),
        tavily_api_key="",
        judge_api_key="jk", judge_model="judge-model",
        judge_base_url="https://judge.example.com/v1",
    )


def judge_fake():
    """动态 judge：忠实度批量全 supported；覆盖率第 1 要点命中（evidence [1]）。"""
    def handle(messages, **kw):
        sys = messages[0]["content"]
        if "忠实度" in sys:
            payload = json.loads(messages[-1]["content"])
            return final(json.dumps(
                [{"claim_id": c["claim_id"], "label": "supported", "rationale": "ok"}
                 for c in payload], ensure_ascii=False))
        payload = json.loads(messages[-1]["content"])
        return final(json.dumps([
            {"key_point": kp, "covered_in_report": i == 0,
             "evidence": [1] if i == 0 else [], "covered_by_retrieval": i == 0}
            for i, kp in enumerate(payload["key_points"])], ensure_ascii=False))
    return SmartFakeLLM([handle] * 12)


def tc(cid, name, args):
    return ToolCallRequest(id=cid, name=name, arguments_raw=args)


class TestHelpers:
    def test_vendor_of(self):
        assert vendor_of("https://api.deepseek.com") == "deepseek"
        assert vendor_of("https://open.bigmodel.cn/api") == "bigmodel"
        assert vendor_of("https://judge.example.com/v1") == "judge"  # 首个非常见前缀段

    def test_estimate_cost(self):
        assert estimate_cost("deepseek-flash", 1_000_000, 250_000) == 2.0
        assert estimate_cost("unknown-model", 999, 999) == 0.0

    def test_git_version_shape(self):
        v = git_version()
        assert v == "unknown" or re.fullmatch(r"[0-9a-f]{7,}(-dirty)?", v)

    def test_load_dataset(self, tmp_path):
        p = tmp_path / "ds.yaml"
        p.write_text(DATASET_1, encoding="utf-8")
        questions, version = load_dataset(p)
        assert len(questions) == 1
        q = questions[0]
        assert q.question_type.value == "fact_check" and len(q.key_points) == 3
        assert re.fullmatch(r"[0-9a-f]{8}", version)


def a0_llm(url: str):
    """A0 假件：fetch → 从工具返回抄 snippet_id → 自带编号的 claims+sections。"""
    def dynamic(messages):
        tool_msg = [m for m in messages if m["role"] == "tool"][-1]["content"]
        sid = re.search(r"id=([^\]]+)\]", tool_msg).group(1)
        quote = tool_msg.split("]", 1)[1].strip()[:40]
        obj = {
            "claims": [{"text": "MCP 是开放标准", "quote": quote, "snippet_id": sid}],
            "sections": [{"heading": "结论", "body": f"MCP 是开放标准……[[1]]"}],
        }
        return final(json.dumps(obj, ensure_ascii=False))
    return SmartFakeLLM([
        LLMResult(content=None, tool_calls=[tc("t1", "web_search", '{"query": "MCP"}')]),
        LLMResult(content=None, tool_calls=[
            tc("t2", "fetch_page", json.dumps({"url": url, "query": "MCP"}))]),
        dynamic,
    ])


class TestA0Record:
    def test_a0_end_to_end(self, tmp_path):
        from tests.test_fetch import URL, mocked_client

        settings = make_settings(tmp_path)
        db = Database(settings.database_path)
        record = run(build_record(
            load_dataset_qs(tmp_path)[0], "A0", settings,
            llm_factory=lambda: a0_llm(URL), judge_llm=judge_fake(),
            db=db, http_client=mocked_client(),
            code_version="test1", dataset_version="dsv1"))
        db.close()

        assert record.config == "A0" and record.verifications == []
        assert record.report is not None and len(record.report.citation_map) == 1
        assert record.faithfulness == 1.0          # judge 判 supported
        assert record.coverage == pytest.approx(1 / 3, abs=1e-3)  # 3 要点中 1 个有证据命中
        assert record.status is RunStatus.OK and record.succeeded
        assert record.judge_vendor == "judge"      # 与 producer 跨厂商
        assert record.check_judge_independence() == []
        # 覆盖证据 ⊆ citation_map（开放项 ② 口径由 judge 过滤保证）
        for h in record.key_point_hits:
            assert set(h.evidence_claim_ids) <= set(record.report.cited_claim_ids)


def load_dataset_qs(tmp_path):
    p = tmp_path / "ds.yaml"
    p.write_text(DATASET_1, encoding="utf-8")
    return load_dataset(p)[0]


class TestA2Record:
    def test_a2_record_with_judge_and_roundtrip(self, tmp_path):
        """planner+3researcher+verifier+writer → judge → EvalRecord 落库往返。"""
        from tests.test_fetch import URL, mocked_client
        from tests.test_orchestrator import mocked_a2_factory  # 复用 A2 假件工厂

        settings = make_settings(tmp_path)
        db = Database(settings.database_path)
        record = run(build_record(
            load_dataset_qs(tmp_path)[0], "A2", settings,
            llm_factory=mocked_a2_factory(URL), judge_llm=judge_fake(),
            db=db, http_client=mocked_client(),
            code_version="test1", dataset_version="dsv1"))

        claims = [c for f in record.findings for c in f.claims]
        assert len(claims) == 3
        assert len(record.verifications) == 3        # verifications 进记录（开放项：partial 拆解）
        assert record.faithfulness == 1.0
        assert record.coverage == pytest.approx(1 / 3, abs=1e-3)
        f = record.funnel()
        assert f.verifier_pass_rate == 1.0 and f.claims_unverified == 0
        assert record.succeeded
        # 落库往返（派生指标不序列化，读回重算一致）
        run(db.save_eval_record(record))
        db2 = Database(settings.database_path)
        loaded = db2.load_eval_records("A2")
        db2.close()
        assert len(loaded) == 1 and loaded[0] == record
        assert loaded[0].faithfulness == record.faithfulness


class TestRenderAndBatch:
    def test_render_report_pairs_metrics(self, tmp_path):
        from tests.test_fetch import URL, mocked_client
        from tests.test_orchestrator import mocked_a2_factory

        settings = make_settings(tmp_path)
        db = Database(settings.database_path)
        rec = run(build_record(
            load_dataset_qs(tmp_path)[0], "A2", settings,
            llm_factory=mocked_a2_factory(URL), judge_llm=judge_fake(),
            db=db, http_client=mocked_client(), code_version="t", dataset_version="d"))
        db.close()
        md = render_report([rec], "ds.yaml", "abc12345", 1, settings, "t", [])
        assert "| A2 |" in md
        assert "忠实度" in md and "覆盖率" in md and "F1" in md   # 成对输出
        assert "同源偏差自检：OK" in md
        assert "judge" in md  # judge 厂商标识出现在报告头

    def test_run_batch_writes_report_and_persists(self, tmp_path, monkeypatch):
        from tests.test_fetch import URL, mocked_client
        from tests.test_orchestrator import mocked_a2_factory

        monkeypatch.chdir(tmp_path)  # 报告写到 cwd/eval/reports
        settings = make_settings(tmp_path)
        ds = tmp_path / "ds.yaml"
        ds.write_text(DATASET_1, encoding="utf-8")
        md, records = run(run_batch(
            str(ds), ["A2"], settings=settings, llm_factory=mocked_a2_factory(URL),
            judge_factory=lambda s: judge_fake(), http_client=mocked_client()))
        assert len(records) == 1 and records[0].succeeded
        reports = list((tmp_path / "eval" / "reports").glob("eval-*.md"))
        assert len(reports) == 1 and "| A2 |" in reports[0].read_text(encoding="utf-8")

    def test_unknown_config_rejected(self, tmp_path):
        from tests.test_fetch import mocked_client

        settings = make_settings(tmp_path)
        ds = tmp_path / "ds.yaml"
        ds.write_text(DATASET_1, encoding="utf-8")
        with pytest.raises(SystemExit):
            run(run_batch(str(ds), ["X9"], settings=settings,
                          llm_factory=lambda: SmartFakeLLM([]),
                          judge_factory=lambda s: judge_fake(),
                          http_client=mocked_client()))
