"""eval.runner 测试：A0 基线、A2 全链路组装、汇总表、落库往返（全部离线）。"""

import asyncio
import json
import re

import pytest

from eval.runner import (
    build_record,
    estimate_cost,
    git_version,
    latest_per_question,
    load_dataset,
    make_judge_llm,
    render_report,
    run_batch,
    vendor_of,
)
from src.agent.config import Settings
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
    """动态 judge：忠实度批量全 supported；覆盖率第 1 要点命中（evidence [1]）。
    结果带 model 真身——断言 judge_model 钉版本（第十一轮）。"""
    def handle(messages, **kw):
        sys = messages[0]["content"]
        if "忠实度" in sys:
            payload = json.loads(messages[-1]["content"])
            return LLMResult(content=json.dumps(
                [{"claim_id": c["claim_id"], "label": "supported", "rationale": "ok"}
                 for c in payload], ensure_ascii=False), model="mock-judge-1017")
        payload = json.loads(messages[-1]["content"])
        return LLMResult(content=json.dumps([
            {"key_point": kp, "covered_in_report": i == 0,
             "evidence": [1] if i == 0 else [], "covered_by_retrieval": i == 0}
            for i, kp in enumerate(payload["key_points"])], ensure_ascii=False),
            model="mock-judge-1017")
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
        # resp.model 真身进 model_snapshots / judge_model（第十一轮：钉版本）
        assert set(record.model_snapshots.values()) == {"mock-flash-1017"}
        assert record.judge_model == "mock-judge-1017"
        assert record.judge_effort == "high"  # judge 档位进记录（复现参数）
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
        assert "judge 档位：high" in md  # 档位进报告头（复现参数）
        assert "judge" in md  # judge 厂商标识出现在报告头

        # 同表混用 judge 配置时必须显式告警，别让读者以为数字可比。
        # 注意用不同 question_id：同题不同档会被渲染层去重（第十四轮 P2
        # 保留最新一条）——混用告警的语义是"跨题混档"，那才是真实风险。
        other = rec.model_copy(update={"question_id": "q002", "judge_effort": "low"})
        md2 = render_report([rec, other], "ds.yaml", "abc12345", 2, settings, "t", [])
        assert "配置混用" in md2 and "low" in md2

        # 重跑单题后 latest-only 只留最新一条（按 rowid 升序 → 后者）
        old = rec.model_copy(update={"question_id": "q1", "code_version": "old"})
        new = rec.model_copy(update={"question_id": "q1", "code_version": "new"})
        assert latest_per_question([old, new]) == [new]

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


class TestNinthAudit:
    """第九轮审计修复的回归。"""

    def test_judge_vendor_fallback_flags_independence(self, tmp_path):
        """P1：JUDGE_BASE_URL 留空 → judge 实际回落生产 base_url，
        vendor 必须回落成真名让自检抓到，而不是 'unknown' 静默放行。"""
        from tests.test_fetch import URL, mocked_client
        from tests.test_orchestrator import mocked_a2_factory

        settings = make_settings(tmp_path).model_copy(update={"judge_base_url": ""})
        db = Database(settings.database_path)
        record = run(build_record(
            load_dataset_qs(tmp_path)[0], "A2", settings,
            llm_factory=mocked_a2_factory(URL), judge_llm=judge_fake(),
            db=db, http_client=mocked_client(), code_version="t", dataset_version="d"))
        db.close()
        assert record.judge_vendor == "deepseek"   # 与 producer 同厂商（诚实）
        assert record.check_judge_independence()   # 自检必须报警

    def test_batch_survives_record_crash(self, tmp_path, monkeypatch):
        """P0-2：一题抛异常 → FAILED 占位记录进分母，批继续、报告照常落盘。"""
        from tests.test_fetch import URL, mocked_client
        from tests.test_orchestrator import mocked_a2_factory

        monkeypatch.chdir(tmp_path)
        settings = make_settings(tmp_path)
        ds = tmp_path / "ds.yaml"
        # 两道题：第一道正常（A2 全链路），第二道 planner 输出永远不是 JSON → PlanError
        ds.write_text(DATASET_1 + DATASET_1.replace("t001", "t002"), encoding="utf-8")

        def factory():
            a2f = mocked_a2_factory(URL)  # 单独实例：6 次调用内计数自洽
            def inner():
                factory.state = getattr(factory, "state", 0) + 1
                if factory.state <= 6:  # 第 1 题：planner+3res+verifier+writer
                    return a2f()
                return SmartFakeLLM([final("我不是 JSON")] * 3)  # 第 2 题：规划崩溃
            return inner

        md, records = run(run_batch(
            str(ds), ["A2"], settings=settings, llm_factory=factory(),
            judge_factory=lambda s: judge_fake(), http_client=mocked_client()))
        assert len(records) == 2
        assert records[0].succeeded
        assert records[1].status is RunStatus.FAILED
        assert "PlanError" in (records[1].error or "")
        # 失败样本进分母：成功率 50%，且报告照常产出
        assert "50%" in md
        assert list((tmp_path / "eval" / "reports").glob("eval-*.md"))

    def test_report_only_rerenders_from_db(self, tmp_path, monkeypatch):
        from tests.test_fetch import URL, mocked_client
        from tests.test_orchestrator import mocked_a2_factory
        from eval.runner import render_from_db

        monkeypatch.chdir(tmp_path)
        settings = make_settings(tmp_path)
        db = Database(settings.database_path)
        rec = run(build_record(
            load_dataset_qs(tmp_path)[0], "A2", settings,
            llm_factory=mocked_a2_factory(URL), judge_llm=judge_fake(),
            db=db, http_client=mocked_client(), code_version="t", dataset_version="d"))
        run(db.save_eval_record(rec))
        db.close()
        md = render_from_db(settings, "ds.yaml")
        assert "| A2 |" in md and "有报告n" in md

    def test_unverified_judge_note_reaches_flags(self, tmp_path):
        """cover_parse_failed 必须出现在 quality_flags——数据缺口显式报数。"""
        def bad_handle(messages, **kw):
            return final(json.dumps(
                [{"key_point": kp, "covered_in_report": True, "evidence": None,
                  "covered_by_retrieval": True} for kp in
                 ["要点一", "要点二", "要点三"]]))
        from tests.test_fetch import URL, mocked_client
        from tests.test_orchestrator import mocked_a2_factory

        settings = make_settings(tmp_path)
        db = Database(settings.database_path)
        # judge 假件要供 4 次调用：忠实度批 2 次（无 label→全部缺口）+ 覆盖 2 次（重试）
        record = run(build_record(
            load_dataset_qs(tmp_path)[0], "A2", settings,
            llm_factory=mocked_a2_factory(URL),
            judge_llm=SmartFakeLLM([bad_handle] * 4),
            db=db, http_client=mocked_client(), code_version="t", dataset_version="d"))
        db.close()
        assert "cover_parse_failed" in record.judge_notes
        assert "cover_parse_failed" in record.quality_flags()

    def test_degraded_report_not_flagged_no_report(self):
        """第十轮真实冒烟 P1：有报告的 degraded 题贴 degraded，
        不再与 no_report 自相矛盾（会污染 W6 全表成功率）。"""
        from datetime import date

        from src.agent.schemas import EvalRecord, ReportArtifact, RunStatus

        rec = EvalRecord(
            run_id="r", question_id="q1", config="A2", code_version="t",
            prompt_version="p1", dataset_version="d",
            question="问题", question_type="fact_check", difficulty=2,
            key_points=["a", "b", "c"], as_of_date=date(2026, 11, 1),
            status=RunStatus.DEGRADED,
            report=ReportArtifact(markdown="# 报告"),
        )
        flags = rec.quality_flags()
        assert "degraded" in flags and "no_report" not in flags

    def test_render_splits_report_rate_and_green_rate(self, tmp_path):
        from tests.test_fetch import URL, mocked_client
        from tests.test_orchestrator import mocked_a2_factory

        settings = make_settings(tmp_path)
        db = Database(settings.database_path)
        rec = run(build_record(
            load_dataset_qs(tmp_path)[0], "A2", settings,
            llm_factory=mocked_a2_factory(URL), judge_llm=judge_fake(),
            db=db, http_client=mocked_client(), code_version="t", dataset_version="d"))
        db.close()
        md = render_report([rec], "ds.yaml", "abc", 1, settings, "t", [])
        assert "有报告率" in md and "全绿率" in md  # 两列拆开，成功率不再含糊
        assert "共同题集" not in md  # 单配置无跨配置比较，不渲染该表

    def test_render_common_subset_table(self):
        """第十三轮：跨配置均值以共同题集为准——缺失数据偏差的结构性防线
        （W6 实测：自有样本均值让 A0 vs A1 忠实度反转 5 分）。"""
        from datetime import date

        from src.agent.schemas import EvalRecord, ReportArtifact, RunStatus

        def rec(cfg: str, qid: str, with_report: bool,
                run_id: str | None = None) -> EvalRecord:
            return EvalRecord(
                run_id=run_id or f"r-{cfg}-{qid}", question_id=qid, config=cfg,
                code_version="t", prompt_version="p1", dataset_version="d",
                question="问题", question_type="fact_check", difficulty=2,
                key_points=["a", "b", "c"], as_of_date=date(2026, 11, 1),
                status=RunStatus.DEGRADED,
                report=ReportArtifact(markdown="# 报告") if with_report else None,
            )

        records = [
            rec("A0", "t001", True), rec("A0", "t002", False),  # A0 缺 t002
            rec("A2", "t001", True), rec("A2", "t002", True),
        ]
        from src.agent.config import Settings
        settings = Settings(llm_api_key="k", llm_model="fake-model",
                            judge_api_key="jk", judge_model="judge-model",
                            judge_base_url="https://judge.example.com/v1")
        md = render_report(records, "ds.yaml", "abc", 2, settings, "t", [])
        assert "共同题集（1 题" in md  # 只有 t001 两方都有报告
        # 主表 A0 n=2（含无报告），共同题集表 A0 n=1——分母差异可见（P4：断 n 值）
        assert "| A0 | 2 " in md and "| A2 | 2 " in md
        assert "| A0 | 1 " in md and "| A2 | 1 " in md

    def test_dedupe_keeps_latest_run(self):
        """第十四轮 P2：重跑过的题按运行次数加权会稀释失败题权重——
        渲染层内部去重，不依赖调用方记得加 --latest-only。"""
        from datetime import date

        from src.agent.schemas import EvalRecord, ReportArtifact, RunStatus

        def rec(cfg: str, qid: str, run_id: str, with_report: bool) -> EvalRecord:
            return EvalRecord(
                run_id=run_id, question_id=qid, config=cfg,
                code_version="t", prompt_version="p1", dataset_version="d",
                question="问题", question_type="fact_check", difficulty=2,
                key_points=["a", "b", "c"], as_of_date=date(2026, 11, 1),
                status=RunStatus.DEGRADED,
                report=ReportArtifact(markdown="# 报告") if with_report else None,
            )

        records = [
            rec("A2", "t001", "r-aaa", False),  # 旧跑失败
            rec("A2", "t001", "r-zzz", True),   # 重跑成功 → 采纳
        ]
        from src.agent.config import Settings
        settings = Settings(llm_api_key="k", llm_model="fake-model")
        md = render_report(records, "ds.yaml", "abc", 1, settings, "t", [])
        assert "| A2 | 1 " in md            # 不按运行次数加权（否则是 2）
        assert "共同题集" not in md          # 单配置不渲染

    def test_common_table_shows_effective_n(self):
        """第十四轮 P1：共同题集按'有报告'划集，但零引用报告的忠实度是
        None——被 _mean 跳过必须以 (n=k) 可见，否则同一偏差从跨配置
        下沉到跨题。"""
        from datetime import date

        from src.agent.schemas import (
            Claim,
            ClaimJudgment,
            EvalRecord,
            Finding,
            ReportArtifact,
            RunStatus,
            SupportLabel,
        )

        def full(cfg: str, qid: str, cited: bool) -> EvalRecord:
            c = Claim(claim_id=f"{cfg}:{qid}:001", sub_query_id=qid, text="论断",
                      quote="引文", url="https://e.com",
                      as_of_date=date(2026, 11, 1), extracted_by="m")
            report = ReportArtifact(
                markdown="# 报告" + ("[1]" if cited else ""),
                citation_map={1: c.claim_id} if cited else {})
            return EvalRecord(
                run_id=f"r-{cfg}-{qid}", question_id=qid, config=cfg,
                code_version="t", prompt_version="p1", dataset_version="d",
                question="问题", question_type="fact_check", difficulty=2,
                key_points=["a", "b", "c"], as_of_date=date(2026, 11, 1),
                status=RunStatus.OK,
                findings=[Finding(sub_query_id=qid, question="q",
                                  status=RunStatus.OK, claims=[c])],
                report=report,
                judgments=[ClaimJudgment(claim_id=c.claim_id,
                                         judge_label=SupportLabel.SUPPORTED,
                                         judge_model="j")] if cited else [],
            )

        # A0 两题都有引用判定；A2 的 t001 零引用 → 忠实度 None
        records = [
            full("A0", "t001", True), full("A0", "t002", True),
            full("A2", "t001", False), full("A2", "t002", True),
        ]
        from src.agent.config import Settings
        settings = Settings(llm_api_key="k", llm_model="fake-model",
                            judge_api_key="jk", judge_model="judge-model",
                            judge_base_url="https://judge.example.com/v1")
        md = render_report(records, "ds.yaml", "abc", 2, settings, "t", [])
        assert "共同题集（2 题" in md
        common_section = md.split("共同题集")[1]
        # A2 忠实度均值只算 1 题，有效样本数必须可见
        assert "(n=1)" in common_section
        # A0 无缺口：不带 (n=) 后缀
        a0_common = common_section.split("| A0 |")[1].splitlines()[0]
        assert "(n=" not in a0_common

    def test_common_empty_is_announced(self):
        """第十四轮 P3：共同题集为空时输出说明行，不静默消失。"""
        from datetime import date

        from src.agent.schemas import EvalRecord, ReportArtifact, RunStatus

        def rec(cfg: str, qid: str, with_report: bool) -> EvalRecord:
            return EvalRecord(
                run_id=f"r-{cfg}-{qid}", question_id=qid, config=cfg,
                code_version="t", prompt_version="p1", dataset_version="d",
                question="问题", question_type="fact_check", difficulty=2,
                key_points=["a", "b", "c"], as_of_date=date(2026, 11, 1),
                status=RunStatus.DEGRADED,
                report=ReportArtifact(markdown="# 报告") if with_report else None,
            )

        records = [
            rec("A2", "t001", True), rec("A2", "t002", True),
            rec("A0", "t001", False), rec("A0", "t002", False),  # A0 全挂
        ]
        from src.agent.config import Settings
        settings = Settings(llm_api_key="k", llm_model="fake-model")
        md = render_report(records, "ds.yaml", "abc", 2, settings, "t", [])
        assert "共同题集为空" in md and "跨配置均值不可比" in md

    def test_safe_print_survives_gbk_console(self, monkeypatch):
        """P0：GBK 控制台打 ¥ → UnicodeEncodeError → 退出码 1。
        兜底逻辑必须 reconfigure(errors='replace') 后重打。"""
        import io

        from eval.runner import _safe_print

        class GbkStdout:
            def __init__(self):
                self.reconfigured = False
                self.chunks: list[str] = []

            def write(self, s):
                if not self.reconfigured:
                    raise UnicodeEncodeError("gbk", "¥", 0, 1, "cannot encode")
                self.chunks.append(s)
                return len(s)

            def flush(self):
                pass

            def reconfigure(self, **kw):
                assert kw.get("errors") == "replace"
                self.reconfigured = True

        fake = GbkStdout()
        monkeypatch.setattr("sys.stdout", fake)
        _safe_print("成本¥0.27")
        assert fake.reconfigured
        assert any("¥" in c for c in fake.chunks)

    def test_jobs_parallel_questions_both_crash(self, tmp_path, monkeypatch):
        """题间并发（W6 用）：两题同时崩 → 两题都记 FAILED，批照常收尾。"""
        from tests.test_fetch import mocked_client

        monkeypatch.chdir(tmp_path)
        settings = make_settings(tmp_path)
        ds = tmp_path / "ds.yaml"
        ds.write_text(DATASET_1 + DATASET_1.replace("t001", "t002"), encoding="utf-8")
        md, records = run(run_batch(
            str(ds), ["A2"], settings=settings,
            llm_factory=lambda: SmartFakeLLM([final("不是 JSON")] * 3),
            judge_factory=lambda s: judge_fake(), http_client=mocked_client(),
            jobs=2))
        assert len(records) == 2
        assert all(r.status is RunStatus.FAILED for r in records)
        assert list((tmp_path / "eval" / "reports").glob("eval-*.md"))


class TestJudgeEffortWiring:
    """judge 档位独立于 producer：GLM 强制思考，disabled 会 400（llm.thinking_body）。"""

    def _judge(self, **kw):
        s = Settings(judge_api_key="jk", judge_model="glm-5.3-flash",
                     judge_base_url="https://open.bigmodel.cn/api/paas/v4", **kw)
        return make_judge_llm(s)

    def test_default_high(self):
        assert self._judge()._s.llm_thinking == "high"

    def test_explicit_low(self):
        assert self._judge(judge_thinking="low")._s.llm_thinking == "low"

    def test_producer_setting_not_leaked(self):
        # 生产链路 disabled（DeepSeek 关思考）不能覆盖 judge 档位
        assert self._judge(llm_thinking="disabled")._s.llm_thinking == "high"
