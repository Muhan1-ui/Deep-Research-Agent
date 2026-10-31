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
from src.agent.schemas import Finding, RunStatus, Verification, count_labels
from src.agent.storage.db import Database
from src.agent.tools.base import Tool, ToolRegistry
from src.agent.tools.fetch import PageFetcher
from src.agent.tools.pages import FetchPageArgs
from src.agent.tools.search import build_search_tool


class _UsageTrap:
    """包装 LLM：累计 usage——planner/verifier/writer 的 token 也进 run 总账。

    之前的总账只数 researcher（findings 求和），规划与写作的成本系统性失踪。
    同时捕获 resp.model：deepseek-flash 是别名，会随版本重新指向——
    复现三元组里真正钉死版本的是响应里的实际模型 id（第十一轮审计）。
    __getattr__ 透传其余属性（aclose 等）。
    """

    def __init__(self, inner, role: str = ""):
        self._inner = inner
        self.role = role
        self.usage_in = 0
        self.usage_out = 0
        self.model = ""  # 该角色实际拿到的 resp.model（别名解析后的真身）

    async def complete(self, messages, tools=None, **kwargs):
        r = await self._inner.complete(messages, tools, **kwargs)
        self.usage_in += getattr(r, "usage_in", 0) or 0
        self.usage_out += getattr(r, "usage_out", 0) or 0
        if getattr(r, "model", ""):
            self.model = r.model
        return r

    def __getattr__(self, name):
        return getattr(self._inner, name)


def _build_tools(settings: Settings, db: Database, http_client=None, mock: bool = False):
    """共享工具构建：run_research 与 eval.runner 的 A0 基线用同一套接线。

    mock=True 只替换工具的**外部实现**（搜索固定结果、抓取走 MockTransport），
    返回的 fetcher 仍是 PageFetcher 的实例，claims 解析 / 落库 / 锚定路径不变。
    """
    registry = ToolRegistry()
    if mock:
        from src.agent.mock import build_mock_fetch_tool, build_mock_search_tool
        fetcher, fetch_tool = build_mock_fetch_tool(settings.pages_dir, on_page=db.save_page)
        registry.register(build_mock_search_tool())
        registry.register(fetch_tool)
        return fetcher, registry
    fetcher = PageFetcher(settings.pages_dir, client=http_client, on_page=db.save_page)
    if settings.tavily_api_key:
        registry.register(build_search_tool(settings.tavily_api_key))
    registry.register(Tool(  # 共享同一个 fetcher：claims 解析 + 落库都靠它
        name="fetch_page",
        description="读取 URL 正文，返回与 query 最相关的片段（带 id）。",
        args_model=FetchPageArgs,
        handler=fetcher,
    ))
    return fetcher, registry


@dataclass
class ResearchRunResult:
    run_id: str
    question: str
    config: str = "A1"
    plan: list[SubQuery] = field(default_factory=list)
    findings: list[Finding] = field(default_factory=list)
    verifications: list["Verification"] = field(default_factory=list)  # 仅 A2 非空
    # {角色: 实际 resp.model}——别名（deepseek-flash）解析后的真身
    # （如 DeepSeek-V4.1-Flash-1017），复现三元组的最后一格
    models_seen: dict[str, str] = field(default_factory=dict)
    total_claims: int = 0
    total_failures_degraded: int = 0  # DEGRADED 子任务数（抽取不全）
    tokens_in: int = 0
    tokens_out: int = 0
    report: object | None = None
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
    mock: bool | None = None,
) -> ResearchRunResult:
    if config not in ("A1", "A2"):  # A0 走 ask 命令
        raise SystemExit(f"未知配置 {config}（research 支持 A1/A2）")
    settings = settings or Settings()
    # 显式参数优先于环境变量/配置（CLI 只在给了 --mock 时传 True）
    use_mock = settings.mock if mock is None else mock
    if not use_mock and not settings.llm_api_key:
        raise SystemExit("缺少 LLM_API_KEY：复制 .env.example 为 .env 并填写")
    if llm_factory is None:
        if use_mock:
            from src.agent.mock import MockLLM
            llm_factory = MockLLM
        else:
            llm_factory = lambda: LLMClient(settings)  # noqa: E731
    # mock 模式下如实标注抽取/核验来源：model 名是 mock，不是真实的 llm_model
    engine = "mock" if use_mock else settings.llm_model
    own_db = db is None
    db = db or Database(settings.database_path)

    run_id = new_run_id()
    trace = JsonlTrace(run_id, settings.trace_dir)
    fetcher, registry = _build_tools(settings, db, http_client, mock=use_mock)
    # llm 生命周期：工厂每产一个就登记一个，finally 统一 aclose——
    # 否则批跑几百次 run，每个 worker 一个连接池，fd 会耗尽
    traps: list[_UsageTrap] = []

    def make_llm(role: str):
        trap = _UsageTrap(llm_factory(), role=role)
        traps.append(trap)
        return trap

    t0 = datetime.now().astimezone()
    result = ResearchRunResult(run_id=run_id, question=question, config=config)
    try:
        trace.event("run_start", question=question, run_id=run_id, config=config,
                    mock=use_mock)
        await db.save_run(run_id, question, config=config)  # 初始 'running'，见 db.py

        plan = await make_plan(make_llm("planner"), question)
        result.plan = plan
        trace.event("plan", sub_queries=[
            {"sub_query_id": s.sub_query_id, "question": s.question} for s in plan
        ])

        sem = asyncio.Semaphore(max_concurrency)

        async def worker(sub: SubQuery) -> Finding:
            async with sem:
                try:
                    finding = await research_sub_query(
                        make_llm("researcher"), registry, run_id, sub,
                        resolver=fetcher, extracted_by=engine,
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
                    # 诊断元数据：配额/网络/key 失效的判别证据进 trace（第十六轮）
                    last_tool_error=(finding.last_tool_error or "")[:200],
                )
                return finding

        result.findings = list(await asyncio.gather(*(worker(s) for s in plan)))
        result.total_claims = sum(len(f.claims) for f in result.findings)
        # 任一子任务非 OK（DEGRADED 或 FAILED）→ run 至少 DEGRADED
        status = _aggregate_status(result.findings)

        # ---- Verifier（仅 A2）+ Writer（A1/A2 都要报告，否则消融没有可比产物）---- #
        all_claims = [c for f in result.findings for c in f.claims]
        if config == "A2" and all_claims:
            verifier_llm = make_llm("verifier")
            result.verifications = await verify_claims(
                verifier_llm, all_claims, checked_by=engine)
            contra = await detect_contradictions(
                verifier_llm, result.verifications, {c.claim_id: c for c in all_claims})
            for v in result.verifications:
                v.contradiction_group = contra.get(v.claim_id)
            counts = count_labels(result.verifications)
            trace.event(
                "verify_done", claims=len(all_claims),
                # 与 funnel() 共用 count_labels：未核验单独报、不计入 partial，
                # "免费看板"的通过率与评测口径不可能再分叉
                supported=counts["supported"], partial=counts["partial"],
                unsupported=counts["unsupported"], unverified=counts["unverified"],
                contradiction_groups=len(set(contra.values())),
            )
        # Writer：A1 无核验 → assemble_citable 视全部论断可引用；A2 只引非 unsupported
        report = await write_report(make_llm("writer"), question, result.findings, result.verifications)
        await db.save_report(run_id, report)
        result.report = report
        trace.event("write_done", cited=len(report.citation_map),
                    refusals=len(report.refusals), verified=len(result.verifications),
                    deduped=report.deduped)
        if not report.citation_map and status is RunStatus.OK:
            status = RunStatus.DEGRADED  # 有论断但报告零引用：降级如实

        # token 总账必须在 writer 之后求和（第九轮审计 P0-1：提前求和会让
        # verifier/writer 的 trap 尚未产生用量就进汇总——治好一半的失踪病）
        result.tokens_in = sum(t.usage_in for t in traps)
        result.tokens_out = sum(t.usage_out for t in traps)
        # resp.model 真身按角色记录（别名解析后的事实；测试假件可能为空→角色缺席）
        result.models_seen = {t.role: t.model for t in traps if t.role and t.model}

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
        for trap in traps:
            aclose = getattr(trap, "aclose", None)
            if aclose is not None:
                await aclose()
        trace.close()
        if own_db:
            db.close()
    return result
