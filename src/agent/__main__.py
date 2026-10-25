"""CLI 入口：python -m src.agent "你的研究问题" [--max-turns N] [--config A0]

W1 只实现 A0（单 Agent：搜索 → 作答）——它同时就是 W6 消融实验的 baseline。
A1/A2 在 W3+ 由 orchestrator 提供，届时 --config 扩展取值。
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

SYSTEM_PROMPT = """你是一个严谨的研究助手，当前运行在单 Agent 模式。
规则：
1. 回答事实性/时效性问题前，必须先调用 web_search（可多次、可改写查询词）；
   对搜索结果中信息量大的来源 URL，再调用 fetch_page 深读相关片段；
2. 只依据搜索/阅读到的内容作答，不编造；检索不到的信息要明说"未检索到"；
3. 回答用中文、简洁分点；
4. 末尾附"来源"清单（标题 + URL），只列真正用到的。"""


def build_registry(settings: Settings) -> ToolRegistry:
    registry = ToolRegistry()
    if settings.tavily_api_key:
        registry.register(build_search_tool(settings.tavily_api_key))
    registry.register(build_fetch_tool(settings.pages_dir))  # fetch_page 无需任何 key
    return registry


async def run_cli(question: str, max_turns: int, config: str) -> int:
    if config != "A0":
        print(f"暂只支持 A0（收到 {config}）；A1/A2 随 W3+ orchestrator 加入", file=sys.stderr)
        return 2
    settings = Settings()
    if not settings.llm_api_key:
        print("缺少 LLM_API_KEY：复制 .env.example 为 .env 并填写（见 README）", file=sys.stderr)
        return 2

    registry = build_registry(settings)
    if "web_search" not in registry.names():
        print("警告: 未配置 TAVILY_API_KEY，本次没有搜索工具，回答仅来自模型自身知识", file=sys.stderr)

    loop = AgentLoop(LLMClient(settings), registry, max_turns=max_turns)
    try:
        result = await loop.run(SYSTEM_PROMPT, question)
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


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="deep-research-agent", description="多智能体深度研究系统（W1: A0 单 Agent 模式）"
    )
    parser.add_argument("question", help="研究问题")
    parser.add_argument("--max-turns", type=int, default=8, help="工具调用轮数上限（默认 8）")
    parser.add_argument("--config", default="A0", choices=["A0"], help="消融配置开关（W6 用）")
    args = parser.parse_args()
    sys.exit(asyncio.run(run_cli(args.question, args.max_turns, args.config)))


if __name__ == "__main__":
    main()
