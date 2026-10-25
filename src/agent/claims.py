"""researcher 原始输出 → Claim 的解析层：snippet_id → quote_locator 的唯一通道。

这是 W3 开工前的最后一块胶水。规则延续"模型报句柄、代码定位偏移"
（第四例：citation_map / word_count / snippet_id 之后）：
researcher 只输出 {text, quote, snippet_id}，绝对偏移、URL、as_of_date、
locator 全部由代码从归档事实填充。

纪律清单（每条都有对应的"第二次执行"测试锁住）：
1. claim_id 含 run_id（schemas 建议格式）——两次 run、消融三组之间
   互不污染，SQLite 拿它当主键是安全的；
2. snippet 查询走 PageResolver 协议而非 PageFetcher 具体类型——今天内存
   字典实现，W3 的 SQLite 实现直接替换；进程重启后 pages 为空的恢复
   场景由存储层实现兜住；
3. 定位成功后 quote 回填为归档原文切片——Claim.quote ≡ locator 切片
   无条件成立，"逐字复制、不改写"不再是文档里的愿望；
4. 引文在片段中出现多次 → ambiguous_quote 失败并回喂"引用更长上下文"，
   绝不静默锚第一处；
5. 失败是数据不是异常：ResolveFailure 带原始 index，claim 编号保持
   原始位置不因中途失败错位；
6. as_of_date 回退链：页面发布日 → 抓取日（转本地时区再取日）。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal, Protocol

from src.agent.schemas import Claim, Contract
from src.agent.tools.pages import FetchedPage, PageSnippet


class RawResearcherClaim(Contract):
    """researcher 结构化输出的单个论断——W3 researcher 输出契约的种子。

    刻意只有三个字段：模型负责"说了什么、引了哪句、来自哪个片段"，
    其余全部由代码从归档填充。
    """

    text: str
    quote: str
    snippet_id: str


class ResolveFailure(Contract):
    """一条原始论断解析失败的记录。index 指向 researcher 输出列表的位置，
    便于把失败喂回模型重试。"""

    index: int
    snippet_id: str | None
    reason: Literal[
        "unknown_snippet", "quote_not_located", "ambiguous_quote", "empty_field"
    ]
    detail: str = ""


class PageResolver(Protocol):
    """snippet_id → (FetchedPage, PageSnippet) 的查询协议。

    今天由 PageFetcher 的内存字典满足；W3 换 SQLite 实现时本层零改动。
    这条缝是"任务可恢复"的前提：进程重启后内存索引为空，历史
    snippet_id 的解析必须落到持久实现上，不能长在 fetcher 身上。
    """

    def get_snippet(self, snippet_id: str) -> tuple[FetchedPage, PageSnippet] | None: ...


@dataclass
class ClaimBuildResult:
    claims: list[Claim] = field(default_factory=list)
    failures: list[ResolveFailure] = field(default_factory=list)


def build_claims(
    run_id: str,
    sub_query_id: str,
    raws: list[RawResearcherClaim],
    resolver: PageResolver,
    extracted_by: str,
) -> ClaimBuildResult:
    """把 researcher 的原始输出解析成可入 Finding 的 Claim。

    claim_id = {run_id}:{sub_query_id}:{序号}（schemas 建议格式）——
    序号取原始输出位置，失败不挤占成功项的编号。
    """
    result = ClaimBuildResult()
    for i, raw in enumerate(raws):
        if not raw.text.strip() or not raw.quote.strip():
            result.failures.append(ResolveFailure(
                index=i, snippet_id=raw.snippet_id, reason="empty_field"))
            continue

        located = resolver.get_snippet(raw.snippet_id)
        if located is None:
            result.failures.append(ResolveFailure(
                index=i, snippet_id=raw.snippet_id, reason="unknown_snippet"))
            continue
        page, snippet = located

        span = snippet.locate_quote(raw.quote)
        if span == "ambiguous":
            result.failures.append(ResolveFailure(
                index=i, snippet_id=raw.snippet_id, reason="ambiguous_quote",
                detail="引文在片段中出现多次，请引用更长上下文以唯一定位"))
            continue
        if span is None:
            result.failures.append(ResolveFailure(
                index=i, snippet_id=raw.snippet_id, reason="quote_not_located"))
            continue

        # 回填归档原文：Claim.quote ≡ locator 切片，无条件逐字成立
        local_start = span[0] - snippet.char_start
        local_end = span[1] - snippet.char_start
        verbatim_quote = snippet.passage[local_start:local_end]

        result.claims.append(Claim(
            claim_id=f"{run_id}:{sub_query_id}:{i + 1:03d}",
            sub_query_id=sub_query_id,
            text=raw.text,
            quote=verbatim_quote,
            url=snippet.url,
            page_title=page.title,
            as_of_date=snippet.published_at or page.fetched_at.astimezone().date(),
            fetched_at=page.fetched_at,
            # 句级锚定：指向 quote 的精确跨度，非整个片段
            quote_locator=f"{page.text_path}#{span[0]}-{span[1]}",
            extracted_by=extracted_by,
        ))
    return result
