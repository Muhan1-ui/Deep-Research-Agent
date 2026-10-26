"""Orchestrator：纯代码状态机（无 LLM 参与调度）。

plan → research(并行) → 落库 + trace。失败降级不阻塞：
单个子任务失败只标记该 Finding，run 继续——W4 的 verify/write
接在同一状态机后面。

llm_factory 注入点：测试传 FakeLLM 工厂，生产默认 LLMClient(settings)。
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime

from src.agent.agents.planner import SubQuery, make_plan
from src.agent.agents.researcher import research_sub_query
from src.agent.agents.verifier import detect_contradictions, verify_claims
from src.agent.agents.writer import write_report
from src.agent.config import Settings
from src.agent.llm import LLMClient
from src.agent.obs.trace import JsonlTrace
from src.agent.schemas import Finding, RunStatus
from src.agent.storage.db import Database
from src.agent.tools.base import Tool, ToolRegistry
from src.agent.tools.fetch import PageFetcher
from src.agent.tools.pages import FetchPageArgs
from src.agent.tools.search import build_search_tool


@dataclass
class ResearchRunResult:
    run_id: str
    question: str
    config: str = "A1"
    plan: list[SubQuery] = field(default_factory=list)
    findings: list[Finding] = field(default_factory=list)
    total_claims: int = 0
    total_failures_degraded: int = 0  # DEGRADED 子任务数（抽取不全）
    tokens_in: int = 0
    tokens_out: int = 0
    report: object | None = None  # ReportArtifact（A2 才有）
    elapsed_s: float = 0.0

    def summary_line(self) -> str:
        ok = sum(f.status is RunStatus.OK for f in self.findings)
        cited = len(self.report.citation_map) if self.report else 0
        return (
            f"run={self.run_id} config={self.config} 子问题={len(self.plan)} "
            f"findings OK/DEGRADED/FAILED={ok}/"
            f"{sum(f.status is RunStatus.DEGRADED for f in self.findings)}/"
            f"{sum(f.status is RunStatus.FAILED for f in self.findings)} "
            f"claims={self.total_claims} cited={cited} "
            f"tokens={self.tokens_in}+{self.tokens_out} 耗时={self.elapsed_s:.1f}s"
        )


def new_run_id() -> str:
    return f"run-{datetime.now().astimezone().strftime('%Y%m%d%H%M%S')}-{uuid.uuid4().hex[:6]}"


def _aggregate_status(findings: list[Finding]) -> RunStatus:
    """run 终态聚合。FAILED 必须进等式：只数 DEGRADED 会把"挂了一个
    子任务"记成完全成功，成功率虚高（第五轮审计 P1-a）。"""
    if not any(f.claims for f in findings):
        return RunStatus.FAILED
    if any(f.status is not RunStatus.OK for f in findings):
        return RunStatus.DEGRADED
    return RunStatus.OK


async def run_research(
    question: str,
    settings: Settings | None = None,
    llm_factory: Callable[[], object] | None = None,
    db: Database | None = None,
    max_concurrency: int = 4,
    config: str = "A1",
    http_client=None,
) -> ResearchRunResult:
    if config not in ("A1", "A2"):  # A0 走 ask 命令
        raise SystemExit(f"未知配置 {config}（research 支持 A1/A2）")
    settings = settings or Settings()
    if not settings.llm_api_key:
        raise SystemExit("缺少 LLM_API_KEY：复制 .env.example 为 .env 并填写")
    llm_factory = llm_factory or (lambda: LLMClient(settings))
    own_db = db is None
    db = db or Database(settings.database_path)

    run_id = new_run_id()
    trace = JsonlTrace(run_id, settings.trace_dir)
    fetcher = PageFetcher(settings.pages_dir, client=http_client, on_page=db.save_page)
    registry = ToolRegistry()
    if settings.tavily_api_key:
        registry.register(build_search_tool(settings.tavily_api_key))
    registry.register(Tool(  # 共享同一个 fetcher：claims 解析 + 落库都靠它
        name="fetch_page",
        description="读取 URL 正文，返回与 query 最相关的片段（带 id）。",
        args_model=FetchPageArgs,
        handler=fetcher,
    ))
    # llm 生命周期：工厂每产一个就登记一个，finally 统一 aclose——
    # 否则批跑几百次 run，每个 worker 一个连接池，fd 会耗尽
    llms: list = []

    def make_llm():
        llm = llm_factory()
        llms.append(llm)
        return llm

    t0 = datetime.now().astimezone()
    result = ResearchRunResult(run_id=run_id, question=question, config=config)
    try:
        trace.event("run_start", question=question, run_id=run_id, config=config)
        await db.save_run(run_id, question, config=config)  # 初始 'running'，见 db.py

        plan = await make_plan(make_llm(), question)
        result.plan = plan
        trace.event("plan", sub_queries=[
            {"sub_query_id": s.sub_query_id, "question": s.question} for s in plan
        ])

        sem = asyncio.Semaphore(max_concurrency)

        async def worker(sub: SubQuery) -> Finding:
            async with sem:
                try:
                    finding = await research_sub_query(
                        make_llm(), registry, run_id, sub,
                        resolver=fetcher, extracted_by=settings.llm_model,
                    )
                except Exception as e:  # noqa: BLE001 —— 单子任务崩溃只标记该 Finding，不拖垮 run
                    finding = Finding(
                        sub_query_id=sub.sub_query_id, question=sub.question,
                        status=RunStatus.FAILED, error=f"{type(e).__name__}: {e}",
                    )
                await db.save_finding(run_id, finding)
                trace.event(
                    "sub_done", sub_query_id=sub.sub_query_id,
                    status=finding.status.value, claims=len(finding.claims),
                    searches=len(finding.search_queries),
                    fetches=finding.pages_fetched,
                    tokens_in=finding.tokens_in, tokens_out=finding.tokens_out,
                    elapsed_s=finding.elapsed_s,
                )
                return finding

        result.findings = list(await asyncio.gather(*(worker(s) for s in plan)))
        result.total_claims = sum(len(f.claims) for f in result.findings)
        result.tokens_in = sum(f.tokens_in for f in result.findings)
        result.tokens_out = sum(f.tokens_out for f in result.findings)
        # 任一子任务非 OK（DEGRADED 或 FAILED）→ run 至少 DEGRADED
        status = _aggregate_status(result.findings)

        # ---- A2：Verifier + 矛盾检测 + Writer（citation_map 程序化生成）---- #
        if config == "A2":
            all_claims = [c for f in result.findings for c in f.claims]
            if all_claims:
                verifier_llm = make_llm()
                verifications = await verify_claims(
                    verifier_llm, all_claims, checked_by=settings.llm_model)
                contra = await detect_contradictions(
                    verifier_llm, verifications, {c.claim_id: c for c in all_claims})
                for v in verifications:
                    v.contradiction_group = contra.get(v.claim_id)
                trace.event(
                    "verify_done", claims=len(all_claims),
                    supported=sum(v.label.value == "supported" for v in verifications),
                    partial=sum(v.label.value == "partial" for v in verifications),
                    unsupported=sum(v.label.value == "unsupported" for v in verifications),
                    contradiction_groups=len(set(contra.values())),
                )
                report = await write_report(
                    make_llm(), question, result.findings, verifications)
                await db.save_report(run_id, report)
                result.report = report
                trace.event("write_done", cited=len(report.citation_map),
                            refusals=len(report.refusals))
                if not report.citation_map and status is RunStatus.OK:
                    status = RunStatus.DEGRADED  # 有论断但报告零引用：降级如实

        await db.finish_run(run_id, status)
        # 字段名带 total_ 前缀：与 sub_done 的 tokens_* 区分，防 jq 全量求和双计
        trace.event("run_done", status=status.value, claims=result.total_claims,
                    total_tokens_in=result.tokens_in, total_tokens_out=result.tokens_out)
    except Exception as e:
        # 进程内异常：如实记 FAILED；被杀（SIGKILL/OOM）则行停在 'running'——
        # 两种都不再伪装成功，聚合时 WHERE finished_at IS NOT NULL
        await db.finish_run(run_id, RunStatus.FAILED, error=f"{type(e).__name__}: {e}")
        trace.event("run_error", error=f"{type(e).__name__}: {e}"[:200])
        raise
    finally:
        result.elapsed_s = (datetime.now().astimezone() - t0).total_seconds()
        await fetcher.aclose()
        for llm in llms:
            aclose = getattr(llm, "aclose", None)
            if aclose is not None:
                await aclose()
        trace.close()
        if own_db:
            db.close()
    return result
