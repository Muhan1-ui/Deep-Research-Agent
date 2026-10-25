"""Writer：已核验论断 → 带引用的研究报告。

citation_map 程序化生成（第二轮审计 ⑧ 的约束落地）：
- 代码给可引用论断分配临时编号并生成输入表；模型只在正文用 [[n]] 标记、
  并在结构化输出里声明 cites——它从头到尾接触不到"编号→claim"的绑定；
- 代码取 cites ∩ 有效编号 ∪ 正文 [[n]] 标记 的并集为"实际引用集"，
  重排成 1..K 连续编号，正文 token 由代码替换（[[旧]] → [新]，无效 token 剥除）；
- 不解析普通 [n]（会误伤代码块与链接）——[[n]] 是我们自己定义的协议 token，
  与自然文本无碰撞；且 cites 声明才是机器可读层，token 只是显示层。
"""

from __future__ import annotations

import re

from src.agent.agents.planner import extract_json_object
from src.agent.schemas import (
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
3. verdict=partial 的论断，引用处标注（部分支持）；
4. 章节用 ## 标题组织，总篇幅 400~800 字；
5. 只输出 JSON 对象：{"sections": [{"heading": "...", "body": "..."}], "cites": [引用过的编号]}。"""

TOKEN_RE = re.compile(r"\[\[(\d+)\]\]")


def assemble_citable(
    findings: list[Finding], verifications: list[Verification],
) -> tuple[list[tuple[int, Claim]], list[Claim]]:
    """可引用论断（非 unsupported）全局按 claim_id 排序后编号 1..k；
    返回 (编号表, 被拒论断列表)。编号分配完全由代码决定，与 finding
    顺序无关——同一批 claims 在任何编排顺序下编号一致（可复现）。"""
    label_by = {v.claim_id: v.label for v in verifications}
    citable: list[Claim] = []
    refused: list[Claim] = []
    for f in findings:
        for c in f.claims:
            (citable if label_by.get(c.claim_id) is not SupportLabel.UNSUPPORTED else refused).append(c)
    citable.sort(key=lambda c: c.claim_id)
    return [(i, c) for i, c in enumerate(citable, 1)], refused


def build_writer_input(question: str, numbered: list[tuple[int, Claim]],
                       verifications: list[Verification]) -> str:
    label_by = {v.claim_id: v.label.value for v in verifications}
    contra_by = {v.claim_id: v.contradiction_group for v in verifications}
    lines = [f"研究问题：{question}", "", "可引用论断表（编号 | 论断 | 引文 | 核验结果）："]
    for n, c in numbered:
        tag = label_by.get(c.claim_id, "unverified")
        if contra_by.get(c.claim_id):
            tag += f"｜矛盾组{contra_by[c.claim_id]}"
        lines.append(f"[[{n}]] | {c.text} | {c.quote[:120]} | {tag}")
    return "\n".join(lines)


def build_report(
    question: str,
    numbered: list[tuple[int, Claim]],
    refused: list[Claim],
    writer_output: dict,
) -> ReportArtifact:
    """从 writer 的结构化输出构造 ReportArtifact。编号绑定由代码完成。"""
    claims_by_no = {n: c for n, c in numbered}
    valid = set(claims_by_no)
    cites = {int(n) for n in writer_output.get("cites", []) if str(n).isdigit()}

    raw_sections = writer_output.get("sections") or []
    used: set[int] = set()
    cleaned_sections: list[tuple[str, str]] = []
    for sec in raw_sections:
        heading = str(sec.get("heading", "")).strip() or "（未命名章节）"
        body = str(sec.get("body", ""))
        body_nos = {int(m) for m in TOKEN_RE.findall(body)}
        used |= body_nos & valid          # 正文出现的有效 token 也算引用（声明遗漏兜底）
        used |= cites & valid
        cleaned_sections.append((heading, body))

    used &= valid
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

    citation_map = {new: claims_by_no[old].claim_id for old, new in renumber.items()}
    refusals = [f"verifier 判 unsupported，拒绝写入：{c.claim_id} {c.text[:60]}" for c in refused]

    return ReportArtifact(
        markdown="\n\n".join(parts), sections=sections,
        citation_map=citation_map, refusals=refusals,
    )


async def write_report(
    llm, question: str, findings: list[Finding], verifications: list[Verification],
    max_retries: int = 1,
) -> ReportArtifact:
    numbered, refused = assemble_citable(findings, verifications)
    if not numbered:
        # 无可引用论断：产出诚实的空报告而非崩溃（run 状态由 findings 决定）
        return ReportArtifact(
            markdown=f"# {question}\n\n未检索到可支撑回答的论断。",
            citation_map={}, refusals=[f"无可引用论断：{c.claim_id}" for c in refused],
        )
    user_msg = build_writer_input(question, numbered, verifications)
    messages: list[dict] = [
        {"role": "system", "content": WRITER_PROMPT},
        {"role": "user", "content": user_msg},
    ]
    output: dict | None = None
    for _ in range(max_retries + 1):
        result = await llm.complete(messages)
        messages.append({"role": "assistant", "content": result.content or ""})
        try:
            output = extract_json_object(result.content or "")
            if output.get("sections"):
                break
        except ValueError:
            pass
        messages.append({
            "role": "user",
            "content": "输出不符合要求（JSON 对象含 sections/cites）。请重新只输出合规 JSON。",
        })
    if output is None:
        return ReportArtifact(
            markdown=f"# {question}\n\n（报告生成失败：writer 输出不合规）",
            citation_map={},
            refusals=[f"writer 输出解析失败；可引用论断 {len(numbered)} 条被弃"],
        )
    return build_report(question, numbered, refused, output)
