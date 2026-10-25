"""Writer 测试：程序化 citation_map——编号绑定、重排、无效引用剥除。

核心断言：模型从头到尾接触不到"编号→claim_id"的绑定；
正文 [[n]] 由代码重写成连续 [n]，citation_map 与正文一致。
"""

import asyncio
import json
from datetime import date

from src.agent.agents.writer import (
    assemble_citable,
    build_report,
    build_writer_input,
    write_report,
)
from src.agent.schemas import (
    Claim,
    Finding,
    RunStatus,
    SupportLabel,
    Verification,
)
from tests.conftest import SmartFakeLLM, final


def run(coro):
    return asyncio.run(coro)


def mk_claim(cid: str, text: str = "论断") -> Claim:
    return Claim(
        claim_id=cid, sub_query_id=cid.split(":")[1], text=f"{text}{cid[-3:]}",
        quote=f"引文{cid[-3:]}", url=f"https://e.com/{cid[-3:]}",
        as_of_date=date(2026, 11, 2), extracted_by="m",
    )


def mk_finding(sub: str, cids: list[str]) -> Finding:
    return Finding(sub_query_id=sub, question=f"{sub} 问题", status=RunStatus.OK,
                   claims=[mk_claim(c) for c in cids])


def mk_verdicts(pairs: list[tuple[str, str]]) -> list[Verification]:
    return [Verification(claim_id=cid, label=SupportLabel(lb), rationale="r",
                         checked_by="m", prompt_version="v") for cid, lb in pairs]


class TestAssemble:
    def test_numbering_deterministic_unsupported_refused(self):
        findings = [mk_finding("SQ2", ["r:SQ2:002", "r:SQ2:001"]),
                    mk_finding("SQ1", ["r:SQ1:001"])]
        verdicts = mk_verdicts([
            ("r:SQ2:001", "supported"), ("r:SQ2:002", "unsupported"),
            ("r:SQ1:001", "partial"),
        ])
        numbered, refused = assemble_citable(findings, verdicts)
        assert [n for n, _ in numbered] == [1, 2]
        assert [c.claim_id for _, c in numbered] == ["r:SQ1:001", "r:SQ2:001"]
        assert [c.claim_id for c in refused] == ["r:SQ2:002"]

    def test_writer_input_carries_tokens_and_verdicts(self):
        numbered = [(1, mk_claim("r:SQ1:001"))]
        text = build_writer_input("问题", numbered,
                                  mk_verdicts([("r:SQ1:001", "partial")]))
        assert "[[1]]" in text and "partial" in text and "问题" in text


class TestBuildReport:
    def numbered(self):
        # 三条可引用：编号 1/2/3
        claims = [mk_claim("r:SQ1:001"), mk_claim("r:SQ1:002"), mk_claim("r:SQ2:001")]
        return [(i, c) for i, c in enumerate(claims, 1)], claims

    def test_programmatic_citation_map_and_renumbering(self):
        numbered, claims = self.numbered()
        # 模型引用了 1 和 3（跳过 2）；cites 里混入一个无效编号 99
        output = {
            "sections": [{"heading": "结论", "body": "论断甲……[[1]]。论断乙……[[3]]。"}],
            "cites": [1, 3, 99],
        }
        report = build_report("问题", numbered, [], output)
        # 重排为连续 1..2；无效 99 被剥除
        assert report.citation_map == {1: "r:SQ1:001", 2: "r:SQ2:001"}
        assert "[[1]]" not in report.markdown and "[99]" not in report.markdown
        assert "……[1]。" in report.markdown and "……[2]。" in report.markdown
        assert report.cited_claim_ids == ["r:SQ1:001", "r:SQ2:001"]

    def test_body_token_counts_even_if_cites_misses_it(self):
        """模型在正文用了 [[2]] 但忘了写进 cites → 并集兜底，引用不丢。"""
        numbered, _ = self.numbered()
        output = {"sections": [{"heading": "h", "body": "只用正文标记[[2]]"}], "cites": []}
        report = build_report("问题", numbered, [], output)
        assert report.citation_map == {1: "r:SQ1:002"}

    def test_unmatched_token_stripped(self):
        numbered, _ = self.numbered()
        output = {"sections": [{"heading": "h", "body": "引用[[2]]和无效[[7]]"}], "cites": [2]}
        report = build_report("问题", numbered, [], output)
        assert "[7]" not in report.markdown and "[[7]]" not in report.markdown
        assert "[1]" in report.markdown  # 2 重排为 1

    def test_refusals_record_unsupported(self):
        numbered, claims = self.numbered()
        refused = [mk_claim("r:SQ3:009")]
        report = build_report("问题", numbered, refused, {
            "sections": [{"heading": "h", "body": "……[[1]]"}], "cites": [1]})
        assert any("r:SQ3:009" in r for r in report.refusals)


class TestWriteReport:
    def test_no_citable_claims_yields_honest_empty(self):
        finding = mk_finding("SQ1", ["r:SQ1:001"])
        verdicts = mk_verdicts([("r:SQ1:001", "unsupported")])
        report = run(write_report(
            SmartFakeLLM([]), "问题", [finding], verdicts))
        assert report.citation_map == {}
        assert "未检索到" in report.markdown

    def test_repair_on_bad_json(self):
        finding = mk_finding("SQ1", ["r:SQ1:001"])
        verdicts = mk_verdicts([("r:SQ1:001", "supported")])
        good = json.dumps({"sections": [{"heading": "h", "body": "……[[1]]"}], "cites": [1]})
        llm = SmartFakeLLM([final("先说明思路……"), final(good)])
        report = run(write_report(llm, "问题", [finding], verdicts))
        assert report.citation_map == {1: "r:SQ1:001"}
