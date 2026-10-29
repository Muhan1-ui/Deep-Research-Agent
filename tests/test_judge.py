"""eval judge 测试：忠实度判定、覆盖率判定、证据过滤（⊆ citation_map）、temperature=0。"""

import asyncio
import json
from datetime import date

from eval.judge.judge import FAITH_MARKER, judge_cited_claims, judge_key_points
from src.agent.schemas import Claim, ClaimJudgment, ReportArtifact, SupportLabel
from tests.conftest import SmartFakeLLM, final


def run(coro):
    return asyncio.run(coro)


def claim(cid: str, text: str = "论断") -> Claim:
    return Claim(
        claim_id=cid, sub_query_id=cid.split(":")[1], text=f"{text}{cid[-3:]}",
        quote=f"引文{cid[-3:]}", url=f"https://e.com/{cid[-3:]}",
        as_of_date=date(2026, 11, 2), extracted_by="m",
    )


def report_with(citation_map: dict[int, str]) -> ReportArtifact:
    return ReportArtifact(markdown="正文", citation_map=citation_map)


def _last_json(messages) -> dict | list:
    """取最后一条可解析的 user JSON（重试后最后一条是纠正消息，跳过）。"""
    for m in reversed(messages):
        c = m.get("content") or ""
        if c.lstrip().startswith(("[", "{")):
            return json.loads(c)
    raise ValueError("no json payload")


def faith_handle(default: str = "supported", skip: set[str] | None = None):
    """动态应答忠实度批量判定：全部 default，skip 里的漏判。"""
    skip = skip or set()

    def handle(messages, **kw):
        payload = _last_json(messages)
        return final(json.dumps(
            [{"claim_id": c["claim_id"], "label": default, "rationale": "引文支持"}
             for c in payload if c["claim_id"] not in skip],
            ensure_ascii=False))

    return handle


def cover_handle(n_points: int, covered: dict[int, list[int]]):
    """动态应答覆盖率判定：covered 里给 {要点序: evidence 编号}。"""

    def handle(messages, **kw):
        payload = _last_json(messages)
        out = []
        for i, kp in enumerate(payload["key_points"]):
            out.append({
                "key_point": kp,
                "covered_in_report": i in covered,
                "evidence": covered.get(i, []),
                "covered_by_retrieval": i in covered,
            })
        return final(json.dumps(out, ensure_ascii=False))

    return handle


class TestJudgeCitedClaims:
    def test_all_supported_with_model_tag(self):
        claims = [claim("r:S:1"), claim("r:S:2")]
        llm = SmartFakeLLM([faith_handle(), faith_handle()])
        js = run(judge_cited_claims(llm, claims, report_with({1: "r:S:1", 2: "r:S:2"}), "judge-x"))
        assert [j.judge_label for j in js] == [SupportLabel.SUPPORTED] * 2
        assert all(j.judge_model == "judge-x#j1" for j in js)
        assert llm.kwargs[0].get("temperature") == 0.0  # judge 必须 temp=0

    def test_batching_and_missing_verdict_is_gap(self):
        """>12 条分批；漏判的记 judge_label=None（数据缺口，非 supported）。"""
        claims = [claim(f"r:S:{i:03d}") for i in range(1, 14)]  # 13 条 → 2 批
        cmap = {i: f"r:S:{i:03d}" for i in range(1, 14)}
        llm = SmartFakeLLM([faith_handle(), faith_handle(), faith_handle()])
        js = run(judge_cited_claims(llm, claims, report_with(cmap), "j"))
        assert len(js) == 13
        assert all(j.judge_label is SupportLabel.SUPPORTED for j in js)

    def test_missing_in_batch_becomes_none(self):
        claims = [claim("r:S:1"), claim("r:S:2")]
        llm = SmartFakeLLM([faith_handle(skip={"r:S:2"}), faith_handle(skip={"r:S:2"})])
        js = run(judge_cited_claims(llm, claims, report_with({1: "r:S:1", 2: "r:S:2"}), "j"))
        by_id = {j.claim_id: j for j in js}
        assert by_id["r:S:1"].judge_label is SupportLabel.SUPPORTED
        assert by_id["r:S:2"].judge_label is None  # 缺口显式，quality_flags 会报 judge_gap

    def test_system_prompt_carries_independence_marker(self):
        llm = SmartFakeLLM([faith_handle(), faith_handle()])
        run(judge_cited_claims(llm, [claim("r:S:1")], report_with({1: "r:S:1"}), "j"))
        assert FAITH_MARKER in llm.calls[0][0]["content"]


class TestJudgeKeyPoints:
    def test_cover_with_evidence_mapping(self):
        """evidence 编号 → citation_map 里的 claim_id；notes 为空=正常。"""
        claims = [claim("r:S:1"), claim("r:S:2")]
        rep = report_with({1: "r:S:1"})
        llm = SmartFakeLLM([cover_handle(3, {0: [1]}), cover_handle(3, {0: [1]})])
        hits, notes = run(judge_key_points(
            llm, "问题", ["要点一", "要点二", "要点三"], rep, claims, "j"))
        assert notes == []
        assert hits[0].covered_in_report and hits[0].evidence_claim_ids == ["r:S:1"]
        assert not hits[1].covered_in_report and hits[1].evidence_claim_ids == []

    def test_uncited_evidence_number_filtered(self):
        """judge 报了 citation_map 外的编号 → 过滤；过滤后为空 → 如实降级未覆盖。"""
        claims = [claim("r:S:1"), claim("r:S:2")]
        rep = report_with({1: "r:S:1"})  # 只有 1 号被引用；2 号存在但未引用
        llm = SmartFakeLLM([cover_handle(2, {0: [2]}), cover_handle(2, {0: [2]})])
        hits, _ = run(judge_key_points(llm, "问题", ["要点一", "要点二"], rep, claims, "j"))
        assert not hits[0].covered_in_report and hits[0].evidence_claim_ids == []

    def test_shape_mismatch_retries_then_conservative_with_flag(self):
        """形状失败重试一次；仍失败 → 全部未覆盖 + cover_parse_failed 旗标
        （与'真的没写'可区分——第九轮审计的方法论缺口）。"""
        claims = [claim("r:S:1")]
        rep = report_with({1: "r:S:1"})
        llm = SmartFakeLLM([final("我觉得都覆盖了"), final("还是不对")])
        hits, notes = run(judge_key_points(llm, "问题", ["要点一", "要点二"], rep, claims, "j"))
        assert len(llm.calls) == 2
        assert all(not h.covered_in_report for h in hits)
        assert notes == ["cover_parse_failed"]

    def test_null_evidence_is_shape_failure(self):
        """P0-2 探针：'evidence': null 直接打断整批——现在必须是形状失败。"""
        def handle(messages, **kw):
            return final(json.dumps([
                {"key_point": "要点一", "covered_in_report": True,
                 "evidence": None, "covered_by_retrieval": True}]),)
        claims = [claim("r:S:1")]
        rep = report_with({1: "r:S:1"})
        llm = SmartFakeLLM([handle, handle])
        hits, notes = run(judge_key_points(llm, "问题", ["要点一"], rep, claims, "j"))
        assert len(llm.calls) == 2  # 重试了一次
        assert not hits[0].covered_in_report
        assert notes == ["cover_parse_failed"]

    def test_string_evidence_and_bool_rejected(self):
        """evidence "12" 逐字符解析成 12、covered_* "false" 经 bool() 变 True——
        两类假阳性都必须按形状失败处理。"""
        def handle(messages, **kw):
            return final(json.dumps([
                {"key_point": "要点一", "covered_in_report": "false",
                 "evidence": ["12"], "covered_by_retrieval": "true"}]))
        claims = [claim("r:S:1")]
        rep = report_with({1: "r:S:1"})
        llm = SmartFakeLLM([handle, handle])
        hits, notes = run(judge_key_points(llm, "问题", ["要点一"], rep, claims, "j"))
        assert notes == ["cover_parse_failed"]
        assert not hits[0].covered_in_report and hits[0].evidence_claim_ids == []

    def test_empty_citation_map_keeps_retrieval_signal(self):
        """报告零引用时覆盖判定照跑：检索命中如实评估，不被一并归零。"""
        claims = [claim("r:S:1"), claim("r:S:2")]
        rep = report_with({})  # 零引用
        llm = SmartFakeLLM([cover_handle(2, {0: [0]}), cover_handle(2, {0: [0]})])
        hits, notes = run(judge_key_points(llm, "问题", ["要点一", "要点二"], rep, claims, "j"))
        assert notes == []
        assert hits[0].covered_by_retrieval and not hits[0].covered_in_report
        assert hits[0].evidence_claim_ids == []  # citation_map 为空，无证据可挂

    def test_temperature_zero(self):
        llm = SmartFakeLLM([cover_handle(1, {0: [1]}), cover_handle(1, {0: [1]})])
        run(judge_key_points(llm, "问题", ["要点一"],
                             report_with({1: "r:S:1"}), [claim("r:S:1")], "j"))
        assert llm.kwargs[0].get("temperature") == 0.0
