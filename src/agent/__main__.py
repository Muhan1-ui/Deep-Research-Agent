"""CLI 入口。

  python -m src.agent ask "问题"        # A0 单 Agent（搜索→作答，消融 baseline）
  python -m src.agent research "问题"   # 多 Agent 研究管线（W3：规划→并行检索→落库）
"""

from __future__ import annotations

import argparse
import asyncio
import sys

from src.agent.config import Settings
from src.agent.llm import FatalLLMError, LLMClient, RetryableLLMError
from src.agent.runtime.loop import AgentLoop
from src.agent.tools.base import ToolRegistry
from src.agent.tools.fetch import build_fetch_tool
from src.agent.tools.search import build_search_tool

ASK_PROMPT = """你是一个严谨的研究助手，当前运行在单 Agent 模式。
规则：
1. 回答事实性/时效性问题前，必须先调用 web_search（可多次、可改写查询词）；
   对搜索结果中信息量大的来源 URL，再调用 fetch_page 深读相关片段；
2. 只依据搜索/阅读到的内容作答，不编造；检索不到的信息要明说"未检索到"；
3. 回答用中文、简洁分点；
4. 末尾附"来源"清单（标题 + URL），只列真正用到的。"""


def build_registry(settings: Settings) -> ToolRegistry:
    registry = ToolRegistry()
    if settings.mock:
        from src.agent.mock import build_mock_fetch_tool, build_mock_search_tool
        _, fetch_tool = build_mock_fetch_tool(settings.pages_dir)
        registry.register(build_mock_search_tool())
        registry.register(fetch_tool)
        return registry
    if settings.tavily_api_key:
        registry.register(build_search_tool(settings.tavily_api_key))
    registry.register(build_fetch_tool(settings.pages_dir))  # fetch_page 无需任何 key
    return registry


async def run_ask(question: str, max_turns: int, mock: bool | None = None) -> int:
    settings = Settings(mock=True) if mock else Settings()
    if not settings.mock and not settings.llm_api_key:
        print("缺少 LLM_API_KEY：复制 .env.example 为 .env 并填写（见 README）", file=sys.stderr)
        return 2
    if settings.mock:
        from src.agent.mock import MockLLM
        llm = MockLLM()
    else:
        llm = LLMClient(settings)
    loop = AgentLoop(llm, build_registry(settings), max_turns=max_turns)
    try:
        result = await loop.run(ASK_PROMPT, question)
    except FatalLLMError as e:
        print(f"致命错误（修配置后重试）: {e}", file=sys.stderr)
        return 2
    except RetryableLLMError as e:
        print(f"传输错误重试耗尽: {e}", file=sys.stderr)
        return 3
    print(result.answer or "(模型未给出回答)")
    print(
        f"[stats] turns={result.turns} tool_calls={result.tool_calls_total} "
        f"tool_errors={result.tool_errors_total} tokens_in={result.usage_in} "
        f"tokens_out={result.usage_out} stop={result.stop_reason}",
        file=sys.stderr,
    )
    return 0


async def run_research_cli(
    question: str, concurrency: int, config: str = "A1", mock: bool | None = None
) -> int:
    from src.agent.orchestrator.run import run_research
    from src.agent.schemas import RunStatus

    settings = Settings(mock=True) if mock else Settings()
    result = await run_research(
        question, settings=settings, max_concurrency=concurrency, config=config, mock=mock)
    print(f"◆ 研究计划（{len(result.plan)} 个子问题）:")
    for s in result.plan:
        print(f"  {s.sub_query_id}: {s.question}")
    print("◆ 各子问题结果:")
    for f in result.findings:
        flag = {"ok": "✓", "degraded": "△", "failed": "✗"}[f.status.value]
        print(f"  {flag} {f.sub_query_id} claims={len(f.claims)} "
              f"searches={len(f.search_queries)} fetches={f.pages_fetched} "
              f"({f.elapsed_s:.1f}s)")
        if f.error:
            print(f"      {f.error[:160]}")
    print(f"◆ {result.summary_line()}")
    print(f"◆ 明细: sqlite={settings.database_path}  trace=data/traces/{result.run_id}.jsonl")
    if result.report is not None:
        print("\n========== 研究报告 ==========")
        print(result.report.markdown)
        print("========== 引用清单 ==========")
        cited_claims = {c.claim_id: c for f in result.findings for c in f.claims}
        for n, cid in result.report.citation_map.items():
            c = cited_claims.get(cid)
            print(f"[{n}] {c.url if c else cid}")
    return 0 if any(f.claims for f in result.findings) else 1


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="deep-research-agent", description="多智能体深度研究系统"
    )
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_ask = sub.add_parser("ask", help="A0 单 Agent：搜索后作答（消融 baseline）")
    p_ask.add_argument("question")
    p_ask.add_argument("--max-turns", type=int, default=8)
    p_ask.add_argument("--mock", action="store_true", default=None,
                       help="mock 模式：固定 LLM/检索，无需 key、不联网（也可 MOCK=1）")

    p_res = sub.add_parser("research", help="多 Agent 管线：规划→并行检索→落库")
    p_res.add_argument("question")
    p_res.add_argument("--concurrency", type=int, default=4)
    p_res.add_argument("--config", default="A1", choices=["A1", "A2"],
                       help="消融配置（A2 = Verifier，随 W4 生效）")
    p_res.add_argument("--mock", action="store_true", default=None,
                       help="mock 模式：固定 LLM/检索，无需 key、不联网（也可 MOCK=1）")

    args = parser.parse_args()
    if args.cmd == "ask":
        sys.exit(asyncio.run(run_ask(args.question, args.max_turns, args.mock)))
    else:
        sys.exit(asyncio.run(
            run_research_cli(args.question, args.concurrency, args.config, args.mock)))


if __name__ == "__main__":
    main()
