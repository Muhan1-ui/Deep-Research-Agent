"""--mock 运行模式：无需 API key、无需网络，跑通完整的 A2 研究链路。

设计红线：mock 只替换**外部世界**（LLM / 搜索 / HTTP 抓取），不替换任何
内部逻辑。规划解析、claims 解析、句级锚定（quote_locator）、引用完整性、
verifier 判定、writer 的 citation_map 全部走真实代码路径——mock 里不允许
出现"只在 mock 下成立"的简化格式，否则就是自己骗自己。

三个假实现与真实实现接口完全一致：
- MockLLM    ≙ LLMClient.complete(messages, tools) -> LLMResult
              角色按各 agent 的 system prompt 首句前缀识别：orchestrator 的
              role 只存在于 _UsageTrap 包装层，工厂拿不到，这是现有代码里
              唯一可用的角色信号。
- MockSearch ≙ TavilySearch.__call__(WebSearchArgs) -> str（同一段输出格式）
- MockFetch  ≙ PageFetcher（继承之，只把 HTTP 传输换成 MockTransport——
              抓取归档、trafilatura 抽取、切段、BM25 选片、偏移锚定全部真跑）

固定资料的 URL 用 example.invalid（RFC 2606 保留域名，不可解析）；再加
MockTransport 拦截，双保险：即使某处漏了 mock，也打不到外网。
"""

from __future__ import annotations

import json
import re

import httpx

from src.agent.llm import LLMResult, ToolCallRequest
from src.agent.tools.base import Tool
from src.agent.tools.fetch import PageFetcher
from src.agent.tools.pages import FetchPageArgs
from src.agent.tools.search import WebSearchArgs

# --------------------------------------------------------------------------- #
# 固定外部世界：一份关于 MCP 架构的 mock 资料，共 3 段、每段 3 句
# --------------------------------------------------------------------------- #
MOCK_PAGE_HTML = """<html><head><title>MCP 协议核心架构（mock 资料页）</title></head>
<body><article>
<h1>Model Context Protocol 架构概览</h1>
<p>MCP（Model Context Protocol）是一个开放协议，用于规范大模型应用与外部数据源和工具之间的连接方式。MCP 采用客户端-服务器架构，宿主应用通过客户端与多个服务器保持一对一连接。每个服务器都可以提供三类标准能力：工具（Tools）、资源（Resources）和提示（Prompts）。</p>
<p>通信层面，MCP 基于 JSON-RPC 2.0 协议交换消息，支持 stdio 与 Streamable HTTP 两种传输方式。客户端与服务器之间遵循生命周期约定，依次经历初始化、能力协商与消息交换三个阶段。初始化完成后，客户端可以发送请求列出服务器提供的工具清单。</p>
<p>安全模型方面，宿主应用在将服务器数据传给大模型之前必须征得用户明确同意。MCP 规范要求服务器不得在未经授权的情况下访问宿主之外的数据。采样（Sampling）能力允许服务器在用户批准后借助客户端发起模型补全请求。</p>
</article></body></html>"""

MOCK_SEARCH_RESULTS = [
    (
        "MCP 协议架构详解（mock）",
        "https://example.invalid/mcp-architecture",
        "MCP 采用客户端-服务器架构，宿主应用通过客户端与多个服务器保持一对一连接。",
    ),
    (
        "MCP 传输与生命周期（mock）",
        "https://example.invalid/mcp-transport",
        "MCP 基于 JSON-RPC 2.0 协议交换消息，支持 stdio 与 Streamable HTTP 两种传输方式。",
    ),
    (
        "MCP 安全模型（mock）",
        "https://example.invalid/mcp-security",
        "宿主应用在将服务器数据传给大模型之前必须征得用户明确同意。",
    ),
]


def _serve_fixture(request: httpx.Request) -> httpx.Response:
    """MockTransport handler：任何 URL 都返回同一份固定资料（不解析域名）。"""
    return httpx.Response(200, html=MOCK_PAGE_HTML)


class MockSearch:
    """web_search 的假实现：输出格式与 TavilySearch 完全一致。"""

    async def __call__(self, args: WebSearchArgs) -> str:
        lines = [f"搜索结果 for {args.query!r}:"]
        for i, (title, url, summary) in enumerate(
            MOCK_SEARCH_RESULTS[: args.max_results], 1
        ):
            lines.append(f"{i}. {title}\n   {url}\n   {summary}")
        return "\n".join(lines)


class MockFetch(PageFetcher):
    """fetch_page 的假实现：PageFetcher + MockTransport。

    继承而不是重写：归档落盘、trafilatura 抽正文、切段、BM25 选片段、
    偏移锚定、on_page 落库钩子全部复用真实实现——这样 researcher 拿到
    的 snippet_id / passage 与真实链路完全同构，句级锚定才有校验意义。
    """

    def __init__(self, pages_dir, on_page=None):
        self._mock_client = httpx.AsyncClient(transport=httpx.MockTransport(_serve_fixture))
        super().__init__(pages_dir, client=self._mock_client, on_page=on_page)

    async def aclose(self) -> None:
        # 注入的 client 真实 PageFetcher 不负责关闭（归调用方），这里由 mock 自己收尾
        await super().aclose()
        await self._mock_client.aclose()


_FETCH_TOOL_DESCRIPTION = (
    "读取指定 URL 的网页正文，返回与 query 最相关的 1~3 个原文片段"
    "（不是全文），每个片段带 id。web_search 结果中需要深读某个来源时"
    "使用；query 传当前子问题或搜索词，用于片段筛选。"
)


def build_mock_search_tool() -> Tool:
    return Tool(
        name="web_search",
        description=(
            "联网搜索引擎。输入查询词，返回标题/URL/摘要列表。"
            "回答时效性或事实性问题前应先调用；可多次调用不同查询词。"
        ),
        args_model=WebSearchArgs,
        handler=MockSearch(),
    )


def build_mock_fetch_tool(pages_dir, on_page=None) -> tuple[MockFetch, Tool]:
    """返回 (fetcher, tool)：researcher 的 PageResolver 需要同一个 fetcher。"""
    fetcher = MockFetch(pages_dir, on_page=on_page)
    return fetcher, Tool(
        name="fetch_page",
        description=_FETCH_TOOL_DESCRIPTION,
        args_model=FetchPageArgs,
        handler=fetcher,
    )


# --------------------------------------------------------------------------- #
# MockLLM：按角色返回该角色合法的结构化输出
# --------------------------------------------------------------------------- #
# 前缀 → 角色。顺序无重叠，但保持从长到短便于阅读。
_ROLE_PREFIXES = (
    ("你是研究规划师", "planner"),
    ("你是研究员", "researcher"),
    ("你是事实核查员", "verifier"),
    ("你是矛盾检测员", "contradiction"),
    ("你是研究报告撰写人", "writer"),
    ("你是一个严谨的研究助手", "ask"),
)

_SNIPPET_RE = re.compile(
    r"\[片段\d+/\d+ · id=([^\]]+)\] (.*?)(?=\n\[片段\d+/\d+ · id=|\Z)", re.S
)
_SENT_RE = re.compile(r"[^。！？!?]+[。！？!?]")
_WRITER_ROW_RE = re.compile(r"^\[\[(\d+)\]\] \| (.+?) \| (.+?) \| (.+)$", re.M)


def _role_of(system_prompt: str) -> str:
    for prefix, role in _ROLE_PREFIXES:
        if system_prompt.startswith(prefix):
            return role
    # 新加了 agent 却没扩 mock：宁可炸在开发机上，也不要在无人值守时伪造输出
    raise RuntimeError(
        f"MockLLM 未覆盖的角色 prompt（前 40 字）：{system_prompt[:40]!r}"
    )


def _sentences(passage: str) -> list[str]:
    """按行 + 句末标点切句；只要足够长（≥12 字）的完整句。"""
    out: list[str] = []
    for line in passage.splitlines():
        out.extend(s.strip() for s in _SENT_RE.findall(line))
    return [s for s in out if len(s) >= 12]


def _claims_from_fetch(fetch_text: str) -> str:
    """从 fetch_page 的**真实输出**里抄句子构造 claims。

    这里刻意不复用固定资料常量：mock researcher 像真模型一样只读工具结果，
    引文逐字来自片段、snippet_id 原样回传——build_claims 的锚定校验因此
    校验的是真实链路，不会被"mock 自己作弊"绕过。
    """
    spotted = _SNIPPET_RE.findall(fetch_text)
    pools = [
        (sid, [s for s in _sentences(passage) if passage.count(s) == 1])
        for sid, passage in spotted
    ]
    claims: list[dict] = []
    used: set[str] = set()
    # 第一轮：每个片段先贡献首句（尽量跨源引用）；第二轮：补齐到 3 条
    for sid, sents in pools:
        for s in sents:
            if s not in used:
                claims.append({"text": s, "quote": s, "snippet_id": sid})
                used.add(s)
                break
    for sid, sents in pools:
        for s in sents:
            if len(claims) >= 3:
                break
            if s not in used:
                claims.append({"text": s, "quote": s, "snippet_id": sid})
                used.add(s)
    return json.dumps(claims, ensure_ascii=False)


def _last_user(messages: list[dict]) -> str:
    users = [str(m.get("content") or "") for m in messages if m.get("role") == "user"]
    return users[-1] if users else ""


def _tool_texts(messages: list[dict]) -> list[str]:
    return [str(m.get("content") or "") for m in messages if m.get("role") == "tool"]


class MockLLM:
    """LLMClient 的假实现：同签名、同返回类型、token 计 0、模型名 mock。

    角色行为（都产出能被现有解析器接受的**真实形状**）：
    - planner     : `[{"question", "search_hints"}]`，3 个平行子问题
    - researcher  : 先 web_search，再 fetch_page，最后 claims JSON（quote 逐字来自片段）
    - verifier    : `[{"claim_id", "label", "rationale"}]`，逐条对应输入
    - contradiction: `[]`
    - writer      : `{"sections": [...]}`，正文用 [[n]] 引用
    - ask (A0)    : 同样先搜后读，最后给带来源的纯文本答案
    """

    model = "mock"
    usage_in = 0
    usage_out = 0

    def __init__(self) -> None:
        self._call_seq = 0

    async def complete(
        self,
        messages: list[dict],
        tools: list[dict] | None = None,
        max_retries: int = 4,
        temperature: float | None = None,
        extra_body: dict | None = None,
    ) -> LLMResult:
        system = str(messages[0].get("content") or "")
        role = _role_of(system)
        if role == "planner":
            return self._final(self._plan(messages))
        if role in ("researcher", "ask"):
            return self._search_then_read(messages, claims=role == "researcher")
        if role == "verifier":
            return self._final(self._verdicts(messages))
        if role == "contradiction":
            return self._final("[]")
        return self._final(self._report(messages))  # writer

    # ------------------------------ 各角色输出 ------------------------------ #
    @staticmethod
    def _plan(messages: list[dict]) -> str:
        question = _last_user(messages).strip()
        items = [
            {"question": f"{question}：核心概念与整体架构",
             "search_hints": [f"{question} 架构"]},
            {"question": f"{question}：工作流程与通信机制",
             "search_hints": [f"{question} 通信"]},
            {"question": f"{question}：安全模型与应用生态",
             "search_hints": [f"{question} 安全"]},
        ]
        return json.dumps(items, ensure_ascii=False)

    def _search_then_read(self, messages: list[dict], claims: bool) -> LLMResult:
        """researcher / ask 共用的工具调用节奏：search → fetch → 最终输出。"""
        texts = _tool_texts(messages)
        search_hits = [t for t in texts if t.startswith("搜索结果")]
        fetch_hits = [t for t in texts if "【网页】" in t]
        question = _last_user(messages).splitlines()[0].strip() or "mock 查询"

        if not search_hits:
            return self._tool_call("web_search", {"query": question, "max_results": 3})
        if not fetch_hits:
            m = re.search(r"https://\S+", search_hits[0])
            url = m.group(0) if m else MOCK_SEARCH_RESULTS[0][1]
            return self._tool_call(
                "fetch_page", {"url": url, "query": question, "max_snippets": 3}
            )
        if claims:
            return self._final(_claims_from_fetch(fetch_hits[-1]))
        return self._final(self._ask_answer(search_hits[0]))

    @staticmethod
    def _ask_answer(search_text: str) -> str:
        sources = re.findall(r"^\d+\. (.+)\n\s+(https://\S+)", search_text, re.M)
        lines = [
            "（mock 模式）根据固定资料：MCP 采用客户端-服务器架构，"
            "基于 JSON-RPC 2.0 交换消息，并在把服务器数据传给大模型前要求用户授权。",
            "",
            "来源：",
        ]
        lines.extend(f"{i}. {title} — {url}" for i, (title, url) in enumerate(sources, 1))
        return "\n".join(lines)

    @staticmethod
    def _verdicts(messages: list[dict]) -> str:
        payload = _last_user(messages)
        try:
            items = json.loads(payload)
        except ValueError:
            return "[]"
        verdicts = [
            {
                "claim_id": it.get("claim_id", ""),
                "label": "supported",
                # mock 的 claim.text 就是引文原句，判 supported 是如实的
                "rationale": "引文逐字包含论断（mock 判定；解析与校验走真实链路）",
            }
            for it in items
            if isinstance(it, dict)
        ]
        return json.dumps(verdicts, ensure_ascii=False)

    @staticmethod
    def _report(messages: list[dict]) -> str:
        rows = _WRITER_ROW_RE.findall(_last_user(messages))
        half = (len(rows) + 1) // 2

        def body(subset: list[tuple[str, str, str, str]]) -> str:
            out = []
            for n, text, _quote, tag in subset:
                head = tag.split("｜")[0].strip()
                ann = {"partial": "（部分支持）", "未核验": "（未核验）",
                       "unverified": "（未核验）"}.get(head, "")
                out.append(f"{text}[[{n}]]{ann}")
            return "\n".join(out)

        sections = [
            {"heading": "核心发现", "body": body(rows[:half])},
            {"heading": "讨论与限制", "body": body(rows[half:])},
        ]
        return json.dumps({"sections": sections}, ensure_ascii=False)

    # ------------------------------ 构造返回 ------------------------------ #
    def _tool_call(self, name: str, args: dict) -> LLMResult:
        self._call_seq += 1
        return LLMResult(
            content=None,
            tool_calls=[ToolCallRequest(
                id=f"mock-call-{self._call_seq}",
                name=name,
                arguments_raw=json.dumps(args, ensure_ascii=False),
            )],
            usage_in=0,
            usage_out=0,
            model="mock",
        )

    @staticmethod
    def _final(text: str) -> LLMResult:
        return LLMResult(content=text, usage_in=0, usage_out=0, model="mock")
