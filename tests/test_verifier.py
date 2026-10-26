"""Verifier 测试：三分类判定、解析重试、漏判回退（保守 partial）、矛盾组。"""

import asyncio
import json
from datetime import date

from src.agent.agents.verifier import detect_contradictions, verify_claims
from src.agent.schemas import Claim, SupportLabel, Verification
from tests.conftest import SmartFakeLLM, final


def run(coro):
    return asyncio.run(coro)


def claim(cid: str) -> Claim:
    return Claim(
        claim_id=cid, sub_query_id=cid.split(":")[1], text=f"论断{cid}",
        quote=f"引文{cid}", url="https://e.com", as_of_date=date(2026, 11, 2),
        extracted_by="m",
    )


def verdicts_json(items: list[tuple[str, str]]) -> str:
    return json.dumps(
        [{"claim_id": cid, "label": lb, "rationale": "理由"} for cid, lb in items],
        ensure_ascii=False)


class TestVerifyClaims:
    def test_happy_batch(self):
        llm = SmartFakeLLM([final(verdicts_json([
            ("r:SQ1:001", "supported"), ("r:SQ1:002", "partial"),
            ("r:SQ1:003", "unsupported"),
        ]))])
        vs = run(verify_claims(llm, [claim("r:SQ1:001"), claim("r:SQ1:002"), claim("r:SQ1:003")],
                               checked_by="m"))
        assert [v.label for v in vs] == [
            SupportLabel.SUPPORTED, SupportLabel.PARTIAL, SupportLabel.UNSUPPORTED]
        assert all(v.checked_by == "m" and v.prompt_version == "v1" for v in vs)

    def test_retry_on_garbage_then_valid(self):
        llm = SmartFakeLLM([final("我觉得都没问题"), final(verdicts_json([("r:S:1", "supported")]))])
        vs = run(verify_claims(llm, [claim("r:S:1")], checked_by="m"))
        assert vs[0].label is SupportLabel.SUPPORTED
        assert "不符合要求" in llm.calls[1][-1]["content"]  # 回喂了错误说明

    def test_missing_verdict_retries_then_falls_back_flagged(self):
        """漏判必须重试（第六轮 P2-C）；重试仍缺 → fallback 且 unverified=True
        （与真 partial 严格区分）。"""
        partial = verdicts_json([("r:S:1", "supported")])  # 两条只判一条
        llm = SmartFakeLLM([final(partial), final(partial)])
        vs = run(verify_claims(llm, [claim("r:S:1"), claim("r:S:2")], checked_by="m"))
        assert len(llm.calls) == 2  # 部分响应也触发重试
        assert vs[0].label is SupportLabel.SUPPORTED and not vs[0].unverified
        assert vs[1].label is SupportLabel.PARTIAL and vs[1].unverified
        assert "未核验" in vs[1].rationale

    def test_unknown_claim_id_ignored(self):
        llm = SmartFakeLLM([final(verdicts_json([("ghost", "supported"), ("r:S:1", "partial")]))])
        vs = run(verify_claims(llm, [claim("r:S:1")], checked_by="m"))
        assert vs[0].label is SupportLabel.PARTIAL  # ghost 被忽略，只剩 :1 的判定


class TestDetectContradictions:
    def _vs(self, *ids):
        return [Verification(claim_id=i, label=SupportLabel.SUPPORTED,
                             rationale="r", checked_by="m", prompt_version="v") for i in ids]

    def test_groups_applied(self):
        llm = SmartFakeLLM([final(json.dumps(
            [{"claim_ids": ["r:S:1", "r:S:2"], "summary": "数据口径矛盾"}]))])
        mapping = run(detect_contradictions(
            llm, self._vs("r:S:1", "r:S:2", "r:S:3"),
            {c.claim_id: c for c in [claim("r:S:1"), claim("r:S:2"), claim("r:S:3")]}))
        assert mapping == {"r:S:1": "cg1", "r:S:2": "cg1"}
        assert "r:S:3" not in mapping

    def test_bad_output_returns_empty_not_crash(self):
        llm = SmartFakeLLM([final("没有矛盾（非 JSON）")])
        mapping = run(detect_contradictions(
            llm, self._vs("r:S:1", "r:S:2"), {}))
        assert mapping == {}

    def test_single_claim_short_circuits(self):
        mapping = run(detect_contradictions(
            SmartFakeLLM([]), self._vs("r:S:1"), {}))
        assert mapping == {} and SmartFakeLLM([]).calls == []
