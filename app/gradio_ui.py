"""Gradio 演示前端：规划 → 并行检索 → 核验 → 带引用报告。

运行：
    python app/gradio_ui.py          # 真实链路（需 .env 里的 key）
    python app/gradio_ui.py --mock   # 演示模式：固定资料，不联网、不消耗额度
    MOCK=1 python app/gradio_ui.py   # 同上，用环境变量选择

本文件只做展示：所有判定、计数、引用关系都来自 src/ 的返回值，前端不重算。
布局照设计稿：左栏（标题/输入/计划/指标）+ 右栏（报告/核验表/来源）。
"""

from __future__ import annotations

import argparse
import asyncio
import html
import json
import os
import re
import sqlite3
import sys
import threading
import time
from pathlib import Path

import gradio as gr

# 直接运行 `python app/gradio_ui.py` 时 sys.path[0] 是 app/，补上仓库根目录，
# 否则 `import src` 会失败（不依赖 PYTHONPATH / editable install）。
_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from src.agent.config import Settings  # noqa: E402
from src.agent.orchestrator.run import run_research  # noqa: E402
from src.agent.schemas import REFERENCE_HEADING, Claim, ReportArtifact  # noqa: E402

CONFIG_MAP = {"A1 · 不核验": "A1", "A2 · 完整系统": "A2"}
MOCK_BANNER = (
    "演示模式（mock）：使用固定资料，不联网、不消耗额度 · 为便于观察已放慢渲染节奏"
)

CSS = """
<style>
.gradio-container,
.gradio-container button,
.gradio-container input,
.gradio-container textarea {
    font-family: Inter, "Microsoft YaHei", "PingFang SC", "Noto Sans SC", system-ui, sans-serif;
}
body { line-height: 1.75; overflow-y: scroll; }
.gradio-container { line-height: 1.75; }
.gradio-container {
    width: 100% !important;
    max-width: 1480px !important;
    margin: 0 auto !important;
    padding-left: 16px !important;
    padding-right: 16px !important;
}
/* 外层若还有 max-width / 居中约束，一并放开，让内容铺满窗口 */
.gradio-container > .main,
.gradio-container > .main > .wrap {
    max-width: 100% !important;
    width: 100% !important;
}

/* Gradio 默认底部链接条 */
footer, .footer { display: none !important; }

/* 主色兜底：主题 primary_hue 之外再钉死一次 */
.primary, button.primary {
    background: #4F46E5 !important;
    border-color: #4F46E5 !important;
    color: #fff !important;
}

.num { font-variant-numeric: tabular-nums; }

.card {
    border: 1px solid #E5E7EB;
    border-radius: 8px;
    padding: 14px 16px;
    background: #fff;
    margin-bottom: 12px;
}

/* Gradio 6.30 的 .prose * { color } 会盖掉这些胶囊色，必须 !important */
.cite {
    display: inline-block;
    padding: 0 5px;
    border-radius: 4px;
    background: #EEF2FF;
    color: #4F46E5 !important;
    font-size: 11px;
    font-variant-numeric: tabular-nums;
}
.ok { background: #ECFDF5; color: #047857 !important; }
.partial { background: #FFFBEB; color: #B45309 !important; }
.bad { background: #FEF2F2; color: #B91C1C !important; }
.unknown { background: #F3F4F6; color: #4B5563 !important; }

.pill {
    display: inline-block;
    padding: 1px 8px;
    border-radius: 999px;
    font-size: 12px;
    font-weight: 500;
    white-space: nowrap;
}

.url, .source-url { word-break: break-all; }

#dr-title { padding: 2px 2px 10px; }
.dr-title { font-size: 18px; font-weight: 700; color: #111827; letter-spacing: .01em; }
.dr-sub { font-size: 12px; color: #6B7280; margin-top: 2px; }

.card-title { font-size: 13px; font-weight: 600; color: #374151; margin-bottom: 8px; }

.plan-row {
    display: flex;
    align-items: center;
    gap: 8px;
    padding: 7px 0;
    border-top: 1px dashed #F3F4F6;
}
.plan-row:first-of-type { border-top: 0; padding-top: 0; }
.dot {
    width: 14px;
    height: 14px;
    border-radius: 50%;
    flex: 0 0 14px;
    background: #D1D5DB;
    display: inline-flex;
    align-items: center;
    justify-content: center;
    font-size: 9px;
    font-weight: 700;
    color: #fff;
    line-height: 1;
}
.dot.ok { background: #10B981; }
.dot.warn { background: #F59E0B; }
.dot.bad { background: #EF4444; }
.dot.ok::after { content: "✓"; }
.dot.warn::after { content: "!"; }
.dot.bad::after { content: "×"; }
.plan-q {
    flex: 1;
    min-width: 0;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
    font-size: 13px;
    color: #1F2937;
}
.plan-meta { flex: none; font-size: 12px; color: #6B7280; white-space: nowrap; }
.plan-elapsed { color: #9CA3AF; margin-left: 4px; }
.verify-summary { font-size: 12px; color: #6B7280; margin-bottom: 8px; }

.metrics { display: grid; grid-template-columns: 1fr 1fr; gap: 8px; }
.metric { background: #F9FAFB; border: 1px solid #F1F5F9; border-radius: 6px; padding: 10px 12px; }
.metric-num { font-size: 20px; font-weight: 600; color: #111827 !important; line-height: 1.2; font-variant-numeric: tabular-nums; }
.metric-cap { font-size: 11px; color: #9CA3AF !important; margin-top: 2px; }

.report h3 { font-size: 14px; color: #111827; margin: 14px 0 6px; }
.report h3:first-child { margin-top: 0; }
.report p { margin: 0 0 8px; line-height: 1.75; font-size: 14px; color: #1F2937; }
.empty { font-size: 13px; color: #9CA3AF; line-height: 1.75; }
.alert { font-size: 13px; line-height: 1.75; }

table.verify { width: 100%; border-collapse: collapse; font-size: 13px; }
table.verify th, table.verify td {
    border: 1px solid #E5E7EB !important;
    padding: 8px 10px;
    text-align: left;
    white-space: normal !important;
    word-break: break-word;
}
table.verify th { background: #F9FAFB !important; color: #6B7280 !important; font-weight: 600; }
table.verify td { vertical-align: top; line-height: 1.6; }
table.verify td:last-child { width: 30%; }
table.verify a { color: #4F46E5; text-decoration: none; }

.sources { margin: 0; padding-left: 20px; }
.sources li { margin-bottom: 8px; line-height: 1.6; }
/* 同一来源的多个引用编号各自成胶囊，不能挤成“123” */
.sources .cite { margin-right: 4px; }
.sources .source-title { font-size: 13px; color: #111827; text-decoration: none; }
.sources .source-url { font-size: 12px; color: #6B7280; }
.sources a:hover { text-decoration: underline; }

.mock-banner {
    background: #EEF2FF;
    color: #3730A3;
    border: 1px solid #C7D2FE;
    border-radius: 8px;
    padding: 8px 12px;
    font-size: 13px;
    margin-bottom: 12px;
}

#input-card {
    border: 1px solid #E5E7EB !important;
    border-radius: 8px;
    padding: 14px 16px;
    background: #fff;
    margin-bottom: 12px;
}
#question-box textarea { line-height: 1.75; }

/* 去掉 gr.Group 外层自带的容器边框，只留内层卡片边框 */
.card-flat {
    border: none !important;
    background: transparent !important;
    padding: 0 !important;
}

/* Gradio 6 的 Group 会把 elem_id 同时挂到内外两层，内层只保留布局 */
#input-card > #input-card {
    border: 0 !important;
    padding: 0 !important;
    margin: 0 !important;
    border-radius: 0 !important;
    background: transparent !important;
}
/* Group 用灰色底 + 1px 间隙画分隔线；卡片内改回透明并给正常间距 */
#input-card .styler,
#input-card .form { background: transparent !important; gap: 10px; }

/* A1/A2：横排胶囊分段控件 */
#config-radio { margin-bottom: 0; }
#config-radio > .wrap:not([data-testid]) { display: flex; flex-wrap: wrap; gap: 8px; }
#config-radio label {
    display: inline-flex;
    align-items: center;
    margin: 0 !important;
    padding: 5px 14px;
    border: 1px solid #E5E7EB;
    border-radius: 999px;
    background: #fff;
    font-size: 13px;
    cursor: pointer;
    transition: background .15s, border-color .15s, color .15s;
}
#config-radio input[type="radio"] { display: none !important; }
#config-radio label:has(input:checked),
#config-radio label.selected {
    background: #EEF2FF;
    border-color: #C7D2FE;
    color: #4F46E5;
}
#config-radio label:has(input:checked) span,
#config-radio label.selected span { color: #4F46E5 !important; }

/* 输入区 label：普通小灰字，覆盖 Gradio 默认的靛蓝胶囊样式 */
label > span, .label-wrap span, .label-wrap > span, [data-testid="block-info"] {
    background: transparent !important;
    color: #6B7280 !important;
    font-size: 12px !important;
    font-weight: 500 !important;
    border-radius: 0 !important;
    padding: 0 !important;
}

/* 右栏自然高度：整页滚动，不在列内再套滚动条 */
.main-row { align-items: flex-start !important; }
.right-col {
    display: flex;
    flex-direction: column;
    gap: 16px;
}
/* 右栏三个 HTML 块按内容撑开，核验表/来源卡显示全部行 */
.panel-report,
.panel-verify,
.panel-sources {
    display: block;
    flex: none !important;
}
.right-col .html-container,
.right-col .prose {
    overflow: visible !important;
    max-height: none !important;
}
.right-col .card { margin-bottom: 0; }

#right-panel { gap: 16px !important; }
#right-panel .card { margin-bottom: 0; }
/* gr.HTML 自带 10px 上下内边距，会把卡片间距撑到 28px；右栏压平 */
#right-panel .html-container { padding: 0 !important; }
</style>
"""


# --------------------------------------------------------------------------- #
# 渲染工具（只读 result 的返回值，不做任何业务计算）
# --------------------------------------------------------------------------- #
def _esc(text: object) -> str:
    return html.escape(str(text), quote=True)


def _card(title: str, body: str) -> str:
    return f'<div class="card"><div class="card-title">{_esc(title)}</div>{body}</div>'


def _title_html() -> str:
    return (
        '<div id="dr-title">'
        '<div class="dr-title">深度研究</div>'
        '<div class="dr-sub">规划 → 并行检索 → 核验 → 带引用报告</div>'
        "</div>"
    )


def _plan_html(result=None) -> str:
    if result is None or not result.plan:
        rows = [
            f'<div class="plan-row"><span class="dot"></span>'
            f'<span class="plan-q">待规划：子问题 {i}</span>'
            f'<span class="plan-meta"><span class="num">—</span> 条论断</span></div>'
            for i in range(1, 4)
        ]
        return _card("研究计划", "".join(rows))

    findings = {f.sub_query_id: f for f in result.findings}
    rows = []
    for sub in result.plan:
        finding = findings.get(sub.sub_query_id)
        dot = ""
        if finding is not None:
            dot = {"ok": "ok", "degraded": "warn", "failed": "bad"}.get(
                finding.status.value, ""
            )
        n_claims = len(finding.claims) if finding is not None else 0
        question_text = _esc(sub.question)
        rows.append(
            f'<div class="plan-row"><span class="dot {dot}"></span>'
            f'<span class="plan-q" title="{question_text}">{question_text}</span>'
            f'<span class="plan-meta"><span class="num">{n_claims}</span> 条论断</span></div>'
        )
    return _card("研究计划", "".join(rows))


def _plan_progress_html(plan: list[dict] | None, subs: dict[str, dict]) -> str:
    """流式版计划卡：plan 事件后三行灰点，sub_done 后逐行变绿并补上条数与耗时。

    只读 trace 字段，不推断：没有 sub_done 的行保持灰色 + “— 条论断”。
    """
    if not plan:
        rows = [
            f'<div class="plan-row"><span class="dot"></span>'
            f'<span class="plan-q">待规划：子问题 {i}</span>'
            f'<span class="plan-meta"><span class="num">—</span> 条论断</span></div>'
            for i in range(1, 4)
        ]
        return _card("研究计划", "".join(rows))

    rows = []
    for sub in plan:
        sqid = str(sub.get("sub_query_id", ""))
        info = subs.get(sqid)
        dot = ""
        meta = '<span class="num">—</span> 条论断'
        if info is not None:
            dot = {"ok": "ok", "degraded": "warn", "failed": "bad"}.get(
                str(info.get("status", "")), ""
            )
            meta = f'<span class="num">{int(info.get("claims", 0) or 0)}</span> 条论断'
            elapsed = info.get("elapsed_s")
            if elapsed is not None:
                meta += f' <span class="plan-elapsed num">{float(elapsed):.1f}s</span>'
        question_text = _esc(sub.get("question", ""))
        rows.append(
            f'<div class="plan-row"><span class="dot {dot}"></span>'
            f'<span class="plan-q" title="{question_text}">{question_text}</span>'
            f'<span class="plan-meta">{meta}</span></div>'
        )
    return _card("研究计划", "".join(rows))


def _metric_html(value: str, unit: str) -> str:
    return (
        '<div class="metric">'
        f'<div class="metric-num num">{_esc(value)}</div>'
        f'<div class="metric-cap">{_esc(unit)}</div>'
        "</div>"
    )


def _metrics_values_html(values: list[tuple[str, str]]) -> str:
    cells = "".join(_metric_html(v, u) for v, u in values)
    return f'<div class="card"><div class="card-title">运行指标</div><div class="metrics">{cells}</div></div>'


def _token_caption(mock: bool) -> str:
    """mock 模式不消耗额度，token 恒为 0，说明里要写清楚否则像统计坏了。"""
    return "token（mock 不计）" if mock else "token"


def _metrics_html(result=None, mock: bool = False) -> str:
    caption = _token_caption(mock)
    if result is None:
        # 未运行时一律显示占位符：不允许出现看起来像真实结果的数字。
        values = [("—", "条论断"), ("—", "条引用"), ("—", caption), ("—", "秒")]
    else:
        cited = len(result.report.citation_map) if result.report is not None else 0
        tokens = result.tokens_in + result.tokens_out
        values = [
            (str(result.total_claims), "条论断"),
            (str(cited), "条引用"),
            (str(tokens), caption),
            (f"{result.elapsed_s:.1f}", "秒"),
        ]
    return _metrics_values_html(values)


def _rich_body(body: str, citation_map: dict[int, str]) -> str:
    """纯文本 → 段落 HTML；[n] 只在 n 确实在 citation_map 里时才变胶囊。"""
    text = _esc(body)
    valid = {int(n) for n in citation_map}

    def _cite(m: re.Match) -> str:
        n = int(m.group(1))
        return f'<span class="cite">{n}</span>' if n in valid else m.group(0)

    text = re.sub(r"\[(\d+)\]", _cite, text)
    paragraphs = [p.strip() for p in re.split(r"\n\s*\n", text) if p.strip()]
    return "".join(f"<p>{p.replace(chr(10), '<br>')}</p>" for p in paragraphs)


def _report_card(report=None) -> str:
    if report is None:
        return _card("研究报告", '<div class="empty">运行后在此显示带引用的报告正文。</div>')

    parts = []
    for section in report.sections:
        if section.heading == REFERENCE_HEADING:
            continue  # 来源单独成块，避免正文区重复
        parts.append(f"<h3>{_esc(section.heading)}</h3>")
        parts.append(_rich_body(section.body, report.citation_map))

    body = "".join(parts) or '<div class="empty">报告暂无正文。</div>'
    return _card("研究报告", f'<div class="report">{body}</div>')


def _report_html(result=None) -> str:
    if result is None or result.report is None:
        return _report_card(None)
    return _report_card(result.report)


def _error_report(message: str) -> str:
    return _card(
        "研究报告",
        f'<div class="alert bad" style="padding:8px 10px;border-radius:6px;">{_esc(message)}</div>',
    )


def _verify_card(
    claims: list[Claim],
    verifications: list | None = None,
    counts: dict | None = None,
) -> str:
    """核验表。verifications=None 表示「核验已结束但逐条判决尚未取到」——
    此时只给中性「已核验」胶囊 + 顶部聚合计数，绝不猜某条属于哪个桶；
    run_done 拿到 result 后会用真实判决重绘。"""
    header = "<tr><th>论断</th><th>状态</th><th>来源</th></tr>"
    if not claims:
        body = '<tr><td colspan="3" class="empty">运行后显示逐条论断的核验结果。</td></tr>'
        return _card("论断核验", f'<table class="verify">{header}{body}</table>')

    labels = {
        "supported": ("ok", "已支撑"),
        "partial": ("partial", "部分支撑"),
        "unsupported": ("bad", "未支撑"),
    }
    by_claim = {v.claim_id: v for v in (verifications or [])}
    rows = []
    for claim in claims:
        v = by_claim.get(claim.claim_id)
        if verifications is None:
            pill = '<span class="pill unknown">已核验</span>'
        elif v is None or v.unverified:
            pill = '<span class="pill unknown">未核验</span>'
        else:
            cls, text = labels.get(v.label.value, ("unknown", "未核验"))
            pill = f'<span class="pill {cls}">{text}</span>'
        title = claim.page_title or claim.url
        source = (
            f'<a href="{_esc(claim.url)}" target="_blank" rel="noopener">'
            f"{_esc(title)}</a>"
        )
        rows.append(
            f"<tr><td>{_esc(claim.text)}</td><td>{pill}</td><td>{source}</td></tr>"
        )

    summary = ""
    if counts:
        summary = (
            '<div class="verify-summary">'
            f'已支撑 {int(counts.get("supported", 0) or 0)} · '
            f'部分支撑 {int(counts.get("partial", 0) or 0)} · '
            f'未支撑 {int(counts.get("unsupported", 0) or 0)} · '
            f'未核验 {int(counts.get("unverified", 0) or 0)}'
            "</div>"
        )
    body = "".join(rows) or '<tr><td colspan="3" class="empty">没有可展示的论断。</td></tr>'
    return _card("论断核验", f'{summary}<table class="verify">{header}{body}</table>')


def _verify_html(result=None) -> str:
    if result is None:
        return _verify_card([])
    claims = [c for f in result.findings for c in f.claims]
    return _verify_card(claims, result.verifications)


def _sources_card(report=None, claims: list[Claim] | None = None) -> str:
    if report is None or not report.citation_map:
        return _card("来源", '<div class="empty">运行后显示报告引用来源。</div>')

    claim_by_id = {c.claim_id: c for c in (claims or [])}
    grouped: dict[str, dict] = {}
    for n in sorted(report.citation_map):
        claim = claim_by_id.get(report.citation_map[n])
        if claim is None:
            continue
        entry = grouped.setdefault(claim.url, {"title": claim.page_title or claim.url, "nums": []})
        entry["nums"].append(n)

    items = []
    for url, entry in grouped.items():
        badges = "".join(f'<span class="cite">{n}</span>' for n in entry["nums"])
        items.append(
            "<li>"
            f'{badges} <a class="source-title" href="{_esc(url)}" target="_blank" '
            f'rel="noopener">{_esc(entry["title"])}</a>'
            f'<div class="source-url">{_esc(url)}</div>'
            "</li>"
        )
    body = f'<ol class="sources">{"".join(items)}</ol>'
    return _card("来源", body)


def _sources_html(result=None) -> str:
    if result is None or result.report is None:
        return _sources_card(None)
    claims = [c for f in result.findings for c in f.claims]
    return _sources_card(result.report, claims)


# --------------------------------------------------------------------------- #
# 事件处理
# --------------------------------------------------------------------------- #
def _resolve_pace(mock: bool, pace_s: float) -> float:
    """mock 下默认放慢渲染节奏，让流式更新看得见；真实模式不拖慢。"""
    if pace_s > 0:
        return float(pace_s)
    return 0.45 if mock else 0.0


def do_research(
    question: str, config: str, mock: bool = False, pace_s: float = 0.0
):
    """返回 (计划, 指标, 报告, 核验表, 来源) 五块 HTML。

    一次性返回，没有流式 yield，pace_s 仅用于与 stream_research 保持同一接口。
    """
    if not question or not question.strip():
        return (
            _plan_html(),
            _metrics_html(mock=mock),
            _error_report("请输入研究问题"),
            _verify_html(),
            _sources_html(),
        )

    config_code = CONFIG_MAP.get(config, "A2")
    try:
        # --mock / MOCK=1 时显式传 mock=True；否则保持原行为（读 .env）。
        settings = Settings(mock=True) if mock else Settings()
        result = asyncio.run(
            run_research(question.strip(), settings=settings, config=config_code)
        )
    except SystemExit as e:  # run_research 用 SystemExit 报配置缺失
        return (
            _plan_html(),
            _metrics_html(mock=mock),
            _error_report(f"配置错误：{e}"),
            _verify_html(),
            _sources_html(),
        )
    except Exception as e:  # noqa: BLE001 —— UI 必须把异常呈现给用户而不是崩掉
        return (
            _plan_html(),
            _metrics_html(mock=mock),
            _error_report(f"运行失败：{type(e).__name__}: {e}"),
            _verify_html(),
            _sources_html(),
        )

    return (
        _plan_html(result),
        _metrics_html(result, mock=mock),
        _report_html(result),
        _verify_html(result),
        _sources_html(result),
    )


def _format_exception(exc: BaseException) -> str:
    if isinstance(exc, SystemExit):
        return f"配置错误：{exc}"
    return f"运行失败：{type(exc).__name__}: {exc}"


def _load_claims_from_db(db_path: Path, run_id: str) -> list[Claim]:
    """从 findings 表读已落库的 claims（流式渲染细节用）。

    指标仍然只认 trace 的聚合值；这里只取正文/URL 这类 trace 不记录的字段。
    """
    try:
        conn = sqlite3.connect(str(db_path), timeout=3)
    except sqlite3.Error:
        return []
    try:
        rows = conn.execute(
            "SELECT claims FROM findings WHERE run_id=? ORDER BY sub_query_id",
            (run_id,),
        ).fetchall()
    except sqlite3.Error:
        rows = []
    finally:
        conn.close()

    claims: list[Claim] = []
    for (raw,) in rows:
        try:
            items = json.loads(raw or "[]")
        except (TypeError, json.JSONDecodeError):
            continue
        for item in items:
            try:
                claims.append(Claim.model_validate(item))
            except Exception:  # noqa: BLE001 —— 单条坏数据不拖垮整张表
                continue
    return claims


def _load_report_from_db(db_path: Path, run_id: str) -> ReportArtifact | None:
    try:
        conn = sqlite3.connect(str(db_path), timeout=3)
    except sqlite3.Error:
        return None
    try:
        row = conn.execute(
            "SELECT artifact FROM reports WHERE run_id=?", (run_id,)
        ).fetchone()
    except sqlite3.Error:
        row = None
    finally:
        conn.close()
    if not row:
        return None
    try:
        return ReportArtifact.model_validate(json.loads(row[0]))
    except Exception:  # noqa: BLE001 —— 报告读不出来时下个事件会再试
        return None


def _hydrate_claims_from_db(state: dict, db_path: Path) -> None:
    run_id = state.get("run_id")
    if not run_id or state.get("claims"):
        return
    state["claims"] = _load_claims_from_db(db_path, run_id)


def _hydrate_from_db(state: dict, db_path: Path) -> None:
    run_id = state.get("run_id")
    if not run_id:
        return
    if not state.get("claims"):
        state["claims"] = _load_claims_from_db(db_path, run_id)
    if state.get("report") is None:
        state["report"] = _load_report_from_db(db_path, run_id)


def _progress_snapshot(state: dict, start: float, button_enabled: bool = False):
    """把当前已读到的事件渲染成一组 HTML。未到达的阶段一律显示占位符。"""
    result = state.get("result")
    claims: list[Claim] = list(state.get("claims") or [])
    report = state.get("report")
    if result is not None:
        result_claims = [c for f in result.findings for c in f.claims]
        if result_claims:
            claims = result_claims
        report = result.report or report

    verify_counts = state.get("verify")
    if result is not None and state.get("config") == "A2":
        verifications = result.verifications  # 逐条真实判决
    elif state.get("config") == "A1" and (claims or verify_counts):
        verifications = []  # A1 没有核验：如实显示“未核验”
    else:
        verifications = None  # A2 核验已结束但 result 还没返回：只给中性胶囊

    done = state.get("done") or {}
    write = state.get("write") or {}
    verify = verify_counts or {}

    if done.get("claims") is not None:
        claims_val = str(done["claims"])
    elif verify.get("claims") is not None:
        claims_val = str(verify["claims"])
    elif result is not None:
        claims_val = str(result.total_claims)
    else:
        claims_val = "—"

    cited_val = str(write["cited"]) if "cited" in write else "—"
    tokens_val = "—"
    seconds_val = "—"
    if done:
        if "total_tokens_in" in done or "total_tokens_out" in done:
            tokens_val = str(
                int(done.get("total_tokens_in") or 0)
                + int(done.get("total_tokens_out") or 0)
            )
        if result is not None:
            seconds_val = f"{result.elapsed_s:.1f}"
        else:
            seconds_val = f"{time.monotonic() - start:.1f}"

    metrics_html = _metrics_values_html(
        [
            (claims_val, "条论断"),
            (cited_val, "条引用"),
            (tokens_val, _token_caption(bool(state.get("mock")))),
            (seconds_val, "秒"),
        ]
    )

    error = state.get("error")
    if error:
        report_html = _error_report(error)
    elif report is not None:
        report_html = _report_card(report)
    else:
        if not state.get("plan"):
            phase = "正在规划子问题…"
        elif state.get("verify"):
            phase = "正在核验并撰写报告…"
        else:
            phase = "正在并行检索与抽取…"
        report_html = _card("研究报告", f'<div class="empty">{phase}</div>')

    return (
        _plan_progress_html(state.get("plan") or [], state.get("subs") or {}),
        metrics_html,
        report_html,
        _verify_card(claims, verifications, counts=verify_counts),
        _sources_card(report, claims),
        gr.update(interactive=button_enabled),
    )


def stream_research(
    question: str,
    config: str,
    mock: bool = False,
    timeout_s: float = 300.0,
    pace_s: float = 0.0,
):
    """生成器：边跑管线边 yield 五块 HTML + 按钮状态。

    管线在后台线程里跑；主线程只读它自己写的 JSONL trace（增量、记住字节
    偏移），每读到一个事件就 yield 一版界面。不启动 Gradio，也不依赖浏览器。

    pace_s 只影响渲染节奏（yield 之间 sleep），不影响后台管线：
    mock 且未显式指定时默认 0.45 秒，真实模式默认 0 秒。
    """
    start = time.monotonic()
    config_code = CONFIG_MAP.get(config, "A2")
    pace = _resolve_pace(mock, pace_s)

    if not question or not question.strip():
        yield (
            _plan_progress_html([], {}),
            _metrics_values_html(
                [
                    ("—", "条论断"),
                    ("—", "条引用"),
                    ("—", _token_caption(mock)),
                    ("—", "秒"),
                ]
            ),
            _error_report("请输入研究问题"),
            _verify_card([]),
            _sources_card(None),
            gr.update(interactive=True),
        )
        return

    settings = Settings(mock=True) if mock else Settings()
    db_path = Path(settings.database_path)
    trace_dir = Path(settings.trace_dir)
    state: dict = {
        "config": config_code,
        "mock": mock,
        "run_id": None,
        "plan": [],
        "subs": {},
        "verify": None,
        "write": None,
        "done": None,
        "claims": [],
        "report": None,
        "result": None,
        "error": None,
    }

    try:
        trace_dir.mkdir(parents=True, exist_ok=True)
        before = {p.name for p in trace_dir.glob("*.jsonl")}
    except OSError:
        before = set()

    holder: dict = {}

    def _work() -> None:
        try:
            holder["result"] = asyncio.run(
                run_research(
                    question.strip(),
                    settings=settings,
                    max_concurrency=4,
                    config=config_code,
                )
            )
        except BaseException as exc:  # noqa: BLE001 —— 线程里的异常必须交回 UI
            holder["error"] = exc

    thread = threading.Thread(target=_work, name="gradio-research", daemon=True)
    thread.start()

    # 第一次 yield：让按钮立刻进入“运行中”，页面有反馈。
    yield _progress_snapshot(state, start)

    trace_path: Path | None = None
    pos = 0
    buf = b""

    while True:
        if time.monotonic() - start > timeout_s:
            state["error"] = (
                f"运行超时（{timeout_s:.0f} 秒）：已停止等待，后台管线可能仍在运行。"
            )
            yield _progress_snapshot(state, start, button_enabled=True)
            return

        progressed = False

        if trace_path is None:
            try:
                fresh = [
                    p for p in trace_dir.glob("*.jsonl") if p.name not in before
                ]
            except OSError:
                fresh = []
            if fresh:
                trace_path = max(fresh, key=lambda p: p.stat().st_mtime_ns)
                state["run_id"] = trace_path.stem

        if trace_path is not None:
            try:
                with open(trace_path, "rb") as fh:
                    fh.seek(pos)
                    chunk = fh.read()
            except OSError:
                chunk = b""

            if chunk:
                pos += len(chunk)
                buf += chunk
                events = []
                while b"\n" in buf:
                    raw, buf = buf.split(b"\n", 1)
                    line = raw.decode("utf-8", "replace").strip()
                    if not line:
                        continue
                    try:
                        events.append(json.loads(line))
                    except json.JSONDecodeError:
                        continue

                for ev in events:
                    name = ev.get("event")
                    if name == "run_start":
                        state["run_id"] = ev.get("run_id") or state["run_id"]
                        continue
                    if name == "plan":
                        state["plan"] = ev.get("sub_queries") or []
                    elif name == "sub_done":
                        state["subs"][str(ev.get("sub_query_id"))] = ev
                    elif name == "verify_done":
                        state["verify"] = ev
                        # 只补 claims（trace 不记正文）；报告留到 write_done，
                        # 否则轮询稍慢时会出现“核验阶段报告已写好”的假渐进。
                        _hydrate_claims_from_db(state, db_path)
                    elif name == "write_done":
                        state["write"] = ev
                        _hydrate_from_db(state, db_path)
                    elif name == "run_done":
                        state["done"] = ev
                        thread.join(timeout=5.0)
                        if "result" in holder:
                            state["result"] = holder["result"]
                        if "error" in holder:
                            state["error"] = _format_exception(holder["error"])
                        _hydrate_from_db(state, db_path)
                        yield _progress_snapshot(state, start, button_enabled=True)
                        return
                    elif name == "run_error":
                        state["error"] = f"运行失败：{ev.get('error', '未知错误')}"
                        yield _progress_snapshot(state, start, button_enabled=True)
                        return
                    else:
                        continue

                    if pace > 0 and name in ("sub_done", "verify_done", "write_done"):
                        time.sleep(pace)
                    yield _progress_snapshot(state, start)
                    progressed = True

        if "error" in holder:
            state["error"] = _format_exception(holder["error"])
            yield _progress_snapshot(state, start, button_enabled=True)
            return

        if "result" in holder and not thread.is_alive():
            # trace 可能被外部清理，或 run_done 行还没落盘：以 result 收尾
            state["result"] = holder["result"]
            _hydrate_from_db(state, db_path)
            yield _progress_snapshot(state, start, button_enabled=True)
            return

        if not progressed:
            time.sleep(0.25)


def _env_mock() -> bool:
    return os.environ.get("MOCK", "").strip().lower() in ("1", "true", "yes", "on")


def build_demo(mock: bool = False) -> gr.Blocks:
    with gr.Blocks(
        title="深度研究 · Deep Research Agent",
        theme=gr.themes.Soft(primary_hue="indigo", neutral_hue="slate"),
    ) as demo:
        gr.HTML(CSS, elem_id="dr-css")
        if mock:
            gr.HTML(f'<div class="mock-banner">{MOCK_BANNER}</div>', elem_id="mock-banner")

        with gr.Row(equal_height=False, elem_classes="main-row"):
            with gr.Column(scale=3, min_width=330, elem_id="left-panel"):
                gr.HTML(_title_html(), elem_id="dr-title-block")
                # 外层只负责布局（去掉 Gradio 自带容器边框），内层 Group 才是卡片
                with gr.Column(elem_classes="card-flat"):
                    with gr.Group(elem_id="input-card"):
                        question = gr.Textbox(
                            label="研究问题",
                            placeholder="输入一个研究问题，例如：MCP 协议的核心架构",
                            lines=2,
                            elem_id="question-box",
                        )
                        config = gr.Radio(
                            ["A1 · 不核验", "A2 · 完整系统"],
                            value="A2 · 完整系统",
                            label="配置",
                            elem_id="config-radio",
                            # Gradio 6.x 的 Radio 已无 horizontal 参数；方向由下方 CSS 控制
                        )
                        btn = gr.Button("开始研究", variant="primary")

                plan = gr.HTML(_plan_html(), elem_id="plan-block")
                metrics = gr.HTML(_metrics_html(mock=mock), elem_id="metrics-block")

            with gr.Column(
                scale=8, elem_id="right-panel", elem_classes="right-col"
            ):
                report = gr.HTML(
                    _report_html(),
                    elem_id="report-block",
                    elem_classes="panel-report",
                )
                verify = gr.HTML(
                    _verify_html(),
                    elem_id="verify-block",
                    elem_classes="panel-verify",
                )
                sources = gr.HTML(
                    _sources_html(),
                    elem_id="sources-block",
                    elem_classes="panel-sources",
                )

        def _run(q: str, c: str):
            # 必须是生成器：Gradio 会把每次 yield 推给前端，实现流式更新。
            yield from stream_research(q, c, mock=mock)

        btn.click(
            _run,
            inputs=[question, config],
            outputs=[plan, metrics, report, verify, sources, btn],
        )
    return demo


def main() -> None:
    parser = argparse.ArgumentParser(description="深度研究 Gradio 演示前端")
    parser.add_argument("--mock", action="store_true", help="演示模式：固定资料，不联网、不消耗额度")
    parser.add_argument("--port", type=int, default=7860, help="监听端口（默认 7860）")
    parser.add_argument("--host", default="127.0.0.1", help="监听地址（默认 127.0.0.1）")
    args = parser.parse_args()

    mock = args.mock or _env_mock()
    build_demo(mock=mock).launch(
        server_name=args.host,
        server_port=args.port,
        show_error=True,
        footer_links=[],  # Gradio 6.x 没有 footer=False；空列表等价于不显示底部链接
    )


if __name__ == "__main__":
    main()
