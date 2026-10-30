"""eval.runner —— 一条命令出评测报告（W5 DoD）。

    python -m eval.runner --dataset eval/dataset/sample.yaml --configs A2 --limit 2

流程：读题 → 逐题跑配置 → judge 判引用与要点 → 组装 EvalRecord
（复现三元组 + 漏斗 + 成对指标）→ 落库 eval_records → 汇总表落盘 eval/reports/。

口径（全部有 schema/测试背书，runner 不允许另起一套）：
- 忠实度 × 覆盖率 × F1 成对输出（防"少写刷忠实度"）；
- 未核验单独成桶，不进通过率（count_labels 单一实现）；
- 覆盖证据 ⊆ citation_map（W5 定死，关闭第二轮审计开放项 ②）；
- verifier_pass_rate 仅对 A2 有意义（A0/A1 无核验阶段，返回 None → 报告显示 n/a）。
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

# prompt 版本拆分（第九轮审计）：生产链路与 judge 各自演进，改一个字各 +1。
# judge 版本随 ClaimJudgment.judge_model 标签（"model#j2"）传递，不占这个字段。
PRODUCER_PROMPT_VERSION = "p1"

A0_PROMPT = """你是研究助手（单 Agent 基线模式）。自行决定是否调用 web_search / fetch_page 完成研究，然后：
1. 从读到的内容中提取 3~10 条论断，每条附逐字引文与来源片段 id；
2. 撰写报告（## 章节，400~800 字），句末用 [[n]] 引用——n 是你 claims 列表的序号（从 1 开始、按列出顺序）；
3. 只输出 JSON 对象：{"claims": [{"text": "...", "quote": "...", "snippet_id": "..."}], "sections": [{"heading": "...", "body": "..."}]}
检索不到的信息明说"未检索到"，不要编造。"""

# A0 的格式修复轮：单 agent 跑完 10 轮 tool 后偶尔不按契约收尾（final answer 不是 JSON）。
# 不给检索机会、只要求把已有答案按契约输出——否则"格式能力"会被记成"研究能力"。
A0_JSON_RETRY = """你的上一条回答不是合法 JSON。不要重新检索，只把上面的最终答案按契约输出：
{"claims": [{"text": "...", "quote": "...", "snippet_id": "..."}], "sections": [{"heading": "...", "body": "..."}]}
除这一个 JSON 对象外，不要输出任何其它文字。"""

# 估算价目（CNY / M tokens，(input, output)）：deepseek-flash 2026-10 牌价估算，
# 以厂商账单为准核对后调整；未登记的模型成本记 0（token 数始终如实）
PRICE_CNY_PER_MTOK: dict[str, tuple[float, float]] = {
    "deepseek-flash": (1.0, 4.0),  # = DeepSeek-V4.1-Flash-1017，producer 链路
    # 备用：若把 judge 换成 deepseek-v4-pro（官方牌价 2026-10，cache miss、非高峰
    # $0.66/M in、$1.98/M out，按与 flash 相同汇率折算 → ¥4.4 / ¥13.2；高峰翻倍）
    "deepseek-v4-pro": (4.4, 13.2),
    # judge：GLM-5.3-Flash 官方牌价 2026-10（¥0.8 / ¥2.8，cache hit ¥0.23）——
    # 不是免费额度；旧 glm-4-flash 已不在模型列表里
    "glm-5.3-flash": (0.8, 2.8),
}


def _safe_print(text: str) -> None:
    """GBK 控制台打 ¥ 会 UnicodeEncodeError → 退出码 1（第十轮真实冒烟 P0：
    DoD"一条命令出报告"的最后一厘米）。降级为 replace 重试，不崩。"""
    import sys

    try:
        print(text)
    except UnicodeEncodeError:
        try:
            sys.stdout.reconfigure(errors="replace")
        except (AttributeError, ValueError):
            pass
        print(text)


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
        # judge 档位独立于 producer：GLM 强制思考，disabled 会 400（见 llm.thinking_body）
        "llm_thinking": settings.judge_thinking,
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

    def make_llm(role: str = "A0"):
        trap = _UsageTrap(llm_factory(), role=role)
        traps.append(trap)
        return trap

    result = ResearchRunResult(run_id=run_id, question=question, config="A0")
    t0 = datetime.now().astimezone()
    try:
        trace.event("run_start", question=question, run_id=run_id, config="A0")
        await db.save_run(run_id, question, "A0")
        lr = await AgentLoop(make_llm(), registry, max_turns=10).run(A0_PROMPT, question)
        result.tokens_in = sum(t.usage_in for t in traps)
        result.tokens_out = sum(t.usage_out for t in traps)
        result.models_seen = {t.role: t.model for t in traps if t.role and t.model}

        obj: dict | None = None
        raws, failures = [], []
        answer = lr.answer or ""
        try:
            obj = extract_json_object(answer)
        except ValueError:
            # 一次"只补格式"的重试（不重新检索）。没有这一步，A0 会因为收尾格式
            # 失败被记成"0 论断"，把格式能力算进研究能力（W6 审计发现）。
            trace.event("a0_json_retry")
            repair = await make_llm().complete(
                [*lr.messages, {"role": "user", "content": A0_JSON_RETRY}])
            answer = repair.content or ""
            result.tokens_in = sum(t.usage_in for t in traps)
            result.tokens_out = sum(t.usage_out for t in traps)
            try:
                retry_ok = bool(extract_json_object(answer))
            except ValueError:
                retry_ok = False
            trace.event("a0_json_retry_done", ok=retry_ok)
        try:
            obj = extract_json_object(answer)
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

    # judge token 单独计量（覆盖判定 payload 含整份报告，不是小数目）
    jtrap = _UsageTrap(judge_llm)
    all_claims = [c for f in result.findings for c in f.claims]
    judgments: list[ClaimJudgment] = []
    hits: list[KeyPointHit] = []
    judge_notes: list[str] = []
    if result.report is not None and all_claims:
        if result.report.citation_map:
            judgments = await judge_cited_claims(
                jtrap, all_claims, result.report, settings.judge_model)
        # 覆盖判定独立于引用判定：报告零引用时检索命中仍可如实评估
        # （漏斗①与写作解耦——"检索 90% 覆盖 40%"的断点信息不能被抹掉）
        hits, judge_notes = await judge_key_points(
            jtrap, q.question, q.key_points, result.report,
            all_claims, settings.judge_model)
    elif result.report is not None:  # 无任何抽取物：检索命中无从谈起
        hits = [KeyPointHit(key_point=kp, covered_by_retrieval=False,
                            covered_in_report=False) for kp in q.key_points]

    status = _aggregate_status(result.findings)
    if result.report is not None and not result.report.citation_map and status is RunStatus.OK:
        status = RunStatus.DEGRADED

    # 成本分拆（第九轮审计）：researcher 按生产模型计价，planner/writer 的
    # strong 模型另一价，judge 再一价；未登记价目的模型计 0（报告里注明）
    researcher_in = sum(f.tokens_in for f in result.findings)
    researcher_out = sum(f.tokens_out for f in result.findings)
    strong = settings.strong_llm_model or settings.llm_model
    cost_cny = (
        estimate_cost(settings.llm_model, researcher_in, researcher_out)
        + estimate_cost(strong,
                        max(result.tokens_in - researcher_in, 0),
                        max(result.tokens_out - researcher_out, 0))
        + estimate_cost(settings.judge_model or "", jtrap.usage_in, jtrap.usage_out)
    )

    return EvalRecord(
        run_id=result.run_id, question_id=q.question_id, config=config,
        code_version=code_version, prompt_version=PRODUCER_PROMPT_VERSION,
        dataset_version=dataset_version,
        # 复现三元组最后一格：resp.model 真身（别名解析后的事实）优先，
        # settings 推导兜底（测试假件不带 model 时）
        model_snapshots={
            role: result.models_seen.get(role) or _model_for(settings, role)
            for role in ("planner", "researcher", "verifier", "writer")
        },
        judge_model=jtrap.model or settings.judge_model or "unknown",
        judge_effort=settings.judge_thinking,
        producer_vendors=[vendor_of(settings.llm_base_url)],
        # base_url 留空时 judge 实际回落到生产 base_url（见 make_judge_llm）——
        # vendor 必须回落成真名，让同厂商跑批被自检抓到，而不是 "unknown" 静默放行
        judge_vendor=vendor_of(settings.judge_base_url or settings.llm_base_url),
        question=q.question, question_type=q.question_type, difficulty=q.difficulty,
        key_points=q.key_points, as_of_date=q.as_of_date,
        findings=result.findings, verifications=result.verifications,
        report=result.report, judgments=judgments, key_point_hits=hits,
        status=status,
        total_tokens_in=result.tokens_in, total_tokens_out=result.tokens_out,
        judge_tokens_in=jtrap.usage_in, judge_tokens_out=jtrap.usage_out,
        judge_notes=judge_notes,
        cost_cny=round(cost_cny, 4),
        wall_seconds=result.elapsed_s,
        search_calls=sum(len(f.search_queries) for f in result.findings),
        fetch_calls=sum(f.pages_fetched for f in result.findings),
        trace_path=f"{settings.trace_dir}/{result.run_id}.jsonl",
    )


def _mean(values: list[float | None]) -> float | None:
    vals = [v for v in values if v is not None]
    return round(sum(vals) / len(vals), 4) if vals else None


def _fmt(v: float | None, pct: bool = False) -> str:
    """缺值印 n/a 而不是 None——"没算"和"算出 0"必须长得不一样。"""
    if v is None:
        return "n/a"
    return f"{v:.0%}" if pct else f"{v:.4g}"


def _dedupe(records: list[EvalRecord]) -> list[EvalRecord]:
    """按 (config, question_id) 保留 run_id 最新的一条（第十四轮 P2）。

    重跑过的题按运行次数加权会稀释失败题的权重——结构防线不依赖
    调用方记得加 --latest-only。
    """
    best: dict[tuple[str, str], EvalRecord] = {}
    order: list[tuple[str, str]] = []
    for r in records:
        key = (str(r.config.value), r.question_id)
        if key not in best:
            order.append(key)
        if key not in best or r.run_id > best[key].run_id:
            best[key] = r
    return [best[k] for k in order]


def _metric_cell(values: list[float | None], pct: bool = False) -> str:
    """均值 + 有效样本数（第十四轮 P1）：None 被跳过时必须可见——
    'n=2 的 1.0' 和 'n=1 的 1.0' 不是同一个数，否则缺失数据偏差
    从跨配置下沉到跨题。"""
    vals = [v for v in values if v is not None]
    if not vals:
        return "n/a"
    base = f"{sum(vals) / len(vals):.0%}" if pct else f"{sum(vals) / len(vals):.4g}"
    return base if len(vals) == len(values) else f"{base}(n={len(vals)})"


def render_report(
    records: list[EvalRecord], dataset_path: str, dataset_version: str,
    n_questions: int, settings: Settings, code_version: str, warnings: list[str],
) -> str:
    ts = datetime.now().astimezone().strftime("%Y-%m-%d %H:%M")
    records = _dedupe(records)  # 第十四轮 P2：去重是渲染层的责任，不靠调用方
    priced_models = {settings.llm_model, settings.strong_llm_model or settings.llm_model,
                     settings.judge_model or ""}
    unpriced = sorted(m for m in priced_models if m and m not in PRICE_CNY_PER_MTOK)
    mixes = sorted({f"{r.judge_model}#{r.judge_effort or '?'}" for r in records})
    mixed_judges = len({(r.judge_model, r.judge_effort) for r in records}) > 1
    codes = sorted({r.code_version for r in records}) or [code_version]
    code_disp = "、".join("`" + c + "`" for c in codes)
    lines = [
        f"# 评测报告 {ts}", "",
        f"- 数据集：`{dataset_path}`（v{dataset_version}，n={n_questions}）"
        f"　配置：{','.join(sorted({str(r.config.value) for r in records}))}"
        f"　code：{code_disp}　producer prompt：{PRODUCER_PROMPT_VERSION}",
        f"- judge：{settings.judge_model or 'unknown'}"
        f"（{vendor_of(settings.judge_base_url or settings.llm_base_url)}）"
        f"　judge 档位：{','.join(sorted({r.judge_effort or '未记录' for r in records}))}"
        f"　producer：{','.join(sorted({v for r in records for v in r.producer_vendors}))}",
        f"- 同源偏差自检：{'；'.join(warnings) if warnings else 'OK（judge 与生产链路跨厂商）'}",
        *([f"- ⚠ 配置混用：{', '.join(mixes)}——同表跨 judge 配置不可比"
           "（加 --latest-only 或整表重跑）"] if mixed_judges else []),
        "",
        "| 配置 | n | 有报告n | 有报告率 | 全绿率 | 忠实度 | 覆盖率 | F1 | 检索命中 | 抽取Σ | 未核验 | 引用率 | tokens(in/out) | 成本¥ | 墙钟s |",
        "|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|",
    ]
    def _rows(rs: list[EvalRecord]) -> list[str]:
        out = []
        for config in sorted({str(r.config.value) for r in rs}):
            per = [r for r in rs if str(r.config.value) == config]
            funnels = [r.funnel() for r in per]
            out.append(
                f"| {config} | {len(per)} "
                f"| {sum(1 for r in per if r.report is not None)} "
                f"| {_fmt(_mean([1.0 if r.report is not None else 0.0 for r in per]), pct=True)} "
                f"| {_fmt(_mean([1.0 if r.succeeded else 0.0 for r in per]), pct=True)} "
                f"| {_metric_cell([r.faithfulness for r in per])} "
                f"| {_metric_cell([r.coverage for r in per])} "
                f"| {_metric_cell([r.f1 for r in per])} "
                f"| {_fmt(_mean([f.retrieval_hit_rate for f in funnels]), pct=True)} "
                f"| {sum(f.claims_extracted for f in funnels)} "
                f"| {sum(f.claims_unverified for f in funnels)} "
                f"| {_fmt(_mean([f.writer_citation_rate for f in funnels]), pct=True)} "
                f"| {sum(r.total_tokens_in for r in per)}+{sum(r.total_tokens_out for r in per)} "
                f"| {sum(r.cost_cny for r in per):.2f} "
                f"| {sum(r.wall_seconds for r in per):.0f} |"
            )
        return out

    configs_all = sorted({str(r.config.value) for r in records})
    lines += _rows(records)
    lines += [
        "",
        "注：有报告率=产出报告的题占比；全绿率=无任何子任务降级/失败的题占比"
        "（degraded 题有报告、有指标，计入有报告率但不计全绿——W6 全表按此口径读）。"
        "均值只在'有值'样本上计算。通过率仅对有核验阶段的配置有意义（A0/A1 显示 n/a）；未核验另计。"
        "成本为估算价（researcher/strong/judge 分档），"
        + (f"未登记价目：{','.join(unpriced)}（计 0 不代表免费）。" if unpriced else "")
        + "judge token 在逐题明细单列。"
        "judge 档位见首行；强制思考的厂商（如 GLM-5.3）下 temperature 仅尽力而为，"
        "确定性以校准重测一致性为准。",
        "",
    ]

    # 共同题集表（第十三轮）：跨配置均值只在"所有配置都有报告"的题上可比——
    # 自有样本均值会奖励失败多的配置（W6 实测：A0 vs A1 忠实度因此反转 5 分）。
    # 这是结构性防线：50 题正式跑批时这个偏差在产物里不可能再悄悄出现。
    if len(configs_all) >= 2:
        by_q: dict[str, set[str]] = {}
        for r in records:
            if r.report is not None:
                by_q.setdefault(r.question_id, set()).add(str(r.config.value))
        common_q = {q for q, cs in by_q.items() if len(cs) == len(configs_all)}
        if common_q:
            common = [r for r in records
                      if r.question_id in common_q and r.report is not None]
            lines += [
                f"### 共同题集（{len(common_q)} 题：所有配置都有报告——跨配置均值以此表为准）",
                "",
                "| 配置 | n | 有报告n | 有报告率 | 全绿率 | 忠实度 | 覆盖率 | F1 | 检索命中 | 抽取Σ | 未核验 | 引用率 | tokens(in/out) | 成本¥ | 墙钟s |",
                "|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|",
            ] + _rows(common) + [""]
        else:
            # 第十四轮 P3：空集必须留痕，不能静默消失
            lines += [
                "", "共同题集为空：没有任何题在所有配置下都有报告——跨配置均值不可比，"
                "跨配置结论只读主表的 有报告率/全绿率 与逐题明细。", "",
            ]

    lines += ["## 逐题明细", ""]
    for r in records:
        flags = ",".join(r.quality_flags()) or "-"
        lines.append(
            f"- **{r.config.value}/{r.question_id}** {r.status.value}"
            f"{' ✅' if r.succeeded else ' ⚠'}"
            f" 忠实度={_fmt(r.faithfulness)} 覆盖率={_fmt(r.coverage)} F1={_fmt(r.f1)}"
            f" judge_tokens={r.judge_tokens_in}+{r.judge_tokens_out}"
            f" | {r.funnel().describe()} | flags: {flags}")
    return "\n".join(lines) + "\n"


def _failed_record(q: EvalQuestion, config: str, code_version: str,
                   dataset_version: str, error: str) -> EvalRecord:
    """单题崩溃时的占位记录：批继续、样本如实进分母（第九轮审计 P0-2）。"""
    return EvalRecord(
        run_id=new_run_id(), question_id=q.question_id, config=config,
        code_version=code_version, prompt_version=PRODUCER_PROMPT_VERSION,
        dataset_version=dataset_version,
        question=q.question, question_type=q.question_type,
        difficulty=q.difficulty, key_points=q.key_points, as_of_date=q.as_of_date,
        status=RunStatus.FAILED, error=error,
    )


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
    jobs: int = 1,
) -> tuple[str, list[EvalRecord]]:
    """批跑 + 持久化 + 汇总。llm/judge 工厂可注入（测试离线）。

    单题失败不中断批（LLM 输出一个 null 不该让几小时的跑批白费）——
    失败题记 FAILED 占位记录进分母；全量落库后可 --report-only 重渲染。
    """
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
    sem = asyncio.Semaphore(max(1, jobs))  # 题间并发（jobs>1 时 50 题×3 组不再 5.5 小时）

    async def one(q: EvalQuestion, config: str) -> EvalRecord:
        async with sem:
            try:
                rec = await build_record(
                    q, config, settings, llm_factory, judge_llm, db, http_client,
                    code_version=code_v, dataset_version=dsv,
                    max_concurrency=max_concurrency)
            except Exception as e:  # noqa: BLE001 —— 单题失败记 FAILED，批继续
                rec = _failed_record(q, config, code_v, dsv,
                                     f"{type(e).__name__}: {e}"[:300])
            await db.save_eval_record(rec)  # 完成即落库：中断后 --report-only 可续
            return rec

    try:
        records = list(await asyncio.gather(
            *(one(q, cfg) for cfg in configs for q in questions)))
    finally:
        judge_aclose = getattr(judge_llm, "aclose", None)
        if judge_aclose is not None:
            await judge_aclose()
        if own_db:
            db.close()
    warnings = sorted({w for r in records for w in r.check_judge_independence()})
    md = render_report(records, dataset_path, dsv, len(questions),
                       settings, code_v, warnings)
    _write_report(md)
    return md, records


def _write_report(md: str) -> Path:
    out_dir = Path("eval/reports")
    out_dir.mkdir(parents=True, exist_ok=True)
    out = out_dir / f"eval-{datetime.now().astimezone().strftime('%Y%m%d%H%M%S')}.md"
    out.write_text(md, encoding="utf-8", newline="")
    return out


def latest_per_question(records: list[EvalRecord]) -> list[EvalRecord]:
    """同一 (config, question) 重跑多次时只留最后一条。

    load_eval_records 按 rowid 升序返回，所以"最后一条"= 最新一次运行。
    重跑单题做冒烟后，不筛就会让同一题在汇总表里出现两行、均值被重复计数。
    """
    keep = {(str(r.config.value), r.question_id): r for r in records}
    return [r for r in records if keep[(str(r.config.value), r.question_id)] is r]


def render_from_db(settings: Settings, dataset_path: str = "(from-db)",
                   latest_only: bool = False, dataset_version: str | None = None) -> str:
    """从 eval_records 表重渲染汇总——跑批中断/补题后不必重跑。

    dataset_version：只取该数据集版本的记录。不筛会把 sample.yaml 与 W6 的
    记录混进同一张表（题号不同、n 变大、judge 配置也跟着混）。
    """
    db = Database(settings.database_path)
    recs = db.load_eval_records()
    db.close()
    if dataset_version:
        recs = [r for r in recs if r.dataset_version == dataset_version]
    if latest_only:
        recs = latest_per_question(recs)
    if not recs:
        raise SystemExit("eval_records 为空：先跑批再渲染")
    warnings = sorted({w for r in recs for w in r.check_judge_independence()})
    md = render_report(recs, dataset_path, recs[0].dataset_version,
                       len({r.question_id for r in recs}),
                       settings, recs[0].code_version, warnings)
    _write_report(md)
    return md


def main() -> None:
    parser = argparse.ArgumentParser(prog="eval.runner", description="评测批跑与汇总（W5）")
    parser.add_argument("--dataset", default="eval/dataset/sample.yaml")
    parser.add_argument("--configs", nargs="+", default=["A2"],
                        choices=["A0", "A1", "A2"])
    parser.add_argument("--limit", type=int, default=None, help="只跑前 N 题（冒烟用）")
    parser.add_argument("--concurrency", type=int, default=4, help="题内 researcher 并发")
    parser.add_argument("--jobs", type=int, default=1, help="题间并发（W6 全矩阵可开 2~3）")
    parser.add_argument("--report-only", action="store_true",
                        help="不跑批：从 eval_records 表重渲染汇总报告")
    parser.add_argument("--latest-only", action="store_true",
                        help="配合 --report-only：同 (config, 题) 只保留最后一次运行")
    parser.add_argument("--dataset-version", default=None,
                        help="配合 --report-only：只渲染该数据集版本的记录（默认取 --dataset 的版本）")
    args = parser.parse_args()
    settings = Settings()
    if args.report_only:
        dsv = args.dataset_version
        if not dsv and Path(args.dataset).exists():
            _, dsv = load_dataset(args.dataset)
        md = render_from_db(settings, args.dataset, latest_only=args.latest_only,
                            dataset_version=dsv)
    else:
        md, records = asyncio.run(run_batch(
            args.dataset, args.configs, settings,
            limit=args.limit, max_concurrency=args.concurrency, jobs=args.jobs))
        _safe_print(f"共 {len(records)} 条 EvalRecord 已落库（eval_records 表）")
    _safe_print(md)
    _safe_print("报告已写入 eval/reports/")


if __name__ == "__main__":
    main()
