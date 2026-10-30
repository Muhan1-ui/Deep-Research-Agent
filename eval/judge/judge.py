"""评测 judge：跨厂商 LLM-as-judge（W5）。

两个判定任务，共用"只依据给定材料、不用外部知识"的 rubric：
1. 忠实度：被引用的 (text, quote) 对 → supported / partial / unsupported；
2. 覆盖率：参考要点 × (报告正文 + 被引用论断表 + 抽取论断集) →
   covered_in_report(+evidence) / covered_by_retrieval。

口径决策（第二轮审计遗留的开放项 ② 在此定死）：
**evidence_claim_ids 只允许来自 citation_map**（被报告实际引用的论断）——
覆盖率与忠实度从此共享同一证据集。judge 报出的表外编号一律过滤；
过滤后为空则 covered_in_report 如实降级为 False（不猜、不放宽）。

temperature=0.0 显式传入；思考模式默认关闭由 Settings 保证——
否则 DeepSeek 会静默忽略 temperature（见 llm.py 文档）。
"""

from __future__ import annotations

import json

from src.agent.agents.planner import extract_json_array
from src.agent.schemas import Claim, ClaimJudgment, KeyPointHit, ReportArtifact

BATCH = 12

# judge rubric 版本：随提示词实质变更递增，记录为 ClaimJudgment.judge_model 的 "model#j2"。
# j2（第三轮校准）：补两条边界规则——引文外的编号/名称算 partial（引文自身文档编号除外）、
# 对引文主语的无歧义指代不算新增信息。校准中 3/3 分歧都落在这条边界上。
JUDGE_PROMPT_VERSION = "j2"

# 系统提示首句的判别标记：测试与调试靠它区分两类判定调用
FAITH_MARKER = "评测裁判（忠实度）"
COVER_MARKER = "评测裁判（覆盖率）"

FAITH_PROMPT = f"""你是{FAITH_MARKER}，与被评系统来自不同厂商。对每条"论断+引文"，
判断引文是否支持论断。三分类：
- supported：引文完整支持论断的全部内容
- partial：引文只支持论断的一部分，或支持力度弱
- unsupported：引文不支持论断
只依据引文本身判断，禁止使用你的外部知识；引文没有的信息视为论断没有依据。
两条边界规则：
- 编号/名称：论断出现引文里没有的编号、案号、机构名等 → partial；
  但若该编号就是本条引文所属文档自身的编号（引文出自 RFC 9002、论断写“RFC 9002”），不算新增信息。
- 指代：论断用“该义务 / 该判决 / 这两者”指代引文中的主语，只要指代无歧义，不算新增信息。
只输出 JSON 数组，每项形如 {{"claim_id": "...", "label": "supported|partial|unsupported", "rationale": "一句话"}}，
逐条对应输入，不要遗漏。"""

COVER_PROMPT = f"""你是{COVER_MARKER}，与被评系统来自不同厂商。给定研究问题、参考要点、
研究报告正文、被引用论断表（编号→论断）和抽取论断集，对每个参考要点判定：
- covered_in_report：报告是否实质写到该要点（泛泛而谈不算）；
- evidence：支撑该要点的被引用论断编号列表（只能来自论断表；没覆盖给空表）；
- covered_by_retrieval：抽取论断集里是否有论断触及该要点（即使报告没写）。
只依据给定材料判断，禁止使用外部知识。
只输出 JSON 数组，按输入要点的顺序逐条输出：
[{{"key_point": "...", "covered_in_report": true, "evidence": [1], "covered_by_retrieval": true}}]"""

_LABELS = {"supported", "partial", "unsupported"}


def _batched(items: list, size: int = BATCH):
    for i in range(0, len(items), size):
        yield items[i : i + size]


def _parse_faith(text: str, expected: set[str]) -> dict[str, str]:
    out: dict[str, str] = {}
    for it in extract_json_array(text):
        if isinstance(it, dict) and it.get("claim_id") in expected and it.get("label") in _LABELS:
            out[it["claim_id"]] = it["label"]
    return out


async def judge_cited_claims(
    llm, claims: list[Claim], report: ReportArtifact,
    judge_model: str, prompt_version: str = JUDGE_PROMPT_VERSION, max_retries: int = 1,
) -> list[ClaimJudgment]:
    """对报告实际引用的论断逐条判定（忠实度分母的唯一来源）。"""
    by_id = {c.claim_id: c for c in claims}
    cited = [cid for cid in report.cited_claim_ids if cid in by_id]
    judgments: list[ClaimJudgment] = []
    for batch in _batched(cited):
        expected = set(batch)
        payload = json.dumps(
            [{"claim_id": cid, "text": by_id[cid].text, "quote": by_id[cid].quote}
             for cid in batch],
            ensure_ascii=False)
        messages = [{"role": "system", "content": FAITH_PROMPT},
                    {"role": "user", "content": payload}]
        got: dict[str, str] = {}
        for _ in range(max_retries + 1):
            r = await llm.complete(messages, temperature=0.0)
            messages.append({"role": "assistant", "content": r.content or ""})
            try:
                got = _parse_faith(r.content or "", expected)
                if len(got) == len(expected):
                    break
            except ValueError:
                pass
            messages.append({"role": "user",
                             "content": "输出不符合要求（JSON 数组、逐条对应）。请重新只输出合规 JSON。"})
        for cid in batch:
            lb = got.get(cid)
            judgments.append(ClaimJudgment(
                claim_id=cid,
                judge_label=lb if lb else None,  # 缺判定 = 数据缺口，显式报数（judge_gap）
                judge_model=f"{judge_model}#{prompt_version}",
            ))
    return judgments


def _clean_evidence(raw) -> list[int] | None:
    """evidence 白名单收敛（第九轮审计 P0-2）：只认 int/float 列表。

    null（TypeError）、字符串 "1"/"12"（逐字符/巧合解析）、[1.5] 之外
    的形状一律视为形状失败——宁可重试，不猜。
    """
    if not isinstance(raw, list):
        return None
    out = []
    for n in raw:
        if isinstance(n, bool) or not isinstance(n, (int, float)):
            return None
        out.append(int(n))
    return out


def _clean_flag(raw) -> bool | None:
    """布尔只认 True/False：字符串 "false" 经 bool() 会变 True（假阳性）。"""
    return raw if isinstance(raw, bool) else None


def _parse_cover(text: str, n_points: int) -> list[dict] | None:
    """按输入顺序对齐；条数不符或任一字段形状非法 → None（重试）。"""
    items = extract_json_array(text)
    if not isinstance(items, list) or len(items) != n_points:
        return None
    for it in items:
        if not isinstance(it, dict):
            return None
        if _clean_flag(it.get("covered_in_report")) is None:
            return None
        if _clean_flag(it.get("covered_by_retrieval")) is None:
            return None
        if _clean_evidence(it.get("evidence")) is None:
            return None
    return items


async def judge_key_points(
    llm, question: str, key_points: list[str], report: ReportArtifact,
    claims: list[Claim], judge_model: str, prompt_version: str = JUDGE_PROMPT_VERSION,
    max_retries: int = 1,
) -> tuple[list[KeyPointHit], list[str]]:
    """要点覆盖 + 检索命中（漏斗①与覆盖率共用这份数据）。

    evidence 只收 citation_map 内的编号（开放项 ② 的口径落地）；
    过滤后为空 → covered_in_report=False。
    返回 (hits, notes)：notes 含 "cover_parse_failed" 表示形状重试耗尽、
    全部要点为保守回退——与 judge_gap 同类的数据缺口显式报数，
    不与"真的没写"混淆。
    """
    cited_table = [
        {"no": n, "claim_id": cid, "text": next((c.text for c in claims if c.claim_id == cid), "")[:80]}
        for n, cid in sorted(report.citation_map.items())
    ]
    payload = json.dumps({
        "question": question,
        "key_points": key_points,
        "report": report.markdown[:6000],
        "cited_claims": cited_table,
        "extracted_claims": [{"claim_id": c.claim_id, "text": c.text[:60]} for c in claims],
    }, ensure_ascii=False)
    messages = [{"role": "system", "content": COVER_PROMPT},
                {"role": "user", "content": payload}]
    items: list[dict] | None = None
    for _ in range(max_retries + 1):
        r = await llm.complete(messages, temperature=0.0)
        messages.append({"role": "assistant", "content": r.content or ""})
        try:
            items = _parse_cover(r.content or "", len(key_points))
            if items is not None:
                break
        except ValueError:
            pass
        messages.append({"role": "user",
                         "content": "输出不符合要求（JSON 数组、条数与要点一致、按顺序、"
                                    "covered_*/evidence 类型正确）。请重新只输出合规 JSON。"})
    notes: list[str] = []
    if items is None:  # 保守回退：全部未覆盖（宁低勿高），但必须留痕
        items = [{"covered_in_report": False, "evidence": [], "covered_by_retrieval": False}
                 for _ in key_points]
        notes.append("cover_parse_failed")

    hits: list[KeyPointHit] = []
    for kp, item in zip(key_points, items):
        ev_ids = [report.citation_map[n] for n in _clean_evidence(item.get("evidence")) or []
                  if n in report.citation_map]
        covered = _clean_flag(item.get("covered_in_report")) and bool(ev_ids)
        hits.append(KeyPointHit(
            key_point=kp,
            covered_by_retrieval=bool(_clean_flag(item.get("covered_by_retrieval"))),
            covered_in_report=covered,
            evidence_claim_ids=ev_ids,
        ))
    return hits, notes
