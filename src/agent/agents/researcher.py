"""Researcher：一个子问题 → 检索/深读 → 原始论断 JSON → 解析为 Claim 列表。

复用 W1 的 AgentLoop（搜索/抓取/错误回喂/护栏全继承），新增的是：
- 最终答案必须是论断 JSON 数组，代码解析（模型报句柄，代码定位偏移）；
- 解析失败回喂一轮修复（重建全量数组，claim_id 位置稳定不冲突）；
- search_queries / pages_fetched 从 loop messages 里由代码统计，
  模型从不自报这两个数字（不自报原则第五例）。
"""

from __future__ import annotations

import json
import time

from pydantic import ValidationError

from src.agent.agents.planner import SubQuery, extract_json_array
from src.agent.claims import (
    ClaimBuildResult,
    RawResearcherClaim,
    ResolveFailure,
    build_claims,
)
from src.agent.runtime.loop import AgentLoop
from src.agent.schemas import Finding, RunStatus
from src.agent.tools.base import ToolRegistry

RESEARCHER_PROMPT = """你是研究员，负责回答一个子问题。流程：
1. 先用 web_search 检索（可改写查询词、多次调用）；
2. 对最相关的 1~3 个来源调用 fetch_page 深读（query 传子问题原文）；
3. 从片段中提取 3~8 条论断，每条必须引用片段中逐字存在的原文；
4. 最终回答只输出 JSON 数组，每项形如
   {"text": "论断", "quote": "片段中的原文引句（逐字复制）", "snippet_id": "片段 id"}。
不要编造；片段里没有的信息不要写；snippet_id 必须来自 fetch_page 返回的片段。"""


def parse_raw_claims(text: str | None) -> tuple[list[RawResearcherClaim], list[ResolveFailure]]:
    """最终答案 → 原始论断列表。形状不合规的项按位置记失败（修复轮回喂用）。"""
    if not text or not text.strip():
        return [], [ResolveFailure(index=0, snippet_id=None, reason="invalid_shape",
                                   detail="最终输出为空")]
    try:
        data = extract_json_array(text)
    except ValueError as e:
        return [], [ResolveFailure(index=0, snippet_id=None, reason="invalid_shape", detail=str(e))]
    raws, failures = [], []
    for i, item in enumerate(data):
        try:
            raws.append(RawResearcherClaim.model_validate(item))
        except ValidationError as e:
            failures.append(ResolveFailure(index=i, snippet_id=None,
                                           reason="invalid_shape", detail=str(e)[:200]))
    return raws, failures


def _tool_calls_of(messages: list[dict]) -> list[dict]:
    out = []
    for m in messages:
        for tc in m.get("tool_calls") or []:
            out.append(tc)
    return out


def count_tool_usage(messages: list[dict]) -> tuple[list[str], int]:
    """从 loop 对话里统计：web_search 的 query 列表 / fetch_page 次数。代码统计，模型不自报。"""
    queries: list[str] = []
    fetches = 0
    for tc in _tool_calls_of(messages):
        name = tc["function"]["name"]
        try:
            args = json.loads(tc["function"]["arguments"] or "{}")
        except ValueError:
            continue
        if name == "web_search" and args.get("query"):
            q = args["query"]
            if q not in queries:
                queries.append(q)
        elif name == "fetch_page":
            fetches += 1
    return queries, fetches


def _format_failures(failures: list[ResolveFailure]) -> str:
    return "\n".join(
        f"- 第 {f.index} 项 [{f.reason}] {f.detail or ''} (snippet_id={f.snippet_id})"
        for f in failures
    )


def _merged_tool_usage(results: list) -> tuple[list[str], int]:
    """跨全部轮次（含修复轮）合并统计：query 去重保序，fetch 累加。
    修复轮是全新 AgentLoop，检索可能重做一遍——只统计胜出轮会系统性低估成本。"""
    queries: list[str] = []
    fetches = 0
    for r in results:
        q, n = count_tool_usage(r.messages)
        for x in q:
            if x not in queries:
                queries.append(x)
        fetches += n
    return queries, fetches


async def research_sub_query(
    llm,
    registry: ToolRegistry,
    run_id: str,
    sub: SubQuery,
    resolver,
    extracted_by: str,
    max_turns: int = 8,
    allow_repair: bool = True,
) -> Finding:
    t0 = time.perf_counter()
    # planner 的 search_hints 在这里接通：拼进首轮消息（要么用要么删，不留死字段）
    user_msg = sub.question
    if sub.search_hints:
        user_msg += "\n建议搜索词：" + "、".join(sub.search_hints)

    loop = AgentLoop(llm, registry, max_turns=max_turns)
    result = await loop.run(RESEARCHER_PROMPT, user_msg)
    rounds = [result]  # 用量与工具统计跨所有轮累计，不管哪轮的输出被采纳
    tokens_in = result.usage_in
    tokens_out = result.usage_out

    raws, parse_failures = parse_raw_claims(result.answer)
    built: ClaimBuildResult = build_claims(run_id, sub.sub_query_id, raws, resolver, extracted_by)
    failures = parse_failures + built.failures

    if failures and allow_repair and result.answer:
        # 修复轮：要求重发完整数组（含正确项）→ claim_id 按位置重建，
        # 与首轮成功项天然不冲突。取失败更少的一轮。
        repair_user = (
            f"{sub.question}\n\n[修正轮] 你上一轮的论断输出存在以下问题：\n"
            f"{_format_failures(failures)}\n"
            "请修正后重新输出完整 JSON 数组（包括之前正确的项，保持顺序）。只输出 JSON。"
        )
        r2 = await AgentLoop(llm, registry, max_turns=max_turns).run(RESEARCHER_PROMPT, repair_user)
        rounds.append(r2)
        tokens_in += r2.usage_in
        tokens_out += r2.usage_out
        if r2.answer:
            raws2, pf2 = parse_raw_claims(r2.answer)
            built2 = build_claims(run_id, sub.sub_query_id, raws2, resolver, extracted_by)
            f2 = pf2 + built2.failures
            if len(f2) < len(failures):
                built, failures = built2, f2
                result = r2

    queries, fetches = _merged_tool_usage(rounds)
    if not built.claims:
        status = RunStatus.FAILED
        error = f"stop={result.stop_reason}; failures={len(failures)}"
    elif failures:
        status = RunStatus.DEGRADED  # 检索到了但抽取不全（schemas 定义）
        error = _format_failures(failures)[:500]
    else:
        status = RunStatus.OK
        error = None

    return Finding(
        sub_query_id=sub.sub_query_id,
        question=sub.question,
        status=status,
        search_queries=queries,
        pages_fetched=fetches,
        tokens_in=tokens_in,
        tokens_out=tokens_out,
        claims=built.claims,
        error=error,
        elapsed_s=round(time.perf_counter() - t0, 3),
    )
