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
    plan: list[SubQuery] = field(default_factory=list)
    findings: list[Finding] = field(default_factory=list)
    total_claims: int = 0
    total_failures_degraded: int = 0  # DEGRADED 子任务数（抽取不全）
    elapsed_s: float = 0.0

    def summary_line(self) -> str:
        ok = sum(f.status is RunStatus.OK for f in self.findings)
        return (
            f"run={self.run_id} 子问题={len(self.plan)} "
            f"findings OK/DEGRADED/FAILED={ok}/"
            f"{sum(f.status is RunStatus.DEGRADED for f in self.findings)}/"
            f"{sum(f.status is RunStatus.FAILED for f in self.findings)} "
            f"claims={self.total_claims} 耗时={self.elapsed_s:.1f}s"
        )


def new_run_id() -> str:
    return f"run-{datetime.now().astimezone().strftime('%Y%m%d%H%M%S')}-{uuid.uuid4().hex[:6]}"


async def run_research(
    question: str,
    settings: Settings | None = None,
    llm_factory: Callable[[], object] | None = None,
    db: Database | None = None,
    max_concurrency: int = 4,
) -> ResearchRunResult:
    settings = settings or Settings()
    if not settings.llm_api_key:
        raise SystemExit("缺少 LLM_API_KEY：复制 .env.example 为 .env 并填写")
    llm_factory = llm_factory or (lambda: LLMClient(settings))
    own_db = db is None
    db = db or Database(settings.database_path)

    run_id = new_run_id()
    trace = JsonlTrace(run_id, settings.trace_dir)
    fetcher = PageFetcher(settings.pages_dir, on_page=db.save_page)
    registry = ToolRegistry()
    if settings.tavily_api_key:
        registry.register(build_search_tool(settings.tavily_api_key))
    registry.register(Tool(  # 共享同一个 fetcher：claims 解析 + 落库都靠它
        name="fetch_page",
        description="读取 URL 正文，返回与 query 最相关的片段（带 id）。",
        args_model=FetchPageArgs,
        handler=fetcher,
    ))

    t0 = datetime.now().astimezone()
    result = ResearchRunResult(run_id=run_id, question=question)
    try:
        trace.event("run_start", question=question, run_id=run_id)
        await db.save_run(run_id, question, config="A1")

        plan = await make_plan(llm_factory(), question)
        result.plan = plan
        trace.event("plan", sub_queries=[
            {"sub_query_id": s.sub_query_id, "question": s.question} for s in plan
        ])

        sem = asyncio.Semaphore(max_concurrency)

        async def worker(sub: SubQuery) -> Finding:
            async with sem:
                try:
                    finding = await research_sub_query(
                        llm_factory(), registry, run_id, sub,
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
                    fetches=finding.pages_fetched, elapsed_s=finding.elapsed_s,
                )
                return finding

        result.findings = list(await asyncio.gather(*(worker(s) for s in plan)))
        result.total_claims = sum(len(f.claims) for f in result.findings)
        result.total_failures_degraded = sum(
            f.status is RunStatus.DEGRADED for f in result.findings
        )
        status = RunStatus.FAILED if not any(
            f.claims for f in result.findings
        ) else RunStatus.DEGRADED if result.total_failures_degraded else RunStatus.OK
        await db.finish_run(run_id, status)
        trace.event("run_done", status=status.value, claims=result.total_claims)
    finally:
        result.elapsed_s = (datetime.now().astimezone() - t0).total_seconds()
        await fetcher.aclose()
        trace.close()
        if own_db:
            db.close()
    return result
