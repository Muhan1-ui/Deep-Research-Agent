"""Writer 测试：程序化 citation_map——编号绑定、重排、无效引用剥除。

核心断言：模型从头到尾接触不到"编号→claim_id"的绑定；
正文 [[n]] 由代码重写成连续 [n]，citation_map 与正文一致。
"""

import asyncio
import json
import re
from datetime import date

from src.agent.agents.writer import (
    assemble_citable,
    build_report,
    build_writer_input,
    write_report,
)
from src.agent.schemas import (
    REFERENCE_HEADING,
    Claim,
    Finding,
    ReportArtifact,
    RunStatus,
    SupportLabel,
    Verification,
)
from tests.conftest import SmartFakeLLM, final


def run(coro):
    return asyncio.run(coro)


def mk_claim(cid: str, text: str = "论断") -> Claim:
    # quote/url 用完整 cid：不同 claim 必须是不同证据，(quote, url) 归并
    # 只应命中真正的重复（否则测试假件自己制造出"完全相同的论断"）
    return Claim(
        claim_id=cid, sub_query_id=cid.split(":")[1], text=f"{text}{cid}",
        quote=f"引文{cid}", url=f"https://e.com/{cid}",
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
        numbered, refused, deduped = assemble_citable(findings, verdicts)
        assert [n for n, _ in numbered] == [1, 2]
        assert [c.claim_id for _, c in numbered] == ["r:SQ1:001", "r:SQ2:001"]
        assert [c.claim_id for c in refused] == ["r:SQ2:002"]
        assert deduped == 0  # 三条引文互不相同，不该归并

    def test_writer_input_carries_tokens_and_verdicts(self):
        numbered = [(1, mk_claim("r:SQ1:001"))]
        text = build_writer_input("问题", numbered,
                                  mk_verdicts([("r:SQ1:001", "partial")]))
        assert "[[1]]" in text and "partial" in text and "问题" in text

    def test_unverified_renders_distinct_from_partial(self):
        """第六轮 P1-B：'未核验'不得伪装成'部分支持'——输入表里三态可见。"""
        numbered = [(1, mk_claim("r:SQ1:001")), (2, mk_claim("r:SQ1:002"))]
        vs = mk_verdicts([("r:SQ1:001", "partial"), ("r:SQ1:002", "partial")])
        vs[1].unverified = True
        text = build_writer_input("问题", numbered, vs)
        line1, line2 = text.strip().splitlines()[-2:]
        assert "partial" in line1 and "未核验" in line2
        assert "partial" not in line2  # 未核验行不得再出现 partial 字样


def mk_claim_at(cid: str, text: str, quote: str, url: str) -> Claim:
    return Claim(
        claim_id=cid, sub_query_id=cid.split(":")[1], text=text, quote=quote,
        url=url, as_of_date=date(2026, 11, 2), extracted_by="m",
    )


class TestDedupe:
    """同一份证据被多个子问题命中：编号前按 (quote, url) 归并。"""

    def test_identical_quote_url_merged_but_same_url_diff_quote_kept(self):
        shared_quote = "MCP 采用客户端-服务器架构。"
        shared_url = "https://e.com/shared"
        c1 = mk_claim_at("r:SQ1:001", "论断一", shared_quote, shared_url)
        c2 = mk_claim_at("r:SQ1:002", "论断二", shared_quote, shared_url)  # 完全重复
        c3 = mk_claim_at("r:SQ2:001", "论断三", "MCP 使用 JSON-RPC 2.0。", shared_url)
        findings = [
            Finding(sub_query_id="SQ1", question="q1", status=RunStatus.OK,
                    claims=[c1, c2]),
            Finding(sub_query_id="SQ2", question="q2", status=RunStatus.OK,
                    claims=[c3]),
        ]
        verdicts = mk_verdicts([
            ("r:SQ1:001", "supported"), ("r:SQ1:002", "supported"),
            ("r:SQ2:001", "supported"),
        ])
        numbered, refused, deduped = assemble_citable(findings, verdicts)

        assert deduped == 1                                   # 合并掉一条
        # 保留首次出现（claim_id 最小），重复项不分配新编号
        assert [c.claim_id for _, c in numbered] == ["r:SQ1:001", "r:SQ2:001"]

        # 像 writer 一样按输入表逐条写正文：重复项根本不在输入表里
        body = "\n".join(f"{c.text}[[{n}]]" for n, c in numbered)
        report = build_report("问题", numbered, refused,
                              {"sections": [{"heading": "结论", "body": body}]},
                              deduped=deduped)
        sec = next(s for s in report.sections if s.heading == "结论")
        lines = [ln for ln in sec.body.splitlines() if ln.strip()]

        assert len(lines) == 2                                # 正文只剩 2 条
        assert not any("论断二" in ln for ln in lines)        # 完全重复的被归并
        assert any("论断三" in ln for ln in lines)            # 同 URL 不同 quote 保留
        nums = [int(n) for n in re.findall(r"\[(\d+)\]", sec.body)]
        assert nums == [1, 2]                                 # 编号连续
        assert set(nums) == set(report.citation_map)          # 正文与 map 一一对应
        assert report.citation_map == {1: "r:SQ1:001", 2: "r:SQ2:001"}
        assert report.deduped == 1


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

    def test_cites_without_body_not_in_map(self):
        """第六轮 P2-D：正文 token 是唯一成员资格——只声明不展示的编号
        不进 citation_map（忠实度分母不含未展示引用）。"""
        numbered, _ = self.numbered()
        output = {"sections": [{"heading": "h", "body": "只写[[1]]"}],
                  "cites": [1, 2, 3]}  # 声明 3 个，正文只有 1 个
        report = build_report("问题", numbered, [], output)
        assert report.citation_map == {1: "r:SQ1:001"}
        assert report.cited_claim_ids == ["r:SQ1:001"]

    def test_references_section_appended(self):
        """第六轮 P1-A：报告自带来源清单（按 URL 分组），读者可点开核对。"""
        numbered, _ = self.numbered()  # 三条 claim 分属三个 URL
        output = {"sections": [{"heading": "结论", "body": "甲[[1]] 乙[[2]]"}]}
        report = build_report("问题", numbered, [], output)
        assert "## 来源" in report.markdown
        for _, c in numbered[:2]:
            assert c.url in report.markdown  # URL 可见
        assert "[1]" in report.markdown and "[2]" in report.markdown
        # 分组行形如 "[1] ... — url"
        assert " — https://" in report.markdown
        assert any(s.heading == "来源" for s in report.sections)

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


class TestWordCountExcludesReferences:
    def numbered(self):
        claims = [mk_claim("r:SQ1:001"), mk_claim("r:SQ1:002"), mk_claim("r:SQ2:001")]
        return [(i, c) for i, c in enumerate(claims, 1)]

    def test_references_section_not_counted(self):
        """第七轮 P1：篇幅只由正文决定——来源清单随引用数增长，不能算进
        '报告是否变薄'，否则消融里 A2 更长可能只是引用更多。"""
        numbered = self.numbered()
        report = build_report("问题", numbered, [], {
            "sections": [{"heading": "结论", "body": "甲[[1]]乙[[2]]丙[[3]]"}]})
        ref = next(s for s in report.sections if s.heading == REFERENCE_HEADING)
        assert ref.body  # 确实生成了来源行
        prose_only = ReportArtifact(
            markdown="", citation_map=report.citation_map,
            sections=[s for s in report.sections if s.heading != REFERENCE_HEADING])
        assert report.word_count == prose_only.word_count


class TestBareCitationRepair:
    """第七轮 P2：cites 退出后正文 token 是唯一成员资格，单方括号 = 零引用。"""

    BARE = json.dumps({"sections": [{"heading": "结论", "body": "甲……[1]。"}]},
                      ensure_ascii=False)
    TOKEN = json.dumps({"sections": [{"heading": "结论", "body": "甲……[[1]]。"}]},
                       ensure_ascii=False)

    def test_bare_brackets_trigger_retry_then_accept(self):
        finding = mk_finding("SQ1", ["r:SQ1:001"])
        verdicts = mk_verdicts([("r:SQ1:001", "supported")])
        llm = SmartFakeLLM([final(self.BARE), final(self.TOKEN)])
        report = run(write_report(llm, "问题", [finding], verdicts))
        assert len(llm.calls) == 2                    # 确实重试了一次
        assert report.citation_map == {1: "r:SQ1:001"}
        # 第二次请求里必须带上格式修正提示
        assert any("[[n]]" in str(m.get("content", "")) for m in llm.calls[1])

    def test_bare_brackets_persisting_keeps_text_but_no_citations(self):
        finding = mk_finding("SQ1", ["r:SQ1:001"])
        verdicts = mk_verdicts([("r:SQ1:001", "supported")])
        llm = SmartFakeLLM([final(self.BARE), final(self.BARE)])
        report = run(write_report(llm, "问题", [finding], verdicts))
        assert report.citation_map == {}              # 绝不伪称有可校验引用
        assert "甲" in report.markdown                # 正文仍然保留
        assert any("单方括号" in r for r in report.refusals)

    def test_choices_bracket_is_not_treated_as_citation(self):
        """正文含 choices[0] 但另有 [[1]] → 不该误判为格式错误。"""
        body = "流式数据在 choices[0].delta.content，见论断[[1]]。"
        finding = mk_finding("SQ1", ["r:SQ1:001"])
        verdicts = mk_verdicts([("r:SQ1:001", "supported")])
        llm = SmartFakeLLM([final(json.dumps(
            {"sections": [{"heading": "h", "body": body}]}, ensure_ascii=False))])
        report = run(write_report(llm, "问题", [finding], verdicts))
        assert len(llm.calls) == 1                    # 一次通过，无多余重试
        assert report.citation_map == {1: "r:SQ1:001"}
        assert "choices[0]" in report.markdown
