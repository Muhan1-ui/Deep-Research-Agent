"""Writer：已核验论断 → 带引用的研究报告。

citation_map 程序化生成（第二轮审计 ⑧ 的约束落地）：
- 代码给可引用论断分配临时编号并生成输入表；模型只在正文用 [[n]] 标记——
  它从头到尾接触不到"编号→claim"的绑定；
- **正文 [[n]] token 是唯一的成员资格来源**（显示层事实，第六轮审计 P2-D）：
  出现在正文里的有效编号才进 citation_map；cites 声明字段已退出协议，
  "声明了但正文没写"的编号不再进忠实度分母；
- 代码把用到的编号重排成 1..K 连续，正文 token 由代码替换（[[旧]] → [新]，
  无效 token 剥除）；报告末尾追加按 URL 分组的来源清单（第六轮 P1-A）；
- 不解析普通 [n]（会误伤代码块与链接，如 choices[0]）——[[n]] 是我们自己
  定义的协议 token，与自然文本无碰撞；
- 编号前按 (quote, url) 归并完全相同的论断：同一份证据被多个子问题命中时
  只保留一条（首次出现），不生成指向同一引文的冗余编号；归并条数写入
  ReportArtifact.deduped，随 write_done trace 落盘。
"""

from __future__ import annotations

import re

from src.agent.agents.planner import extract_json_object
from src.agent.schemas import (
    REFERENCE_HEADING,
    Claim,
    Finding,
    ReportArtifact,
    ReportSection,
    SupportLabel,
    Verification,
)

WRITER_PROMPT = """你是研究报告撰写人。基于给定的已核验论断，写一份中文研究报告。
规则：
1. 只使用给定论断，不新增事实；论断未覆盖的方面写"未检索到充分信息"；
2. 引用格式：在句子末尾用 [[编号]] 标注（如"……[[1]]"），编号只能来自输入表；
3. 输入表标注 partial 的论断，引用处标注（部分支持）；标注"未核验"的论断标注（未核验）；
4. 章节用 ## 标题组织，总篇幅 400~800 字；不需要写来源清单（程序会生成）；
5. 只输出 JSON 对象：{"sections": [{"heading": "...", "body": "..."}]}。"""

TOKEN_RE = re.compile(r"\[\[(\d+)\]\]")
# 自然语言的引用写法（单方括号）。只用来*检测*格式错误，永不参与引用判定——
# 正文里 "choices[0].delta.content" 这类也会命中，这正是不能拿正则统计引用的原因。
BARE_CITE_RE = re.compile(r"\[\d+\]")

_RETRY_HINT = "输出不符合要求（需要 JSON 对象且含 sections 数组）。请重新只输出合规 JSON。"
_BARE_CITE_HINT = (
    "引用格式不对：正文里用的是单方括号 [1]，协议要求双方括号 [[1]]。"
    "请保持正文内容不变，把所有引用标记改成 [[n]] 形式，然后重新只输出完整 JSON。"
)


def _looks_like_bare_citations(output: dict) -> bool:
    """正文出现 [n] 却没有任何 [[n]]：模型把协议 token 写成了自然引用。

    第七轮审计：cites 退出协议后，正文 token 是唯一的成员资格来源，
    一次符号笔误就会让整份报告零引用（run 直接降级）。这是廉价保险——
    只在异常时多花一次调用，正常路径零开销。
    """
    bodies = [
        str(s.get("body", ""))
        for s in (output.get("sections") or [])
        if isinstance(s, dict)
    ]
    joined = "\n".join(bodies)
    return bool(BARE_CITE_RE.search(joined)) and "[[" not in joined


def dedupe_claims(citable: list[Claim]) -> tuple[list[Claim], int]:
    """按 (quote, url) 归并完全相同的论断，保留首次出现（claim_id 最小）。

    只合并**完全相同**的证据对：同一 URL 但 quote 不同是不同的事实，不能合并
    （否则会丢信息）。归并发生在编号之前，所以重复论断根本拿不到编号，
    正文与来源清单里都不会出现指向同一 quote 的冗余 [n]。
    """
    seen: set[tuple[str, str]] = set()
    kept: list[Claim] = []
    for c in citable:
        key = (c.quote, c.url)
        if key in seen:
            continue
        seen.add(key)
        kept.append(c)
    return kept, len(citable) - len(kept)


def assemble_citable(
    findings: list[Finding], verifications: list[Verification],
) -> tuple[list[tuple[int, Claim]], list[Claim], int]:
    """可引用论断（非 unsupported）全局按 claim_id 排序后编号 1..k；
    返回 (编号表, 被拒论断列表, 被归并掉的重复条数)。编号分配完全由代码
    决定，与 finding 顺序无关——同一批 claims 在任何编排顺序下编号一致
    （可复现）。重复 (quote, url) 在编号前归并，首次出现的 claim_id 保留。"""
    label_by = {v.claim_id: v.label for v in verifications}
    citable: list[Claim] = []
    refused: list[Claim] = []
    for f in findings:
        for c in f.claims:
            (citable if label_by.get(c.claim_id) is not SupportLabel.UNSUPPORTED else refused).append(c)
    citable.sort(key=lambda c: c.claim_id)
    citable, deduped = dedupe_claims(citable)
    return [(i, c) for i, c in enumerate(citable, 1)], refused, deduped


def build_writer_input(question: str, numbered: list[tuple[int, Claim]],
                       verifications: list[Verification]) -> str:
    by_cid = {v.claim_id: v for v in verifications}
    lines = [f"研究问题：{question}", "", "可引用论断表（编号 | 论断 | 引文 | 核验结果）："]
    for n, c in numbered:
        v = by_cid.get(c.claim_id)
        if v is not None and v.unverified:
            tag = "未核验"  # 与真 partial 严格区分：'不知道'≠'知道一点'
        else:
            tag = v.label.value if v is not None else "unverified"
        if v is not None and v.contradiction_group:
            tag += f"｜矛盾组{v.contradiction_group}"
        lines.append(f"[[{n}]] | {c.text} | {c.quote[:120]} | {tag}")
    return "\n".join(lines)


def build_report(
    question: str,
    numbered: list[tuple[int, Claim]],
    refused: list[Claim],
    writer_output: dict,
    deduped: int = 0,
) -> ReportArtifact:
    """从 writer 的结构化输出构造 ReportArtifact。编号绑定由代码完成。

    成员资格 = 正文 [[n]] token ∩ 有效编号（显示层事实）。writer_output
    里的 cites 字段仅为兼容旧格式保留，不参与成员判定。
    """
    claims_by_no = {n: c for n, c in numbered}
    valid = set(claims_by_no)

    raw_sections = writer_output.get("sections") or []
    used: set[int] = set()
    cleaned_sections: list[tuple[str, str]] = []
    for sec in raw_sections:
        heading = str(sec.get("heading", "")).strip() or "（未命名章节）"
        body = str(sec.get("body", ""))
        used |= {int(m) for m in TOKEN_RE.findall(body)} & valid
        cleaned_sections.append((heading, body))

    renumber = {old: new for new, old in enumerate(sorted(used), 1)}

    def _sub(m: re.Match) -> str:
        old = int(m.group(1))
        return f"[{renumber[old]}]" if old in renumber else ""  # 无效/被弃引用：剥除标记

    parts = [f"# {question}"]
    sections: list[ReportSection] = []
    for heading, body in cleaned_sections:
        new_body = TOKEN_RE.sub(_sub, body)
        sections.append(ReportSection(heading=heading, body=new_body))
        parts.append(f"## {heading}\n\n{new_body}")

    # 来源清单（第六轮 P1-A）：按 URL 分组、编号聚合，读者可点开核对
    if renumber:
        inv = {new: old for old, new in renumber.items()}
        grouped: dict[str, list[int]] = {}
        titles: dict[str, str] = {}
        for new in sorted(inv):
            c = claims_by_no[inv[new]]
            grouped.setdefault(c.url, []).append(new)
            titles.setdefault(c.url, c.page_title or c.url)
        ref_body = "\n".join(
            f"{''.join(f'[{n}]' for n in nums)} {titles[url]} — {url}"
            for url, nums in grouped.items()
        )
        parts.append(f"## {REFERENCE_HEADING}\n\n{ref_body}")
        sections.append(ReportSection(heading=REFERENCE_HEADING, body=ref_body))

    citation_map = {new: claims_by_no[old].claim_id for old, new in renumber.items()}
    refusals = [f"verifier 判 unsupported，拒绝写入：{c.claim_id} {c.text[:60]}" for c in refused]

    return ReportArtifact(
        markdown="\n\n".join(parts), sections=sections,
        citation_map=citation_map, refusals=refusals, deduped=deduped,
    )


async def write_report(
    llm, question: str, findings: list[Finding], verifications: list[Verification],
    max_retries: int = 1,
) -> ReportArtifact:
    numbered, refused, deduped = assemble_citable(findings, verifications)
    if not numbered:
        # 无可引用论断：产出诚实的空报告而非崩溃（run 状态由 findings 决定）
        return ReportArtifact(
            markdown=f"# {question}\n\n未检索到可支撑回答的论断。",
            citation_map={}, refusals=[f"无可引用论断：{c.claim_id}" for c in refused],
            deduped=deduped,
        )
    user_msg = build_writer_input(question, numbered, verifications)
    messages: list[dict] = [
        {"role": "system", "content": WRITER_PROMPT},
        {"role": "user", "content": user_msg},
    ]
    output: dict | None = None
    bare_fallback: dict | None = None  # 内容可用但引用格式不合规的候选
    for _ in range(max_retries + 1):
        result = await llm.complete(messages)
        messages.append({"role": "assistant", "content": result.content or ""})
        try:
            parsed = extract_json_object(result.content or "")
        except ValueError:
            parsed = None
        if parsed and parsed.get("sections"):
            if _looks_like_bare_citations(parsed):
                # 单方括号：内容保留为兜底，但要求模型改格式重发一次
                bare_fallback = parsed
                messages.append({"role": "user", "content": _BARE_CITE_HINT})
                continue
            output = parsed
            break
        messages.append({"role": "user", "content": _RETRY_HINT})
    if output is None and bare_fallback is not None:
        # 重试用尽仍不合规：正文仍可读，但不能伪称有可校验引用
        report = build_report(question, numbered, refused, bare_fallback, deduped=deduped)
        report.refusals.append(
            "引用格式不合规：模型使用单方括号 [n] 而非 [[n]]，本次报告无可校验引用"
        )
        return report
    if output is None:
        return ReportArtifact(
            markdown=f"# {question}\n\n（报告生成失败：writer 输出不合规）",
            citation_map={},
            refusals=[f"writer 输出解析失败；可引用论断 {len(numbered)} 条被弃"],
        )
    return build_report(question, numbered, refused, output, deduped=deduped)
