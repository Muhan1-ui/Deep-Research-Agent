"""W0 数据契约层：claim / findings / eval-record。

为什么 W0 只做这件事、不写业务代码
==================================
1. **指标口径决定 schema，schema 决定下游代码。** 忠实度、覆盖率、四层漏斗
   各自"分子是谁、分母是谁"一旦含糊，W5 写完 runner 才发现口径不对，
   W3/W4 的 prompt 和落库结构全要返工。
2. **跨模块的 join key 必须先固定。** `claim_id` 是
   researcher → verifier → writer → judge → 人工抽检 的唯一线索；
   没有它，四层漏斗最后拼不起来，只能各算各的。
3. **这里只放数据形状，不放业务流程。** 取数、判分、聚合分别属于
   orchestrator 和 eval.runner；schema 一旦开始长方法，就会变成上帝类。

指标口径（写死在本文件，runner 里不允许另起一套）
================================================
忠实度 faithfulness
    分子 = 被报告引用、且 judge 判 ``supported`` 的 claim 数
    分母 = 被报告引用、且有 judge 判定的 claim 数
    ⚠ 分母是"被引用的"而非"抽取的"。这意味着 **少写内容就能把比值做高**，
      所以它必须与覆盖率成对报告 —— 单报忠实度 +10pp 是伪结论。

覆盖率 coverage
    分子 = ``covered_in_report=True`` 的 key_point 数
    分母 = 该题 key_point 总数
    ⚠ 覆盖率单独同样可刷（把要点写一遍水词也能"命中"）。因此 KeyPointHit
      强制要求：命中必须给出 ``evidence_claim_ids``，且该 claim 被 judge
      判为 supported/partial。空证据的"命中"直接报错。

F1
    harmonic_mean(faithfulness, coverage)。两者分母不同、统计意义不同，
    F1 只作"不能靠牺牲一方换另一方"的汇总提示，报告里三者必须并列。

四层漏斗 funnel
    ① 检索命中  covered_by_retrieval / len(key_points)
    ② 抽取数量  len(claims)（绝对数：它是后面所有环节的天花板）
    ③ Verifier 通过  (supported + partial) / len(claims)
       ⚠ 口径选择：partial 在 Writer 阶段会保留（带不确定性标注），所以
         算"通过"。若只算 supported，会把 Verifier 的**保守程度**读成
         "通过率下降"。unsupported 单独给字段。
    ④ Writer 引用  len(distinct cited claim) / len(可引用 claim)
    逐层相除即可定位短板：检索丢 → 搜索/query；抽取少 → 切片/prompt；
    通过率低 → Verifier 或证据质量；引用率低 → Writer。

⚠ **漏斗是"推导视图"，不是落库字段。** 落库的漏斗和重算的漏斗一旦不一致，
  就是评测项目最常见的自欺。唯一真源是 claim 列表 + 判定 + 报告引用表。
"""

from __future__ import annotations

import math
from datetime import date, datetime
from enum import Enum
from typing import Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    field_validator,
    model_validator,
)

# 质量闸门：只用于把失败案例归类（报告里的"未达标原因分布"），
# 不用来篡改指标本身。阈值写在这里，避免散落在 runner 里。
MIN_COVERAGE = 0.60
MIN_FAITHFULNESS = 0.85

# 来源清单的章节名：writer 生成它、word_count 排除它——两侧共用这一个常量，
# 避免"改了一边忘了另一边"（第七轮审计 P1）。
REFERENCE_HEADING = "来源"


# --------------------------------------------------------------------------- #
# 基类与枚举
# --------------------------------------------------------------------------- #
class Contract(BaseModel):
    """全部契约模型的基类：未声明字段一律报错。

    契约要比业务代码更"硬"。一旦各处能随手塞字段，W3 的落库、W5 的打分、
    W6 的消融就会各自长出私有字段，最后拼不到一张表里。
    """

    model_config = ConfigDict(extra="forbid", populate_by_name=True)


class SupportLabel(str, Enum):
    """Verifier / judge / 人工共用的三分类。

    三档而不是两档，是因为校招场景里"部分支持"极其常见（quote 支持论断的
    一半），强行二分会让 Verifier 和 judge 都系统性偏向某一侧。
    """

    SUPPORTED = "supported"
    PARTIAL = "partial"
    UNSUPPORTED = "unsupported"

    @property
    def citable(self) -> bool:
        """能否进报告。partial 保留但必须标注不确定性（防 Verifier 误杀写空）。"""
        return self is not SupportLabel.UNSUPPORTED


class SourceQuality(str, Enum):
    """来源可信度分级。

    引用忠实 ≠ 事实正确：quote 被正确引用，但 quote 本身可能是 SEO 垃圾。
    没有这一层，"忠实度 95%"在面试官眼里仍可能是"准确引用了一堆垃圾"。
    """

    OFFICIAL = "official"      # 官方文档 / 发布说明 / 标准
    PAPER = "paper"            # 论文 / 技术报告
    PRIMARY = "primary"        # 一手数据：仓库、变更日志、原始 issue
    COMMUNITY = "community"    # 社区问答 / 论坛
    BLOG = "blog"              # 个人博客 / 媒体
    UNKNOWN = "unknown"

    @property
    def rank(self) -> int:
        """数值越小越可信，供 writer 排序与 report 的"来源质量分布"统计。"""
        return _SOURCE_RANK[self]


_SOURCE_RANK = {
    SourceQuality.OFFICIAL: 0,
    SourceQuality.PAPER: 1,
    SourceQuality.PRIMARY: 2,
    SourceQuality.COMMUNITY: 3,
    SourceQuality.BLOG: 4,
    SourceQuality.UNKNOWN: 5,
}


class QuestionType(str, Enum):
    """评测集的五类题。分桶报指标，才能说清"哪类题是我的短板"。"""

    FACT_CHECK = "fact_check"
    COMPARISON = "comparison"
    TIMELINE = "timeline"
    LANDSCAPE = "landscape"
    CONTROVERSY = "controversy"


class AblationConfig(str, Enum):
    """消融三组。A0 = W1 的 CLI 单 Agent 模式加开关，近乎免费。"""

    A0 = "A0"  # 单 Agent 一次生成（baseline）
    A1 = "A1"  # 多 Agent，无 Verifier
    A2 = "A2"  # 完整系统


class RunStatus(str, Enum):
    """任务/子任务状态。

    DEGRADED 与 FAILED 必须分开：前者"有产物但缺件"仍计入样本，
    后者"无产物"才从分母剔除。混在一起，成功率会随降级策略任意漂移。
    """

    OK = "ok"
    DEGRADED = "degraded"
    FAILED = "failed"


# --------------------------------------------------------------------------- #
# 一、claim / findings（生产链路）
# --------------------------------------------------------------------------- #
class Claim(Contract):
    """一条可验证的论断 —— 整个系统的原子单位。

    `quote` 字段是 v1 就定下的关键决策：researcher 传原文而非摘要，
    否则 Verifier 无据可查，忠实度也无法机器评估。
    """

    claim_id: str = Field(
        description="全局唯一，建议 `{run_id}:{sub_query_id}:{序号}`。"
        "researcher/verifier/writer/judge/人工抽检都靠它对齐，"
        "改动这个 id 的生成规则等于让所有历史记录失效。"
    )
    sub_query_id: str = Field(description="来自哪个子问题 → 漏斗归因的下钻维度。")

    text: str = Field(description="要写进报告的那句话（论断本身）。")
    quote: str = Field(
        description="支撑该论断的原文片段，逐字复制、不改写。"
        "这是可验证性的支点：拿掉它，Verifier 只能评价'像不像'。"
    )
    url: str = Field(description="quote 的来源 URL，报告 References 直接用。")
    page_title: str | None = Field(default=None, description="便于人读 trace 与报告参考列表。")

    source_quality: SourceQuality = Field(
        default=SourceQuality.UNKNOWN,
        description="来源分级。默认 UNKNOWN 而不是猜一个，避免虚假自信。",
    )
    as_of_date: date = Field(
        description="该网页内容对应的信息时点（发布/更新日；取不到则记抓取日并标 UNKNOWN）。"
        "评测集会随时间腐烂，没有这个字段，1 月复现不出 11 月的数字。"
    )
    fetched_at: datetime | None = Field(default=None, description="抓取时刻，用于缓存命中与时效判断。")

    quote_locator: str | None = Field(
        default=None,
        description="`{落盘原文路径}#{起始}-{结束}`。原文落盘不进上下文，"
        "但人工抽检和面试官追问时要能一键回查原句——这是'可追溯'的最后一公里。",
    )
    extracted_by: str = Field(
        default="unknown",
        description="抽取该 claim 的模型 id。便宜模型抽出的 claim 质量差是常见瓶颈，"
        "不记这个字段就无法在 W7 归因到'抽取'层。",
    )

    @field_validator("text", "quote", "url")
    @classmethod
    def _non_empty(cls, v: str) -> str:
        if not v or not v.strip():
            raise ValueError("claim 的 text/quote/url 不允许为空：空 quote 等于不可验证")
        return v.strip()


class Finding(Contract):
    """一个子问题的检索与抽取结果（researcher 的产出）。

    刻意不含 verifier 结论：验证是独立阶段，混进来会让"A1 无 Verifier"
    这组消融没法复用同一份 Finding。
    """

    sub_query_id: str
    question: str = Field(description="planner 生成的子问题原文，报告与归因都按它对齐。")
    status: RunStatus = Field(description="该子任务成败；DEGRADED 表示检索到了但抽取不全。")
    search_queries: list[str] = Field(
        default_factory=list,
        description="实际发出的 query（planner 的 hints 常被改写）。"
        "检索层出问题时要靠它复盘，不记就只能怪'模型不行'。",
    )
    pages_fetched: int = Field(default=0, description="抓取页数 → 成本与耗尽情况。")
    tokens_in: int = Field(default=0, description="该子任务 LLM 输入 token（含修复轮）——W5/W7 成本指标的数据源。")
    tokens_out: int = Field(default=0, description="该子任务 LLM 输出 token（含修复轮）。")
    claims: list[Claim] = Field(default_factory=list)
    error: str | None = Field(default=None, description="降级/失败原因，直接进 trace 与失败归因。")
    elapsed_s: float | None = Field(default=None, description="并行调度时找长尾子任务用。")

    @model_validator(mode="after")
    def _check_claims(self) -> Finding:
        ids = [c.claim_id for c in self.claims]
        if len(ids) != len(set(ids)):
            raise ValueError(f"claim_id 在 {self.sub_query_id} 内重复：下游 join 会错配")
        bad = [c.claim_id for c in self.claims if c.sub_query_id != self.sub_query_id]
        if bad:
            raise ValueError(f"claim.sub_query_id 与所属 finding 不一致：{bad[:3]}")
        return self


class Verification(Contract):
    """Verifier 对单条 claim 的核对结论。"""

    claim_id: str
    label: SupportLabel
    rationale: str = Field(
        description="一句话理由，**强制短**。长 rationale 的 LLM 判定更容易自圆其说，"
        "而且人工抽检时读不动——它只是给人和 judge 的提示，不是证据。"
    )
    unverified: bool = Field(
        default=False,
        description="True=核验服务失败、从未真正核对（保守记 partial 保持可引用）。"
        "渲染与统计必须与真 partial 区分：报告标注「未核验」；通过率/忠实度"
        "把它当数据缺口而非 partial——'不知道'不等于'知道一点'（第六轮审计 P1-B）。",
    )
    contradiction_group: str | None = Field(
        default=None,
        description="同一 group 内的 claim 互斥（跨源矛盾）。矛盾检测的产物，"
        "也是报告里'存在争议'章节的依据。",
    )
    checked_by: str = Field(description="Verifier 模型 id。")
    prompt_version: str = Field(description="prompt 版本号，和 code_version 一起构成复现三元组。")

    @field_validator("rationale")
    @classmethod
    def _short(cls, v: str) -> str:
        v = v.strip()
        if len(v) > 200:
            raise ValueError("rationale 超过 200 字：它应该是理由，不是复述原文")
        return v


def count_labels(verifications: list[Verification]) -> dict[str, int]:
    """三分类 + 未核验的四桶计数 —— funnel() 与 trace/CLI 共用的唯一实现。

    第七轮的教训：字段加了、派生层没跟上，两处口径就会悄悄分叉
    （funnel 排除了未核验，trace 却还在按 label 数）。把计数收敛到一个函数，
    「谁读它」就和「谁写它」不可能再错位。
    """
    counts = {"supported": 0, "partial": 0, "unsupported": 0, "unverified": 0}
    for v in verifications:
        if v.unverified:
            counts["unverified"] += 1
        else:
            counts[v.label.value] += 1
    return counts


# --------------------------------------------------------------------------- #
# 二、报告产物
# --------------------------------------------------------------------------- #
class ReportSection(Contract):
    heading: str
    body: str = Field(description="含内联 [n] 引用标记。引用编号与 citation_map 对齐。")


class ReportArtifact(Contract):
    """Writer 产出。引用关系单独成表，而不是把 [n] 直接塞进 claim 上。

    理由：引用编号是**报告内**的局部编号，同一条 claim 在两次报告里编号不同，
    写回 claim 会让消融对比时数字互相污染。
    """

    markdown: str
    sections: list[ReportSection] = Field(default_factory=list)
    citation_map: dict[int, str] = Field(
        default_factory=dict,
        description="{报告内引用编号 n: claim_id}。忠实度的分母从这张表反查，"
        "所以它必须由 writer 显式输出，不能事后从正则里猜。"
        "唯一真源约束（W4）：编号由程序分配生成，正文里的 [n] 标记不作数——"
        "任何正则扫正文统计引用都会误伤（如代码字段 choices[0] 会被当成 [0]），"
        "统计引用只从本表读。",
    )
    refusals: list[str] = Field(
        default_factory=list,
        description="writer 明确拒绝写入的内容及原因（如证据不足）。"
        "这是覆盖率下降的正当理由，也是'partial 保留'策略的旁证。",
    )

    @property
    def word_count(self) -> int:
        """报告篇幅：CJK 字符逐个计 + 其余按空白分词。

        排除「来源」章节（第七轮审计 P1）：来源清单的行数随引用数增长，
        若计入篇幅，消融表里 "A2 写得更长" 可能只是 "A2 引用更多"——
        恰好污染 word_count 用来防的那件事（"忠实度高是不是因为写得少"）。
        sections 优先；只有 markdown 的旧记录回退整段计数。

        现算而非 Writer 自报——word_count 若是可信输入，"A2 写得少"
        的防线就成了自证。派生值不序列化，与"漏斗是推导视图"同一原则。
        """
        text = (
            "\n".join(s.body for s in self.sections if s.heading != REFERENCE_HEADING)
            if self.sections
            else self.markdown
        )
        cjk = sum(1 for ch in text if "\u4e00" <= ch <= "\u9fff")
        latin_words = len(
            [w for w in text.split() if not any("\u4e00" <= ch <= "\u9fff" for ch in w)]
        )
        return cjk + latin_words

    @property
    def cited_claim_ids(self) -> list[str]:
        """被引用的 distinct claim，保持首次出现顺序（便于稳定复现）。"""
        seen: dict[str, None] = {}
        for _, cid in sorted(self.citation_map.items()):
            seen.setdefault(cid, None)
        return list(seen)

    @model_validator(mode="after")
    def _check_citation_map(self) -> ReportArtifact:
        if len(set(self.citation_map.values())) < len(self.citation_map):
            raise ValueError("citation_map 里有 claim 被分配了多个引用编号：编号必须一对一")
        nums = sorted(self.citation_map)
        if nums and nums != list(range(1, len(nums) + 1)):
            raise ValueError(f"引用编号必须从 1 连续递增，实际 {nums[:5]}...")
        return self


# --------------------------------------------------------------------------- #
# 三、评测记录（eval record）
# --------------------------------------------------------------------------- #
class ClaimJudgment(Contract):
    """单条 claim 的 judge 判定（+ 可选的人工判定）。

    judge 与 human 放在同一个对象里，是为了让"一致率 / 混淆矩阵"不需要
    二次 join——人工抽检 30~50 条时，join 出错是最常见的返工来源。
    """

    claim_id: str
    judge_label: SupportLabel | None = Field(
        default=None, description="None 表示该 claim 未被 judge 覆盖（数据缺口，需在报告里显式报数）。"
    )
    judge_model: str | None = Field(default=None, description="具体模型 id（含日期后缀）。")
    human_label: SupportLabel | None = Field(default=None, description="人工抽检标签；非抽样样本为 None。")
    sample_stratum: Literal["random", "enriched_non_supported"] | None = Field(
        default=None,
        description="抽样层。非 supported 样本被故意富集，"
        "不记录层标签就没法解释'为什么一致率比随机抽样低'——这是面试必被问的点。",
    )
    note: str | None = Field(default=None, description="人工判定的分歧说明，one-liner。")

    @property
    def agreed(self) -> bool | None:
        if self.judge_label is None or self.human_label is None:
            return None
        return self.judge_label is self.human_label


class KeyPointHit(Contract):
    """一条参考要点是否被触达 / 被写进报告。

    检索命中与报告覆盖分开记，是为了让漏斗①和覆盖率用同一份数据源——
    分两处记，就一定会出现"检索命中率 90% 但覆盖率 40%"却查不出在哪断掉。

    已定口径（W5，关闭第二轮审计开放项 ②）：evidence_claim_ids 必须是
    **被报告引用**的 claim（⊆ citation_map）——覆盖率与忠实度从此共享
    同一证据集。由 EvalRecord._consistency 强制，judge/runner 在上游过滤。
    """

    key_point: str
    covered_by_retrieval: bool = Field(description="检索/抽取阶段是否触达（漏斗①分子）。")
    covered_in_report: bool = Field(description="报告中是否真正写到（覆盖率分子）。")
    evidence_claim_ids: list[str] = Field(
        default_factory=list, description="支撑该要点的 claim。空列表 + covered_in_report=True 视为刷分。"
    )

    @model_validator(mode="after")
    def _no_watery_hit(self) -> KeyPointHit:
        if self.covered_in_report and not self.evidence_claim_ids:
            raise ValueError(
                f"要点被判'已覆盖'却无证据 claim：{self.key_point!r}。"
                "这正是覆盖率可被水词刷分的漏洞，schema 层面直接堵掉。"
            )
        return self


class FunnelMetrics(Contract):
    """四层漏斗（推导视图，由 EvalRecord.funnel() 产出，不单独落库）。"""

    retrieval_hits: int = Field(description="① 检索命中的 key_point 数")
    retrieval_total: int = Field(description="① 分母：key_point 总数")
    claims_extracted: int = Field(description="② claims 抽取数（绝对数，是后续一切的天花板）")
    claims_supported: int = Field(default=0, description="③ supported 数")
    claims_partial: int = Field(default=0, description="③ 真 partial 数（计入通过；未核验另计）")
    claims_unverified: int = Field(
        default=0,
        description="③ 未核验数：核验服务失败/漏判，从未真正核对过。单独成桶、"
        "不计入通过率——『不知道』不等于『知道一点』（第七轮审计 P1）。"
        "恒等式：supported + partial + unsupported + unverified == 核验条数",
    )
    claims_unsupported: int = Field(default=0, description="③ unsupported 数（被过滤）")
    claims_citable: int = Field(default=0, description="④ 分母：可引用 claim 数 = supported + partial")
    claims_cited: int = Field(default=0, description="④ 分子：报告实际引用的 distinct claim 数")

    @staticmethod
    def _ratio(num: int, den: int) -> float:
        """分母为 0 一律返回 0.0 而不是抛错：评测要能容忍残缺样本并继续批跑。"""
        return 0.0 if den <= 0 else round(num / den, 4)

    @property
    def retrieval_hit_rate(self) -> float:
        return self._ratio(self.retrieval_hits, self.retrieval_total)

    @property
    def verifier_pass_rate(self) -> float:
        return self._ratio(self.claims_supported + self.claims_partial, self.claims_extracted)

    @property
    def writer_citation_rate(self) -> float:
        return self._ratio(self.claims_cited, self.claims_citable)

    def describe(self) -> str:
        pending = f"｜未核验 {self.claims_unverified}" if self.claims_unverified else ""
        return (
            f"检索 {self.retrieval_hits}/{self.retrieval_total} ({self.retrieval_hit_rate:.0%}) → "
            f"抽取 {self.claims_extracted} → 通过 {self.claims_supported + self.claims_partial} "
            f"({self.verifier_pass_rate:.0%}){pending} → 引用 {self.claims_cited}/{self.claims_citable} "
            f"({self.writer_citation_rate:.0%})"
        )


# --------------------------------------------------------------------------- #
# 四、评测集题目
# --------------------------------------------------------------------------- #
class EvalQuestion(Contract):
    """评测集单题。既有 `as_of_date` 也有 `key_points`：前者防腐烂，后者算覆盖率。"""

    question_id: str
    question: str
    question_type: QuestionType = Field(alias="type", description="yaml 里写 `type:`，与数据集格式一致。")
    difficulty: int = Field(ge=1, le=5, description="1~5。分层报指标比总量数字更有说服力。")
    key_points: list[str] = Field(min_length=3, description="5~8 个为宜；少于 3 个覆盖率没有分辨率。")
    as_of_date: date = Field(description="该题的时效锚点。答题与复现都以它为准。")
    notes: str | None = Field(default=None, description="出题备注：易错点、可接受的等价答案等。")


# --------------------------------------------------------------------------- #
# 五、一次评测运行
# --------------------------------------------------------------------------- #
class EvalRecord(Contract):
    """一次 (题目 × 消融配置) 的完整记录 —— 报告里所有数字的唯一来源。

    `code_version / prompt_version / model_snapshots` 三者合起来构成复现三元组。
    没有它，1 月复现不出 11 月的数字，"可复现"就只是 README 上的一句话。
    """

    # —— 身份 ——
    run_id: str
    question_id: str
    config: AblationConfig

    # —— 复现三元组 ——
    code_version: str = Field(description="git sha（含 -dirty 标记）。")
    prompt_version: str = Field(description="prompt 包版本号；改一个字就要 +1。")
    dataset_version: str = Field(description="评测集版本；题目增删都要 bump。")
    model_snapshots: dict[str, str] = Field(
        default_factory=dict,
        description="{角色: 模型 id}，如 researcher=deepseek-chat、planner=gpt-4.1。"
        "只写'用了 DeepSeek'不算记录，模型名不带日期后缀就无法复现。",
    )
    judge_model: str = Field(default="unknown", description="评分模型 id。")
    producer_vendors: list[str] = Field(
        default_factory=list, description="生产链路用到的厂商（如 ['deepseek']）。用于同源偏差自检。"
    )
    judge_vendor: str = Field(default="unknown", description="judge 厂商。理想情况应与 producer 不同。")

    # —— 题面 ——
    question: str
    question_type: QuestionType
    difficulty: int = Field(ge=1, le=5)
    key_points: list[str] = Field(default_factory=list)
    as_of_date: date

    # —— 产物 ——
    findings: list[Finding] = Field(default_factory=list)
    verifications: list[Verification] = Field(default_factory=list)
    report: ReportArtifact | None = Field(default=None, description="降级/失败时可能为 None，这是合法状态。")
    judgments: list[ClaimJudgment] = Field(default_factory=list)
    key_point_hits: list[KeyPointHit] = Field(default_factory=list)

    # —— 运行事实 ——
    status: RunStatus = RunStatus.OK
    error: str | None = None
    total_tokens_in: int = 0
    total_tokens_out: int = 0
    judge_tokens_in: int = Field(
        default=0,
        description="judge 侧 token（覆盖判定 payload 含整份报告，不是小数目）——"
        "与 total_tokens_* 分开记：前者是生产成本、后者是评测成本。",
    )
    judge_tokens_out: int = 0
    judge_notes: list[str] = Field(
        default_factory=list,
        description="judge 层异常备注（如 cover_parse_failed：覆盖率解析重试耗尽）——"
        "数据缺口显式报数，不与'真的没写'混淆（第九轮审计）。",
    )
    cost_cny: float = 0.0
    wall_seconds: float = 0.0
    search_calls: int = 0
    fetch_calls: int = 0
    trace_path: str | None = Field(default=None, description="JSONL trace 路径；正文不入 trace，只留元数据。")

    # ------------------------------ 派生指标 ------------------------------ #
    def judged_cited(self) -> list[ClaimJudgment]:
        """被报告引用、且已有 judge 判定的 claim —— 忠实度的唯一分母。"""
        if self.report is None:
            return []
        cited = set(self.report.cited_claim_ids)
        return [j for j in self.judgments if j.claim_id in cited and j.judge_label is not None]

    @property
    def faithfulness(self) -> float | None:
        """引用忠实度 = 引用中判 supported 的比例。无引用时返回 None（不是 0）。"""
        js = self.judged_cited()
        if not js:
            return None
        return round(sum(j.judge_label is SupportLabel.SUPPORTED for j in js) / len(js), 4)

    @property
    def coverage(self) -> float | None:
        """要点覆盖率。分母用 key_points 总数，不用 key_point_hits 长度（防止漏记要点被自动豁免）。"""
        if not self.key_points:
            return None
        if not self.key_point_hits:
            return None
        return round(sum(h.covered_in_report for h in self.key_point_hits) / len(self.key_points), 4)

    @property
    def f1(self) -> float | None:
        """忠实度与覆盖率的调和平均。任一为 None 则为 None（不拿 0 顶替）。"""
        p, r = self.faithfulness, self.coverage
        if p is None or r is None or (p + r) == 0:
            return None
        return round(2 * p * r / (p + r), 4)

    def funnel(self) -> FunnelMetrics:
        """从唯一真源推导四层漏斗。不落库，避免两处数字打架。

        未核验的 claim 单独成桶、不进 supported/partial（第七轮审计 P1）：
        它们的 label 出于保守被记成 partial（保持可引用），但"没有核对"
        不是"部分支持"。混进通过率，等于把数据缺口算成证据。
        """
        claims = [c for f in self.findings for c in f.claims]
        counts = count_labels(self.verifications)
        citable_ids = {
            c.claim_id for c in claims if self._label_of(c.claim_id).citable
        }
        cited = set(self.report.cited_claim_ids) if self.report else set()
        return FunnelMetrics(
            retrieval_hits=sum(h.covered_by_retrieval for h in self.key_point_hits),
            retrieval_total=len(self.key_points),
            claims_extracted=len(claims),
            claims_supported=counts["supported"],
            claims_partial=counts["partial"],
            claims_unverified=counts["unverified"],
            claims_unsupported=counts["unsupported"],
            claims_citable=len(citable_ids),
            claims_cited=len(cited & citable_ids),
        )

    def _label_of(self, claim_id: str) -> SupportLabel:
        """无 verifier 记录时（A0/A1 组）视为可引用：消融要能跑在残缺配置上。"""
        for v in self.verifications:
            if v.claim_id == claim_id:
                return v.label
        return SupportLabel.SUPPORTED

    @property
    def succeeded(self) -> bool:
        """任务成功率口径：无异常且拿到报告。**质量不进这个定义** —— 质量由指标单独说话。"""
        return self.status is RunStatus.OK and self.report is not None

    def quality_flags(self) -> list[str]:
        """未达标原因标签，供报告做"失败类型分布"（面试时比多一个功能有用）。"""
        flags: list[str] = []
        if not self.succeeded:
            flags.append("no_report")
        if self.coverage is not None and self.coverage < MIN_COVERAGE:
            flags.append(f"coverage<{MIN_COVERAGE}")
        if self.faithfulness is not None and self.faithfulness < MIN_FAITHFULNESS:
            flags.append(f"faithfulness<{MIN_FAITHFULNESS}")
        flags.extend(self.judge_notes)  # judge 层数据缺口（cover_parse_failed 等）原样上报
        if self.report is not None:
            # 未判定引用的显式报数（ClaimJudgment 文档早就承诺"报告里显式报数"，
            # 这里是它的实现）：静默丢弃会让 2 条引用判 1 条 → 报 1.0 的假满分
            judged_ids = {j.claim_id for j in self.judgments if j.judge_label is not None}
            gap = sum(cid not in judged_ids for cid in self.report.cited_claim_ids)
            if gap:
                flags.append(f"judge_gap:{gap}")
        f = self.funnel()
        if f.retrieval_total and f.retrieval_hit_rate < 0.5:
            flags.append("retrieval_low")
        if f.claims_citable and f.claims_cited / f.claims_citable < 0.5:
            flags.append("writer_dropped_many")
        return flags

    def check_judge_independence(self) -> list[str]:
        """同源偏差自检。软校验：调试期允许同厂，出数前必须为空。

        不写成硬校验，是因为开发早期 mock 阶段必然同厂；
        但 eval.runner 每次批跑都要把它打进报告首行。
        """
        problems: list[str] = []
        if self.judge_model in set(self.model_snapshots.values()):
            problems.append(f"judge_model 与生产模型相同：{self.judge_model}")
        if self.judge_vendor != "unknown" and self.judge_vendor in set(self.producer_vendors):
            problems.append(f"judge 与生产链路同厂商：{self.judge_vendor}")
        return problems

    # ------------------------------ 结构校验 ------------------------------ #
    @model_validator(mode="after")
    def _consistency(self) -> EvalRecord:
        if self.status is RunStatus.OK and self.report is None:
            raise ValueError("status=OK 却没有 report：成功率会被虚高统计")

        ids = [c.claim_id for f in self.findings for c in f.claims]
        if len(ids) != len(set(ids)):
            # Finding 内的查重挡不住跨组重复；set() 会把重复静默坍缩，总量指标失真
            dupes = sorted({i for i in ids if ids.count(i) > 1})
            raise ValueError(f"claim_id 跨 finding 重复：{dupes[:3]}")
        claim_ids = set(ids)

        seen: set[str] = set()
        dup_v: set[str] = set()
        for v in self.verifications:
            (dup_v if v.claim_id in seen else seen).add(v.claim_id)
        if dup_v:
            raise ValueError(f"同一 claim 有多条 verification：{sorted(dup_v)[:3]}")

        orphan = [v.claim_id for v in self.verifications if v.claim_id not in claim_ids]
        if orphan:
            raise ValueError(f"verification 指向不存在的 claim：{orphan[:3]}")

        seen_j: set[str] = set()
        dup_j: set[str] = set()
        for j in self.judgments:
            (dup_j if j.claim_id in seen_j else seen_j).add(j.claim_id)
        if dup_j:
            raise ValueError(f"同一 claim 有多条 judgment（忠实度分母会重复计数）：{sorted(dup_j)[:3]}")

        ghost_j = [j.claim_id for j in self.judgments if j.claim_id not in claim_ids]
        if ghost_j:
            raise ValueError(f"judgment 指向不存在的 claim：{ghost_j[:3]}")

        label_by = {v.claim_id: v.label for v in self.verifications}
        if self.report is not None:
            missing = [cid for cid in self.report.cited_claim_ids if cid not in claim_ids]
            if missing:
                raise ValueError(f"报告引用了不存在的 claim：{missing[:3]}")
            # 设计规则（§3.2）早已定死：unsupported 不得进报告。放行会让漏斗
            # （citable 才计入 claims_cited）与忠实度（计入分母）对同一条引用
            # 给出矛盾处理——面试答"这条引用算不算"会自相矛盾
            bad_cite = [
                cid for cid in self.report.cited_claim_ids
                if label_by.get(cid) is SupportLabel.UNSUPPORTED
            ]
            if bad_cite:
                raise ValueError(
                    f"报告引用了 verifier 判为 unsupported 的 claim：{bad_cite[:3]}"
                )

        if self.key_point_hits and len(self.key_point_hits) != len(self.key_points):
            raise ValueError(
                f"key_point_hits({len(self.key_point_hits)}) 与 key_points"
                f"({len(self.key_points)}) 数量不一致：覆盖率分母会失真"
            )
        if self.key_point_hits:
            # 长度对齐挡不住文本错位：hits 顺序换了覆盖率数字照样对、归因全错
            if sorted(h.key_point for h in self.key_point_hits) != sorted(self.key_points):
                raise ValueError(
                    "key_point_hits 的 key_point 文本与 key_points 不对应：按长度对齐会静默错位"
                )
            # 开放项 ② 定死的口径：覆盖证据必须 ⊆ citation_map——
            # 否则覆盖率拿"抽取了但没引用"的 claim 作证据，与忠实度证据集分叉
            if self.report is not None:
                cited = set(self.report.cited_claim_ids)
                uncited_ev = sorted(
                    {cid for h in self.key_point_hits for cid in h.evidence_claim_ids
                     if cid not in cited}
                )
                if uncited_ev:
                    raise ValueError(
                        f"key_point 证据未被报告引用（证据集必须 ⊆ citation_map）：{uncited_ev[:3]}"
                    )
            # "证据非空"挡不住证据指向幻觉 claim_id——那是水词命中的变体
            ghost_ev = sorted(
                {cid for h in self.key_point_hits for cid in h.evidence_claim_ids
                 if cid not in claim_ids}
            )
            if ghost_ev:
                raise ValueError(f"key_point 证据指向不存在的 claim：{ghost_ev[:3]}")
        return self


# --------------------------------------------------------------------------- #
# 六、人工抽检一致性
# --------------------------------------------------------------------------- #
KAPPA_NOTE = (
    "kappa 在类别极不均衡时会系统性低估：当双方 95% 都判 supported 时，"
    "即使观测一致率 95%，kappa 也可能只有 ~0.47（prevalence effect）。"
    "所以我们以富集抽样的观测一致率为主指标，kappa 只作参考并注明该局限。"
)


class AgreementStats(Contract):
    """人类 vs judge 的一致性汇总。n 小的时候不要看 kappa，看混淆矩阵。"""

    n: int = Field(description="参与比对的人工样本数（有 human_label 的条数）。")
    observed_agreement: float = Field(description="主指标：直接相等比例。")
    cohen_kappa: float | None = Field(default=None, description="参考值，见 KAPPA_NOTE。")
    confusion: dict[str, dict[str, int]] = Field(
        default_factory=dict, description="{human: {judge: count}}。矩阵比单数字更能说明分歧模式。"
    )
    n_enriched: int = Field(default=0, description="富集层样本数，用于说明抽样设计。")
    n_random: int = Field(default=0, description="随机层样本数。")

    def describe(self) -> str:
        k = "n/a" if self.cohen_kappa is None else f"{self.cohen_kappa:.2f}"
        return (
            f"n={self.n}（随机 {self.n_random} / 富集 {self.n_enriched}）"
            f"一致率={self.observed_agreement:.1%} kappa={k}"
        )

    @classmethod
    def from_judgments(cls, judgments: list[ClaimJudgment]) -> AgreementStats:
        pairs = [(j.human_label, j.judge_label) for j in judgments if j.agreed is not None]
        if not pairs:
            return AgreementStats(n=0, observed_agreement=0.0)
        n = len(pairs)
        agree = sum(h is j for h, j in pairs)
        confusion: dict[str, dict[str, int]] = {}
        for h, j in pairs:
            assert h is not None and j is not None
            confusion.setdefault(h.value, {}).setdefault(j.value, 0)
            confusion[h.value][j.value] += 1
        return cls(
            n=n,
            observed_agreement=round(agree / n, 4),
            cohen_kappa=_cohen_kappa([(h.value, j.value) for h, j in pairs]),
            confusion=confusion,
            # 分母自洽：层计数只统计"确实参与比对"的样本，
            # 否则 n=1 而 随机1+富集2=3，三列数字互相打架
            n_enriched=sum(
                j.sample_stratum == "enriched_non_supported"
                for j in judgments if j.agreed is not None
            ),
            n_random=sum(
                j.sample_stratum == "random"
                for j in judgments if j.agreed is not None
            ),
        )


def _cohen_kappa(pairs: list[tuple[str, str]]) -> float | None:
    """Cohen's kappa。类别单一（pe=1）时无定义，返回 None。"""
    if not pairs:
        return None
    n = len(pairs)
    po = sum(a == b for a, b in pairs) / n
    labels = {a for a, _ in pairs} | {b for _, b in pairs}
    pe = 0.0
    for lb in labels:
        pa = sum(a == lb for a, _ in pairs) / n
        pb = sum(b == lb for _, b in pairs) / n
        pe += pa * pb
    if math.isclose(1.0 - pe, 0.0):
        return None
    return round((po - pe) / (1.0 - pe), 4)


__all__ = [
    "AblationConfig",
    "AgreementStats",
    "Claim",
    "ClaimJudgment",
    "EvalQuestion",
    "EvalRecord",
    "Finding",
    "FunnelMetrics",
    "KAPPA_NOTE",
    "KeyPointHit",
    "QuestionType",
    "ReportArtifact",
    "ReportSection",
    "RunStatus",
    "SourceQuality",
    "SupportLabel",
    "Verification",
    "count_labels",
]


# --------------------------------------------------------------------------- #
# 自检：python -m src.agent.schemas
# 手算一遍口径，确认 schema 能拦住"刷分样本"
# --------------------------------------------------------------------------- #
if __name__ == "__main__":
    q = EvalQuestion(
        question_id="Q001",
        question="Rust 1.85 稳定版主要引入了什么特性？",
        type="fact_check",
        difficulty=2,
        key_points=["edition 2024 稳定", "async closures", "trait 相关改进"],
        as_of_date=date(2026, 1, 1),
    )

    def mk(cid: str, text: str, quality: SourceQuality = SourceQuality.OFFICIAL) -> Claim:
        return Claim(
            claim_id=cid,
            sub_query_id="SQ1",
            text=text,
            quote="quote 原文……",
            url="https://example.com/a",
            source_quality=quality,
            as_of_date=date(2026, 1, 1),
            extracted_by="deepseek-chat",
        )

    claims = [mk("c1", "1.85 稳定了 Edition 2024"), mk("c2", "引入 async closures"), mk("c3", "更新了 trait")]
    findings = [
        Finding(
            sub_query_id="SQ1",
            question="Rust 1.85 特性",
            status=RunStatus.OK,
            search_queries=["Rust 1.85 release notes"],
            pages_fetched=3,
            claims=claims,
        )
    ]
    verifications = [
        Verification(claim_id="c1", label=SupportLabel.SUPPORTED, rationale="原文明确", checked_by="glm-4-flash", prompt_version="v1"),
        Verification(claim_id="c2", label=SupportLabel.PARTIAL, rationale="只有部分提及", checked_by="glm-4-flash", prompt_version="v1"),
        Verification(claim_id="c3", label=SupportLabel.UNSUPPORTED, rationale="quote 未涉及", checked_by="glm-4-flash", prompt_version="v1"),
    ]
    report = ReportArtifact(
        markdown="## 结论\n1.85 稳定了 Edition 2024[1]，并引入 async closures[2]（不确定）。",
        citation_map={1: "c1", 2: "c2"},
    )
    rec = EvalRecord(
        run_id="r1",
        question_id=q.question_id,
        config=AblationConfig.A2,
        code_version="abc123-dirty",
        prompt_version="p1",
        dataset_version="d1",
        model_snapshots={"researcher": "deepseek-chat-2026xx", "writer": "gpt-4.1-2026xx"},
        judge_model="glm-4-plus-2026xx",
        producer_vendors=["deepseek", "openai"],
        judge_vendor="zhipu",
        question=q.question,
        question_type=q.question_type,
        difficulty=q.difficulty,
        key_points=q.key_points,
        as_of_date=q.as_of_date,
        findings=findings,
        verifications=verifications,
        report=report,
        judgments=[
            ClaimJudgment(claim_id="c1", judge_label=SupportLabel.SUPPORTED, judge_model="glm", human_label=SupportLabel.SUPPORTED, sample_stratum="random"),
            ClaimJudgment(claim_id="c2", judge_label=SupportLabel.PARTIAL, judge_model="glm", human_label=SupportLabel.UNSUPPORTED, sample_stratum="enriched_non_supported"),
        ],
        key_point_hits=[
            KeyPointHit(key_point=q.key_points[0], covered_by_retrieval=True, covered_in_report=True, evidence_claim_ids=["c1"]),
            KeyPointHit(key_point=q.key_points[1], covered_by_retrieval=True, covered_in_report=True, evidence_claim_ids=["c2"]),
            KeyPointHit(key_point=q.key_points[2], covered_by_retrieval=False, covered_in_report=False),
        ],
    )

    print("-- 指标 --")
    print(f"忠实度={rec.faithfulness} 覆盖率={rec.coverage} F1={rec.f1}")
    print("-- 漏斗 --")
    print(rec.funnel().describe())
    print("-- 质量闸门 --", rec.quality_flags() or "全部达标")
    print("-- 同源偏差 --", rec.check_judge_independence() or "无问题")
    print("-- 人工一致性 --", AgreementStats.from_judgments(rec.judgments).describe())

    # 反例：无证据的"命中"必须被拦
    try:
        KeyPointHit(key_point="x", covered_by_retrieval=True, covered_in_report=True)
        print("!! 水词命中没被拦住")
    except Exception as e:  # noqa: BLE001
        print("-- 拦截水词命中 -- OK")
