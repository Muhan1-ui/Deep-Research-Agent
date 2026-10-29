"""eval.runner —— 一条命令出评测报告（W5 DoD）。

    python -m eval.runner --dataset eval/dataset/sample.yaml --configs A2 --limit 2

流程：读题 → 逐题跑配置 → judge 判引用与要点 → 组装 EvalRecord
（复现三元组 + 漏斗 + 成对指标）→ 落库 eval_records → 汇总表落盘 eval/reports/。

口径（全部有 schema/测试背书，runner 不允许另起一套）：
- 忠实度 × 覆盖率 × F1 成对输出（防"少写刷忠实度"）；
- 未核验单独成桶，不进通过率（count_labels 单一实现）；
- 覆盖证据 ⊆ citation_map（W5 定死，关闭第二轮审计开放项 ②）；
- verifier_pass_rate 仅对 A2 有意义（A0/A1 无核验阶段，漏斗里如实为 0）。
- cost_cny 为估算（价目表见 PRICE_CNY_PER_MTOK），以厂商账单为准。
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import subprocess
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse

import yaml

from src.agent.agents.planner import extract_json_object
from src.agent.agents.researcher import count_tool_usage, parse_raw_claims
from src.agent.agents.writer import assemble_citable, build_report
from src.agent.claims import ResolveFailure, build_claims
from src.agent.config import Settings
from src.agent.llm import LLMClient
from src.agent.obs.trace import JsonlTrace
from src.agent.orchestrator.run import (
    ResearchRunResult,
    _UsageTrap,
    _aggregate_status,
    _build_tools,
    new_run_id,
    run_research,
)
from src.agent.runtime.loop import AgentLoop
from src.agent.schemas import (
    AblationConfig,
    ClaimJudgment,
    EvalQuestion,
    EvalRecord,
    Finding,
    KeyPointHit,
    RunStatus,
)
from src.agent.storage.db import Database

PROMPT_VERSION = "j1"  # judge/覆盖 prompt 版本；改一个字 +1

A0_PROMPT = """你是研究助手（单 Agent 基线模式）。自行决定是否调用 web_search / fetch_page 完成研究，然后：
1. 从读到的内容中提取 3~10 条论断，每条附逐字引文与来源片段 id；
2. 撰写报告（## 章节，400~800 字），句末用 [[n]] 引用——n 是你 claims 列表的序号（从 1 开始、按列出顺序）；
3. 只输出 JSON 对象：{"claims": [{"text": "...", "quote": "...", "snippet_id": "..."}], "sections": [{"heading": "...", "body": "..."}]}
检索不到的信息明说"未检索到"，不要编造。"""

# 估算价目（CNY / M tokens，(input, output)）：deepseek-flash 2026-10 牌价估算，
# 以厂商账单为准核对后调整；未登记的模型成本记 0（token 数始终如实）
PRICE_CNY_PER_MTOK: dict[str, tuple[float, float]] = {
    "deepseek-flash": (1.0, 4.0),
}


def estimate_cost(model: str, tokens_in: int, tokens_out: int) -> float:
    price = PRICE_CNY_PER_MTOK.get(model)
    if not price:
        return 0.0
    return round((tokens_in * price[0] + tokens_out * price[1]) / 1_000_000, 4)


def vendor_of(base_url: str) -> str:
    """从 base_url 提取厂商标识（同源偏差自检用）。api.deepseek.com→deepseek。"""
    parts = (urlparse(base_url).hostname or "").split(".")
    for p in parts:
        if p and p not in {"api", "www", "open", "dashscope"} and not p.isdigit():
            return p
    return "unknown"


def git_version() -> str:
    """git sha（含 -dirty）——复现三元组之一。"""
    try:
        sha = subprocess.run(["git", "rev-parse", "--short", "HEAD"],
                             capture_output=True, text=True, timeout=10).stdout.strip()
        dirty = subprocess.run(["git", "status", "--porcelain"],
                               capture_output=True, text=True, timeout=10).stdout.strip()
        return (sha or "unknown") + ("-dirty" if dirty else "")
    except Exception:  # noqa: BLE001 —— 无 git 环境也要能跑
        return "unknown"


def load_dataset(path: str | Path) -> tuple[list[EvalQuestion], str]:
    raw = Path(path).read_bytes()
    version = hashlib.sha1(raw).hexdigest()[:8]
    data = yaml.safe_load(raw.decode("utf-8"))
    return [EvalQuestion.model_validate(item) for item in data], version


def make_judge_llm(settings: Settings) -> LLMClient:
    """judge 用独立厂商的 key/model：Settings 的 llm_* 槽位被 judge_* 覆写。"""
    if not settings.judge_api_key or not settings.judge_model:
        raise SystemExit(
            "缺少 JUDGE_API_KEY / JUDGE_MODEL：评测 judge 必须与生产链路跨厂商（见 .env.example）")
    judge_settings = settings.model_copy(update={
        "llm_api_key": settings.judge_api_key,
        "llm_base_url": settings.judge_base_url or settings.llm_base_url,
        "llm_model": settings.judge_model,
    })
    return LLMClient(judge_settings)


async def run_a0(
    question: str, settings: Settings, llm_factory, db: Database, http_client=None,
) -> ResearchRunResult:
    """A0 基线：单 Agent 一次 loop（有工具、无分解、无核验）。

    产出复用同一套测量机械：claims 走 build_claims（snippet 句柄→句级锚定），
    报告走 build_report（[[n]] 协议、程序化 citation_map）——模型自列的
    claims 顺序即编号，解析失败的论断编号剥除、refusals 如实记录。
    """
    run_id = new_run_id()
    trace = JsonlTrace(run_id, settings.trace_dir)
    fetcher, registry = _build_tools(settings, db, http_client)
    traps: list[_UsageTrap] = []

    def make_llm():
        trap = _UsageTrap(llm_factory())
        traps.append(trap)
        return trap

    result = ResearchRunResult(run_id=run_id, question=question, config="A0")
    t0 = datetime.now().astimezone()
    try:
        trace.event("run_start", question=question, run_id=run_id, config="A0")
        await db.save_run(run_id, question, "A0")
        lr = await AgentLoop(make_llm(), registry, max_turns=10).run(A0_PROMPT, question)
        result.tokens_in, result.tokens_out = traps[0].usage_in, traps[0].usage_out

        obj: dict | None = None
        raws, failures = [], []
        try:
            obj = extract_json_object(lr.answer or "")
            raws, parse_failures = parse_raw_claims(
                json.dumps(obj.get("claims", []), ensure_ascii=False))
            failures = parse_failures
        except ValueError as e:
            failures = [ResolveFailure(index=0, snippet_id=None,
                                       reason="invalid_shape", detail=str(e)[:200])]
        built = build_claims(run_id, "A0", raws, fetcher, settings.llm_model)
        failures += built.failures
        queries, fetches = count_tool_usage(lr.messages)
        status = (RunStatus.FAILED if not built.claims
                  else RunStatus.DEGRADED if failures else RunStatus.OK)
        finding = Finding(
            sub_query_id="A0", question=question, status=status,
            search_queries=queries, pages_fetched=fetches,
            tokens_in=lr.usage_in, tokens_out=lr.usage_out,
            claims=built.claims,
            error=("; ".join(f"[{f.reason}]idx{f.index}" for f in failures)[:500] or None),
            elapsed_s=round((datetime.now().astimezone() - t0).total_seconds(), 3),
        )
        await db.save_finding(run_id, finding)
        result.findings, result.total_claims = [finding], len(built.claims)

        if built.claims and obj is not None:
            numbered, _ = assemble_citable([finding], [])  # 无核验：全部可引用
            report = build_report(question, numbered, [], obj)
            report.refusals.extend(
                f"A0 论断未解析[{f.reason}] idx={f.index} {f.detail[:80]}" for f in failures)
            await db.save_report(run_id, report)
            result.report = report
            trace.event("write_done", cited=len(report.citation_map))
        status = _aggregate_status(result.findings)
        await db.finish_run(run_id, status)
        trace.event("run_done", status=status.value, claims=result.total_claims,
                    total_tokens_in=result.tokens_in, total_tokens_out=result.tokens_out)
    finally:
        result.elapsed_s = (datetime.now().astimezone() - t0).total_seconds()
        await fetcher.aclose()
        for trap in traps:
            aclose = getattr(trap, "aclose", None)
            if aclose is not None:
                await aclose()
        trace.close()
    return result


def _model_for(settings: Settings, role: str) -> str:
    if role in ("planner", "writer") and settings.strong_llm_model:
        return settings.strong_llm_model
    return settings.llm_model


async def build_record(
    q: EvalQuestion,
    config: str,
    settings: Settings,
    llm_factory,
    judge_llm,
    db: Database,
    http_client=None,
    code_version: str = "unknown",
    dataset_version: str = "unknown",
    max_concurrency: int = 4,
) -> EvalRecord:
    """跑一次 (题目 × 配置) 并组装 EvalRecord——报告里所有数字的唯一来源。"""
    if config == "A0":
        result = await run_a0(q.question, settings, llm_factory, db, http_client)
    else:
        result = await run_research(
            q.question, settings=settings, llm_factory=llm_factory, db=db,
            max_concurrency=max_concurrency, config=config, http_client=http_client)

    from eval.judge.judge import judge_cited_claims, judge_key_points

    all_claims = [c for f in result.findings for c in f.claims]
    judgments: list[ClaimJudgment] = []
    hits: list[KeyPointHit] = []
    if result.report is not None:
        if result.report.citation_map and all_claims:
            judgments = await judge_cited_claims(
                judge_llm, all_claims, result.report, settings.judge_model)
            hits = await judge_key_points(
                judge_llm, q.question, q.key_points, result.report,
                all_claims, settings.judge_model)
        else:  # 有报告无引用：要点如实全记未覆盖（coverage=0，不是 None）
            hits = [KeyPointHit(key_point=kp, covered_by_retrieval=False,
                                covered_in_report=False) for kp in q.key_points]

    status = _aggregate_status(result.findings)
    if result.report is not None and not result.report.citation_map and status is RunStatus.OK:
        status = RunStatus.DEGRADED

    return EvalRecord(
        run_id=result.run_id, question_id=q.question_id, config=config,
        code_version=code_version, prompt_version=PROMPT_VERSION,
        dataset_version=dataset_version,
        model_snapshots={role: _model_for(settings, role)
                         for role in ("planner", "researcher", "verifier", "writer")},
        judge_model=settings.judge_model or "unknown",
        producer_vendors=[vendor_of(settings.llm_base_url)],
        judge_vendor=vendor_of(settings.judge_base_url) if settings.judge_base_url else "unknown",
        question=q.question, question_type=q.question_type, difficulty=q.difficulty,
        key_points=q.key_points, as_of_date=q.as_of_date,
        findings=result.findings, verifications=result.verifications,
        report=result.report, judgments=judgments, key_point_hits=hits,
        status=status,
        total_tokens_in=result.tokens_in, total_tokens_out=result.tokens_out,
        cost_cny=estimate_cost(settings.llm_model, result.tokens_in, result.tokens_out),
        wall_seconds=result.elapsed_s,
        search_calls=sum(len(f.search_queries) for f in result.findings),
        fetch_calls=sum(f.pages_fetched for f in result.findings),
        trace_path=f"{settings.trace_dir}/{result.run_id}.jsonl",
    )


def _mean(values: list[float | None]) -> float | None:
    vals = [v for v in values if v is not None]
    return round(sum(vals) / len(vals), 4) if vals else None


def render_report(
    records: list[EvalRecord], dataset_path: str, dataset_version: str,
    n_questions: int, settings: Settings, code_version: str, warnings: list[str],
) -> str:
    ts = datetime.now().astimezone().strftime("%Y-%m-%d %H:%M")
    lines = [
        f"# 评测报告 {ts}", "",
        f"- 数据集：`{dataset_path}`（v{dataset_version}，n={n_questions}）"
        f"　配置：{','.join(sorted({r.config for r in records}))}"
        f"　code：`{code_version}`　prompt：{PROMPT_VERSION}",
        f"- judge：{settings.judge_model or 'unknown'}"
        f"（{settings.judge_base_url and vendor_of(settings.judge_base_url) or 'unknown'}）"
        f"　producer：{','.join(sorted({v for r in records for v in r.producer_vendors}))}",
        f"- 同源偏差自检：{'；'.join(warnings) if warnings else 'OK（judge 与生产链路跨厂商）'}",
        "",
        "| 配置 | n | 成功率 | 忠实度 | 覆盖率 | F1 | 检索命中 | 抽取Σ | 未核验 | 引用率 | tokens(in/out) | 成本¥ | 墙钟s |",
        "|---|---|---|---|---|---|---|---|---|---|---|---|---|",
    ]
    for config in sorted({str(r.config.value) for r in records}):
        rs = [r for r in records if str(r.config.value) == config]
        funnels = [r.funnel() for r in rs]
        lines.append(
            f"| {config} | {len(rs)} "
            f"| {_mean([1.0 if r.succeeded else 0.0 for r in rs]):.0%} "
            f"| {_mean([r.faithfulness for r in rs])} "
            f"| {_mean([r.coverage for r in rs])} "
            f"| {_mean([r.f1 for r in rs])} "
            f"| {_mean([f.retrieval_hit_rate for f in funnels]):.0%} "
            f"| {sum(f.claims_extracted for f in funnels)} "
            f"| {sum(f.claims_unverified for f in funnels)} "
            f"| {_mean([f.writer_citation_rate for f in funnels]):.0%} "
            f"| {sum(r.total_tokens_in for r in rs)}+{sum(r.total_tokens_out for r in rs)} "
            f"| {sum(r.cost_cny for r in rs):.2f} "
            f"| {sum(r.wall_seconds for r in rs):.0f} |"
        )
    lines += ["", "通过率/未核验仅对 A2 有意义（A0/A1 无核验阶段）。成本为估算价。", "",
              "## 逐题明细", ""]
    for r in records:
        flags = ",".join(r.quality_flags()) or "-"
        lines.append(
            f"- **{r.config.value}/{r.question_id}** {r.status.value}"
            f"{' ✅' if r.succeeded else ' ⚠'}"
            f" 忠实度={r.faithfulness} 覆盖率={r.coverage} F1={r.f1}"
            f" | {r.funnel().describe()} | flags: {flags}")
    return "\n".join(lines) + "\n"


async def run_batch(
    dataset_path: str,
    configs: list[str],
    settings: Settings | None = None,
    limit: int | None = None,
    max_concurrency: int = 4,
    llm_factory=None,
    judge_factory=None,
    db: Database | None = None,
    http_client=None,
) -> tuple[str, list[EvalRecord]]:
    """批跑 + 持久化 + 汇总。llm/judge 工厂可注入（测试离线）。"""
    settings = settings or Settings()
    if not settings.llm_api_key:
        raise SystemExit("缺少 LLM_API_KEY")
    for config in configs:
        if config not in AblationConfig.__members__:
            raise SystemExit(f"未知配置 {config}（可选 A0/A1/A2）")
    questions, dsv = load_dataset(dataset_path)
    if limit:
        questions = questions[:limit]
    if not questions:
        raise SystemExit("评测集为空")
    llm_factory = llm_factory or (lambda: LLMClient(settings))
    judge_llm = (judge_factory or make_judge_llm)(settings)
    own_db = db is None
    db = db or Database(settings.database_path)

    code_v = git_version()
    records: list[EvalRecord] = []
    warnings: set[str] = set()
    try:
        for config in configs:
            for q in questions:
                rec = await build_record(
                    q, config, settings, llm_factory, judge_llm, db, http_client,
                    code_version=code_v, dataset_version=dsv,
                    max_concurrency=max_concurrency)
                await db.save_eval_record(rec)
                records.append(rec)
                warnings.update(rec.check_judge_independence())
    finally:
        if own_db:
            db.close()
    md = render_report(records, dataset_path, dsv, len(questions),
                       settings, code_v, sorted(warnings))
    out_dir = Path("eval/reports")
    out_dir.mkdir(parents=True, exist_ok=True)
    out = out_dir / f"eval-{datetime.now().astimezone().strftime('%Y%m%d%H%M%S')}.md"
    out.write_text(md, encoding="utf-8", newline="")
    return md, records


def main() -> None:
    parser = argparse.ArgumentParser(prog="eval.runner", description="评测批跑与汇总（W5）")
    parser.add_argument("--dataset", default="eval/dataset/sample.yaml")
    parser.add_argument("--configs", nargs="+", default=["A2"],
                        choices=["A0", "A1", "A2"])
    parser.add_argument("--limit", type=int, default=None, help="只跑前 N 题（冒烟用）")
    parser.add_argument("--concurrency", type=int, default=4)
    args = parser.parse_args()
    md, records = asyncio.run(run_batch(
        args.dataset, args.configs, Settings(),
        limit=args.limit, max_concurrency=args.concurrency))
    print(md)
    print(f"共 {len(records)} 条 EvalRecord 已落库（eval_records 表），报告见 eval/reports/")


if __name__ == "__main__":
    main()
