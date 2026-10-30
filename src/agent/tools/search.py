"""web_search 工具：Tavily 实现。

Provider 抽象刻意只留一个函数签名（handler 即 __call__）——W7 切
SearXNG/博查时写一个同类替换即可，不需要配置化框架。
免费层 1000 次/月：调用方（W7 缓存层）负责节流，这里不管。
"""

from __future__ import annotations

import httpx
from pydantic import BaseModel, Field

from src.agent.tools.base import Tool


class WebSearchArgs(BaseModel):
    query: str = Field(description="搜索词；中英文均可，专有名词建议用英文")
    max_results: int = Field(default=5, ge=1, le=10)


class TavilySearch:
    URL = "https://api.tavily.com/search"

    def __init__(self, api_key: str, client: httpx.AsyncClient | None = None):
        self._key = api_key
        self._client = client  # 注入 httpx.AsyncClient（测试用 MockTransport）

    async def __call__(self, args: WebSearchArgs) -> str:
        payload = {"api_key": self._key, "query": args.query, "max_results": args.max_results}
        if self._client is not None:
            resp = await self._client.post(self.URL, json=payload)
        else:
            async with httpx.AsyncClient(timeout=15) as c:
                resp = await c.post(self.URL, json=payload)
        if resp.is_error:
            # 带厂商原文片段：配额（432）/限流（429）/key 失效的判别信息都在
            # body 里，raise_for_status 的消息只有状态行（第十六轮教训）
            raise RuntimeError(f"HTTP {resp.status_code}: {resp.text[:200]}")
        results = resp.json().get("results") or []
        if not results:
            return f"(无搜索结果: {args.query})"
        lines = [f"搜索结果 for {args.query!r}:"]
        for i, r in enumerate(results, 1):
            lines.append(f"{i}. {r.get('title', '')}\n   {r.get('url', '')}\n   {r.get('content', '')}")
        return "\n".join(lines)


def build_search_tool(api_key: str, client: httpx.AsyncClient | None = None) -> Tool:
    return Tool(
        name="web_search",
        description=(
            "联网搜索引擎。输入查询词，返回标题/URL/摘要列表。"
            "回答时效性或事实性问题前应先调用；可多次调用不同查询词。"
        ),
        args_model=WebSearchArgs,
        handler=TavilySearch(api_key, client),
    )
