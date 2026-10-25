"""Gradio demo：研究问题 → 报告（W4 DoD：可演示形态）。

运行：python app/gradio_ui.py   （需先配置 .env；浏览器打开 http://127.0.0.1:7860）
刻意保持最小：同步执行 + 结果展示。流式过程可视化属于锦上添花，
对简历数字零贡献，时间盒外（PLAN 纪律）。
"""

from __future__ import annotations

import asyncio

import gradio as gr

from src.agent.config import Settings
from src.agent.orchestrator.run import run_research


def do_research(question: str, config: str) -> tuple[str, str]:
    if not question.strip():
        return "请输入研究问题", ""
    try:
        result = asyncio.run(
            run_research(question.strip(), settings=Settings(), config=config))
    except SystemExit as e:
        return f"配置错误：{e}", ""
    except Exception as e:  # noqa: BLE001 —— UI 必须把异常呈现给用户而不是崩掉
        return f"运行失败：{type(e).__name__}: {e}", ""

    lines = [
        f"**run**: `{result.run_id}`　**config**: {result.config}",
        result.summary_line(),
    ]
    for f in result.findings:
        flag = {"ok": "✓", "degraded": "△", "failed": "✗"}[f.status.value]
        lines.append(f"- {flag} {f.sub_query_id}：{f.question}（claims={len(f.claims)}）")
    if result.report is not None and result.report.citation_map:
        lines.append("\n---\n### 引用清单")
        for n, cid in result.report.citation_map.items():
            lines.append(f"[{n}] `{cid}`")
    return "\n".join(lines), result.report.markdown if result.report else ""


def build_demo() -> gr.Blocks:
    with gr.Blocks(title="Deep Research Agent") as demo:
        gr.Markdown("# Deep Research Agent\n规划 → 并行检索 → 逐条核验 → 带引用报告")
        with gr.Row():
            question = gr.Textbox(label="研究问题", scale=4,
                                  placeholder="例：MCP 协议的核心架构与生态现状")
            config = gr.Radio(["A1", "A2"], value="A2", label="配置",
                              info="A1=无核验；A2=完整系统（Verifier+Writer）")
            btn = gr.Button("开始研究", variant="primary")
        stats = gr.Markdown(label="运行状态")
        report = gr.Markdown(label="研究报告")
        btn.click(do_research, inputs=[question, config], outputs=[stats, report])
    return demo


if __name__ == "__main__":
    build_demo().launch()
