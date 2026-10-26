"""W0 契约测试：schema 必须拦得住"刷分样本"，指标口径必须可手算复现。

这批测试不联网、不调 API——它们是 CI 里永远稳定的那部分。
"""

from datetime import date

import pytest
from pydantic import ValidationError

from src.agent.schemas import (
    AgreementStats,
    Claim,
    ClaimJudgment,
    EvalQuestion,
    EvalRecord,
    Finding,
    KeyPointHit,
    ReportArtifact,
    ReportSection,
    REFERENCE_HEADING,
    RunStatus,
    SupportLabel,
    Verification,
    count_labels,
)


# ----------------------------- 构造工厂 ----------------------------- #
def claim(cid: str, sub: str = "SQ1", **over) -> Claim:
    base = dict(
        claim_id=cid,
        sub_query_id=sub,
        text=f"论断 {cid}",
        quote=f"原文引文 {cid}",
        url="https://example.com/a",
        as_of_date=date(2026, 11, 1),
        extracted_by="test-model",
    )
    base.update(over)
    return Claim(**base)


def finding(claims: list[Claim], sub: str = "SQ1") -> Finding:
    return Finding(
        sub_query_id=sub,
        question="子问题",
        status=RunStatus.OK,
        search_queries=["q"],
        pages_fetched=1,
        claims=claims,
    )


def verification(cid: str, label: SupportLabel) -> Verification:
    return Verification(
        claim_id=cid, label=label, rationale="ok",
        checked_by="verifier-model", prompt_version="p1",
    )


def record(**over) -> EvalRecord:
    base = dict(
        run_id="r-test",
        question_id="q001",
        config="A2",
        code_version="t1",
        prompt_version="p1",
        dataset_version="d1",
        question="测试问题",
        question_type="fact_check",
        difficulty=2,
        key_points=["要点一", "要点二", "要点三"],
        as_of_date=date(2026, 11, 1),
    )
    base.update(over)
    return EvalRecord(**base)


# ----------------------------- Claim / Finding ----------------------------- #
class TestClaim:
    def test_blank_quote_rejected(self):
        # quote 是事实链支点：空白引文等于不可验证
        with pytest.raises(ValidationError):
            claim("c1", quote="   ")


class TestFinding:
    def test_duplicate_claim_id_rejected(self):
        with pytest.raises(ValidationError, match="重复"):
            finding([claim("c1"), claim("c1")])

    def test_sub_query_mismatch_rejected(self):
        with pytest.raises(ValidationError, match="不一致"):
            finding([claim("c1", sub="SQ2")], sub="SQ1")


# ----------------------------- ReportArtifact ----------------------------- #
class TestReportArtifact:
    def test_same_claim_two_numbers_rejected(self):
        # 一对一约束：{1: c1, 2: c1} 必须被拦（W0 修复的死校验，回归保护）
        with pytest.raises(ValidationError, match="一对一"):
            ReportArtifact(markdown="x[1][2]", citation_map={1: "c1", 2: "c1"})

    def test_numbering_must_be_contiguous_from_1(self):
        with pytest.raises(ValidationError, match="连续递增"):
            ReportArtifact(markdown="x[2]", citation_map={2: "c1"})


# ----------------------------- KeyPointHit ----------------------------- #
class TestKeyPointHit:
    def test_watery_hit_rejected(self):
        # 判"已覆盖"却没有证据 claim = 覆盖率刷分漏洞，schema 层面堵死
        with pytest.raises(ValidationError, match="无证据"):
            KeyPointHit(key_point="要点", covered_by_retrieval=True, covered_in_report=True)

    def test_uncovered_hit_is_legal(self):
        KeyPointHit(key_point="要点", covered_by_retrieval=False, covered_in_report=False)


# ----------------------------- EvalRecord ----------------------------- #
class TestEvalRecordStructuralChecks:
    def test_ok_without_report_rejected(self):
        with pytest.raises(ValidationError, match="report"):
            record(status=RunStatus.OK)

    def test_degraded_without_report_is_legal(self):
        record(status=RunStatus.DEGRADED)

    def test_orphan_verification_rejected(self):
        # status 用 DEGRADED 绕开"OK 无 report"前置校验，专测孤儿 verification 分支
        with pytest.raises(ValidationError, match="不存在"):
            record(status=RunStatus.DEGRADED, verifications=[verification("ghost", SupportLabel.SUPPORTED)])

    def test_hits_length_mismatch_rejected(self):
        with pytest.raises(ValidationError, match="数量不一致"):
            record(status=RunStatus.DEGRADED, key_point_hits=[
                KeyPointHit(key_point="要点一", covered_by_retrieval=True, covered_in_report=True, evidence_claim_ids=["c1"]),
            ])


def _metric_fixture() -> EvalRecord:
    """3 claim：c1 supported / c2 partial / c3 unsupported。
    报告引用 c1、c2；judge 判 c1 supported、c2 partial；
    3 个要点命中 2 个（c3 未覆盖）。"""
    return record(
        findings=[finding([claim("c1"), claim("c2"), claim("c3")])],
        verifications=[
            verification("c1", SupportLabel.SUPPORTED),
            verification("c2", SupportLabel.PARTIAL),
            verification("c3", SupportLabel.UNSUPPORTED),
        ],
        report=ReportArtifact(
            markdown="结论一[1]。结论二（部分支持）[2]。",
            citation_map={1: "c1", 2: "c2"},
        ),
        judgments=[
            ClaimJudgment(claim_id="c1", judge_label=SupportLabel.SUPPORTED,
                          human_label=SupportLabel.SUPPORTED, sample_stratum="random"),
            ClaimJudgment(claim_id="c2", judge_label=SupportLabel.PARTIAL,
                          human_label=SupportLabel.UNSUPPORTED, sample_stratum="enriched_non_supported"),
        ],
        key_point_hits=[
            KeyPointHit(key_point="要点一", covered_by_retrieval=True, covered_in_report=True, evidence_claim_ids=["c1"]),
            KeyPointHit(key_point="要点二", covered_by_retrieval=True, covered_in_report=True, evidence_claim_ids=["c2"]),
            KeyPointHit(key_point="要点三", covered_by_retrieval=False, covered_in_report=False),
        ],
    )


class TestMetricMath:
    """口径手算：这是 W5 runner 之前对指标定义的最后防线。"""

    def test_faithfulness(self):
        # 分母 = 被引用且有判定的 claim（c1,c2）；分子 = supported（c1）→ 0.5
        assert _metric_fixture().faithfulness == 0.5

    def test_coverage(self):
        # 分母 = key_points 总数 3；分子 = 写进报告 2 → 0.6667
        assert _metric_fixture().coverage == pytest.approx(0.6667, abs=1e-3)

    def test_f1(self):
        assert _metric_fixture().f1 == pytest.approx(0.5714, abs=1e-3)

    def test_funnel(self):
        f = _metric_fixture().funnel()
        assert f.retrieval_hit_rate == pytest.approx(2 / 3, abs=1e-3)   # ① 检索命中 2/3
        assert f.claims_extracted == 3                                   # ② 抽取 3
        assert f.verifier_pass_rate == pytest.approx(2 / 3, abs=1e-3)   # ③ (sup+partial)/3，partial 计入通过
        assert f.claims_citable == 2                                     # c3 unsupported 不可引用
        assert f.claims_cited == 2
        assert f.writer_citation_rate == 1.0                             # ④ 2/2

    def test_no_report_means_none_not_zero(self):
        # 无引用/无要点时指标是 None（数据缺口），不是 0（坏成绩）——两种语义必须区分
        rec = record(status=RunStatus.DEGRADED)
        assert rec.faithfulness is None
        assert rec.coverage is None
        assert rec.f1 is None


class TestAgreementStats:
    def test_confusion_and_strata(self):
        stats = AgreementStats.from_judgments(_metric_fixture().judgments)
        assert stats.n == 2
        assert stats.observed_agreement == 0.5
        assert stats.confusion == {
            "supported": {"supported": 1},
            "unsupported": {"partial": 1},
        }
        assert stats.n_random == 1
        assert stats.n_enriched == 1

    def test_kappa_undefined_when_single_category(self):
        # 全部一致且只有一个类别：pe=1，kappa 无定义 → None（不是除零崩溃）
        js = [
            ClaimJudgment(claim_id=f"c{i}", judge_label=SupportLabel.SUPPORTED,
                          human_label=SupportLabel.SUPPORTED, sample_stratum="random")
            for i in range(3)
        ]
        stats = AgreementStats.from_judgments(js)
        assert stats.cohen_kappa is None
        assert stats.observed_agreement == 1.0


# ------------------ 交叉审计补漏（W1 复审产出） ------------------
# 以下用例来自"同源盲区"复审：代码与测试出自同一作者时，盲区会一致地漏。
# 每条都是"怎么作弊这个指标"的威胁模型推演，而不是照代码结构写用例。


class TestCrossFindingIntegrity:
    def test_cross_finding_duplicate_claim_id_rejected(self):
        # Finding 内查重挡不住跨组重复；set() 会把重复静默坍缩，总量失真
        f1 = finding([claim("c1", sub="SQ1")], sub="SQ1")
        f2 = finding([claim("c1", sub="SQ2")], sub="SQ2")
        with pytest.raises(ValidationError, match="跨 finding 重复"):
            record(status=RunStatus.DEGRADED, findings=[f1, f2])

    def test_duplicate_judgment_rejected(self):
        # 同一 claim 两条 judgment 会让忠实度分母重复计数
        with pytest.raises(ValidationError, match="多条 judgment"):
            record(
                status=RunStatus.DEGRADED,
                findings=[finding([claim("c1")])],
                judgments=[
                    ClaimJudgment(claim_id="c1", judge_label=SupportLabel.SUPPORTED),
                    ClaimJudgment(claim_id="c1", judge_label=SupportLabel.PARTIAL),
                ],
            )

    def test_ghost_judgment_rejected(self):
        with pytest.raises(ValidationError, match="judgment 指向不存在"):
            record(
                status=RunStatus.DEGRADED,
                judgments=[ClaimJudgment(claim_id="ghost", judge_label=SupportLabel.SUPPORTED)],
            )

    def test_ghost_evidence_rejected(self):
        # "证据非空"挡不住证据指向幻觉 claim_id：那是水词命中的变体
        hits = [
            KeyPointHit(
                key_point=k,
                covered_by_retrieval=(i == 0),
                covered_in_report=(i == 0),
                evidence_claim_ids=["ghost"] if i == 0 else [],
            )
            for i, k in enumerate(["要点一", "要点二", "要点三"])
        ]
        with pytest.raises(ValidationError, match="证据指向不存在"):
            record(status=RunStatus.DEGRADED, key_point_hits=hits)


class TestWordCountIntegrity:
    def test_computed_not_trusted_input(self):
        rep = ReportArtifact(markdown="中文五个字 and two words")
        assert rep.word_count == 8  # 5 个 CJK 字符 + 3 个非 CJK 词
        # 派生值不进存档：dump 里出现 word_count 才是缺陷
        assert "word_count" not in rep.model_dump()


class TestArchiveRoundtrip:
    """存档往返：model_dump → model_validate 必须无损。

    派生指标（忠实度/覆盖率/F1/篇幅）全部现算、不序列化——
    否则 extra="forbid" 会让存档读不回来，W5 落库当天翻车。
    """

    def test_full_record_roundtrip(self):
        rec = _metric_fixture()
        again = EvalRecord.model_validate(rec.model_dump())
        assert again == rec
        # 派生指标读档后重算，值不丢
        assert again.faithfulness == rec.faithfulness
        assert again.coverage == rec.coverage
        assert again.f1 == rec.f1
        assert again.report.word_count == rec.report.word_count

    @pytest.mark.parametrize(
        "obj",
        [
            EvalQuestion(
                question_id="e1", question="问题？", type="fact_check", difficulty=1,
                key_points=["a", "b", "c"], as_of_date=date(2026, 11, 1),
            ),
            Finding(sub_query_id="SQ1", question="子问题", status=RunStatus.OK, claims=[]),
            ClaimJudgment(claim_id="c1", judge_label=SupportLabel.SUPPORTED),
            KeyPointHit(key_point="要点", covered_by_retrieval=False, covered_in_report=False),
            ReportArtifact(markdown="报告正文"),
        ],
    )
    def test_each_model_roundtrip(self, obj):
        again = type(obj).model_validate(obj.model_dump())
        assert again == obj


# ------------------ 第二轮对抗审计（度量可信性） ------------------
# 这批洞不破坏结构、只破坏"数字能不能信"——写测试时想的是结构对不对，
# 想不到"能不能刷分"，所以同源盲区第一轮没抓到。


class TestMetricIntegrityAudit:
    def test_key_point_text_mismatch_rejected(self):
        # ③ 长度对齐挡不住文本错位：覆盖率数字对、归因全错
        hits = [
            KeyPointHit(key_point=k, covered_by_retrieval=False, covered_in_report=False)
            for k in ["要点一", "要点二", "要点X"]  # 与 key_points 同长但文本不符
        ]
        with pytest.raises(ValidationError, match="不对应"):
            record(status=RunStatus.DEGRADED, key_point_hits=hits)

    def test_citing_unsupported_claim_rejected(self):
        # ④ 设计规则早已定死 unsupported 不进报告；放行会让漏斗与
        # 忠实度对同一条引用给出矛盾处理
        with pytest.raises(ValidationError, match="unsupported"):
            record(
                findings=[finding([claim("c1"), claim("c2")])],
                verifications=[verification("c1", SupportLabel.UNSUPPORTED)],
                report=ReportArtifact(markdown="x[1]", citation_map={1: "c1"}),
            )

    def test_unjudged_cited_claims_reported_not_swallowed(self):
        # 2 条引用只判 1 条：忠实度报 1.0 是假满分，必须显式报缺口
        rec = record(
            findings=[finding([claim("c1"), claim("c2")])],
            report=ReportArtifact(markdown="a[1] b[2]", citation_map={1: "c1", 2: "c2"}),
            judgments=[ClaimJudgment(claim_id="c1", judge_label=SupportLabel.SUPPORTED)],
        )
        assert rec.faithfulness == 1.0  # 数字本身没错
        assert "judge_gap:1" in rec.quality_flags()  # 但缺口必须可见

    def test_agreement_strata_denominators_reconcile(self):
        # 层计数只算参与比对的样本：n == n_random + n_enriched
        js = [
            ClaimJudgment(claim_id="c1", judge_label=SupportLabel.SUPPORTED,
                          human_label=SupportLabel.SUPPORTED, sample_stratum="random"),
            ClaimJudgment(claim_id="c2", judge_label=SupportLabel.PARTIAL,
                          sample_stratum="enriched_non_supported"),   # 未抽到人工
            ClaimJudgment(claim_id="c3", judge_label=SupportLabel.PARTIAL,
                          sample_stratum="enriched_non_supported"),   # 未抽到人工
        ]
        stats = AgreementStats.from_judgments(js)
        assert stats.n == 1
        assert stats.n_random + stats.n_enriched == stats.n  # 修复前 1 < 1+2


# ----------------------------- 第七轮：未核验独立成桶 ----------------------------- #
class TestUnverifiedBucket:
    """核验失败不是"部分支持"。混进通过率 = 把数据缺口当证据。"""

    def build(self) -> EvalRecord:
        return record(
            findings=[finding([claim("c1"), claim("c2"), claim("c3")])],
            verifications=[
                verification("c1", SupportLabel.SUPPORTED),
                verification("c2", SupportLabel.PARTIAL),
                Verification(claim_id="c3", label=SupportLabel.PARTIAL, rationale="未核验",
                             checked_by="verifier-model", prompt_version="p1", unverified=True),
            ],
            report=ReportArtifact(markdown="a[1] b[2] c[3]",
                                  citation_map={1: "c1", 2: "c2", 3: "c3"}),
        )

    def test_unverified_not_counted_as_partial(self):
        f = self.build().funnel()
        assert f.claims_supported == 1
        assert f.claims_partial == 1        # 只剩真 partial
        assert f.claims_unverified == 1     # 未核验单独成桶
        assert f.claims_unsupported == 0

    def test_pass_rate_denominator_excludes_unverified(self):
        f = self.build().funnel()
        # (supported + partial) / extracted = 2/3，而不是把未核验也算进来的 3/3
        assert f.verifier_pass_rate == pytest.approx(2 / 3, abs=1e-4)

    def test_buckets_are_mutually_exclusive_and_total(self):
        f = self.build().funnel()
        assert (f.claims_supported + f.claims_partial
                + f.claims_unsupported + f.claims_unverified) == f.claims_extracted

    def test_unverified_still_citable(self):
        # 保守策略不变：未核验仍可引用（防 Verifier 误杀写空），只是不进通过率
        f = self.build().funnel()
        assert f.claims_citable == 3

    def test_count_labels_is_the_single_source_for_funnel_and_trace(self):
        """第七轮：四桶计数只有一个实现——funnel 与 trace 不可能再分叉。"""
        vers = self.build().verifications
        counts = count_labels(vers)
        assert counts == {"supported": 1, "partial": 1, "unsupported": 0, "unverified": 1}
        f = self.build().funnel()
        assert (f.claims_supported, f.claims_partial,
                f.claims_unverified, f.claims_unsupported) == (
            counts["supported"], counts["partial"],
            counts["unverified"], counts["unsupported"])


# ----------------------------- 第七轮：篇幅不含来源清单 ----------------------------- #
class TestWordCountScope:
    def test_reference_section_not_counted_in_word_count(self):
        prose = "这是一段正文内容。"
        with_refs = ReportArtifact(
            markdown=f"# Q\n\n## 结论\n\n{prose}\n\n## {REFERENCE_HEADING}\n\n[1] 长标题 — https://a.example/very/long/path/that/would/inflate",
            sections=[
                ReportSection(heading="结论", body=prose),
                ReportSection(heading=REFERENCE_HEADING,
                              body="[1] 长标题 — https://a.example/very/long/path/that/would/inflate"),
            ],
        )
        cjk = sum(1 for ch in prose if "\u4e00" <= ch <= "\u9fff")
        # 来源清单（含长 URL）一个字都不该计入篇幅
        assert with_refs.word_count == cjk

