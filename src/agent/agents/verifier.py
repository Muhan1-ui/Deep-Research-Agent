"""Verifier：claim.text 是否被 claim.quote 支持（三分类）+ 跨源矛盾检测。

失败语义（A2 不允许静默放行）：
- 判定解析失败 → 回喂重试一次；仍失败或模型漏判的 claim → 记 PARTIAL +
  rationale 标"验证失败，未核验"——宁可保守可引用，也不静默默认 supported
  （那是 A0/A1 的语义）。
- 矛盾检测解析失败 → 跳过（contradiction_group 保持 None），不影响主流程。
"""

from __future__ import annotations

import json

from src.agent.agents.planner import extract_json_array
from src.agent.schemas import Claim, SupportLabel, Verification

VERIFIER_PROMPT = """你是事实核查员。对每条论断，判断其引文（quote）是否支持论断（text）。
三分类：
- supported：引文完整支持论断
- partial：引文只支持论断的一部分，或支持力度弱、信息不完整
- unsupported：引文不包含支持该论断的内容
只输出 JSON 数组，每项形如
{"claim_id": "...", "label": "supported|partial|unsupported", "rationale": "一句话理由（50字内）"}。
逐条对应输入，不要遗漏、不要新增条目。"""

CONTRADICTION_PROMPT = """你是矛盾检测员。下面是同一研究里已核验的论断列表。
找出互相矛盾（对同一事实给出不一致说法）的论断组。
只输出 JSON 数组，每项形如 {"claim_ids": ["...", "..."], "summary": "矛盾点一句话"}。
没有矛盾就输出 []。"""

_BATCH = 12
_LABELS = {lb.value for lb in SupportLabel}


def _chunks(items: list, size: int):
    for i in range(0, len(items), size):
        yield items[i : i + size]


def _parse_verdicts(text: str, expected_ids: set[str]) -> dict[str, tuple[SupportLabel, str]]:
    items = extract_json_array(text)
    out: dict[str, tuple[SupportLabel, str]] = {}
    for it in items:
        if not isinstance(it, dict):
            continue
        cid = it.get("claim_id")
        label = it.get("label")
        if cid in expected_ids and label in _LABELS:
            # rationale 截断到 schema 上限内，超长理由不值得让整次调用失败
            out[cid] = (SupportLabel(label), str(it.get("rationale", ""))[:200].strip())
    return out


def _unverified(cid: str, checked_by: str, pv: str) -> Verification:
    return Verification(
        claim_id=cid, label=SupportLabel.PARTIAL, unverified=True,
        rationale="验证失败，未核验（保守记 partial）", checked_by=checked_by, prompt_version=pv,
    )


async def verify_claims(
    llm, claims: list[Claim], checked_by: str, prompt_version: str = "v1",
    max_retries: int = 1,
) -> list[Verification]:
    """分批核对全部 claims。返回与输入同序的 Verification 列表（每条必有）。"""
    verifications: list[Verification] = []
    for batch in _chunks(claims, _BATCH):
        expected = {c.claim_id for c in batch}
        payload = json.dumps(
            [{"claim_id": c.claim_id, "text": c.text, "quote": c.quote} for c in batch],
            ensure_ascii=False,
        )
        messages: list[dict] = [
            {"role": "system", "content": VERIFIER_PROMPT},
            {"role": "user", "content": payload},
        ]
        got: dict[str, tuple[SupportLabel, str]] = {}
        for _ in range(max_retries + 1):
            result = await llm.complete(messages)
            messages.append({"role": "assistant", "content": result.content or ""})
            try:
                got = _parse_verdicts(result.content or "", expected)
                # 部分响应必须重试：模型截断/只回前几条时，缺的每条都会
                # 被无声降级成"未核验"（第六轮审计 P2-C，最坏 11/12 条）
                if got and len(got) == len(expected):
                    break
            except ValueError:
                pass
            messages.append({
                "role": "user",
                "content": "输出不符合要求（JSON 数组、逐条对应 claim_id、不得遗漏）。请重新只输出合规 JSON。",
            })
        for c in batch:
            hit = got.get(c.claim_id)
            if hit is None:
                verifications.append(_unverified(c.claim_id, checked_by, prompt_version))
            else:
                verifications.append(Verification(
                    claim_id=c.claim_id, label=hit[0], rationale=hit[1],
                    checked_by=checked_by, prompt_version=prompt_version,
                ))
    return verifications


async def detect_contradictions(
    llm, verifications: list[Verification], claims_by_id: dict[str, Claim],
) -> dict[str, str]:
    """跨源矛盾：返回 claim_id → 组标记（cg1/cg2...）。解析失败返回空 dict。"""
    candidates = [v.claim_id for v in verifications if v.label is not SupportLabel.UNSUPPORTED]
    if len(candidates) < 2:
        return {}
    payload = json.dumps(
        [{"claim_id": cid, "text": claims_by_id[cid].text} for cid in candidates
         if cid in claims_by_id],
        ensure_ascii=False,
    )
    try:
        result = await llm.complete([
            {"role": "system", "content": CONTRADICTION_PROMPT},
            {"role": "user", "content": payload},
        ])
        groups = extract_json_array(result.content or "")
    except ValueError:
        return {}
    mapping: dict[str, str] = {}
    valid = set(candidates)
    for i, g in enumerate(groups, 1):
        if not isinstance(g, dict):
            continue
        ids = [cid for cid in g.get("claim_ids", []) if cid in valid]
        if len(ids) >= 2:  # 矛盾至少两方；组内 id 会互相覆盖跨组情形，可接受
            for cid in ids:
                mapping[cid] = f"cg{i}"
    return mapping
