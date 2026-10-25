"""fetch_page 实现：httpx 直抓 + trafilatura 抽正文 + 段落切段 + BM25 选片段 + 双层归档。

与契约（pages.py）的对齐红线：
1. 归档正文即偏移锚：text 用 newline="" 落盘（Windows 默认会把 \\n 翻成 \\r\\n，
   盘上字节 ≠ 内存文本，所有 locator 静默错位——第三轮审计 P0-1），写完的
   文件长度必须等于 text_chars；html 用字节原样写保真；
2. 归档不可变：page_id 带随机后缀，同秒重抓也必出新 id（P0-2），临时文件 +
   replace 原子落盘，并发读不会看到半截；
3. 传输失败 ≠ 不可读 ≠ 不支持的内容：404/超时/DNS 是 TOOL_ERROR（回喂、计入
   护栏）；JS-only 页面与非白名单 content-type（PDF/图片）是合法空结果——
   模型据此换 URL，不能被错误 streak 误杀。
"""

from __future__ import annotations

import asyncio
import hashlib
import re
import time
import uuid
from collections.abc import Awaitable, Callable
from datetime import date, datetime, timezone
from pathlib import Path

import httpx
import trafilatura
from rank_bm25 import BM25Okapi

from src.agent.tools.base import Tool
from src.agent.tools.pages import FetchPageArgs, FetchedPage, PageSnippet

UA = "deep-research-agent/0.1 (+self-hosted research tool)"
MAX_BYTES = 2_000_000       # 超限按工具错误处理，防内存/上下文事故
FETCH_TIMEOUT = 15.0
TARGET_PASSAGE = 300        # 段落合并目标（codepoint）
MAX_PASSAGE = 600           # 单段上限
SENTENCE_END = "。！？；.!?;"

# 只有这些类型值得进 trafilatura；PDF/图片/zip 走合法空结果（P1-a）。
# content-type 缺失时放行——不少老服务器不回这个头。
ALLOWED_CONTENT_TYPES = {"text/html", "application/xhtml+xml", "text/plain"}

_WORD_RE = re.compile(r"[a-z0-9]+|[\u4e00-\u9fff]")


def tokenize(text: str) -> list[str]:
    """BM25 分词：拉丁词 + CJK 逐字（中文无空格分隔，字粒度对 BM25 已足够）。"""
    return _WORD_RE.findall(text.lower())


def paragraph_spans(text: str) -> list[tuple[int, int]]:
    """按行切出非空段落 span。空白用指针跳过，不改文本一个字符。"""
    spans: list[tuple[int, int]] = []
    i, n = 0, len(text)
    while i < n:
        while i < n and text[i].isspace():
            i += 1
        if i >= n:
            break
        j = i
        while j < n and text[j] != "\n":
            j += 1
        k = j
        while k > i and text[k - 1].isspace():
            k -= 1
        spans.append((i, k))
        i = j + 1
    return spans


def _sentence_cut(window: str) -> int:
    """在窗口尾部找最后一个句末标点；找不到或太靠前（<30%）就硬切。"""
    best = max(window.rfind(ch) for ch in SENTENCE_END)
    cut = best + 1
    if cut < int(MAX_PASSAGE * 0.3):
        cut = MAX_PASSAGE
    return cut


def passage_spans(text: str) -> list[tuple[int, int]]:
    """段落 span → 贪心合并到 TARGET → 超长段按句界切到 ≤MAX。

    全程只有 (start, end) 指针运算：产物是归档正文的逐字切片，
    可验证性由构造方式保证，这是 quote_locator 可信的前提。
    """
    paras = paragraph_spans(text)
    merged: list[tuple[int, int]] = []
    for s, e in paras:
        # 直接并入：span 从首段 start 到当前段 end，中间的换行包含在内
        if merged and (e - merged[-1][0]) <= TARGET_PASSAGE:
            merged[-1] = (merged[-1][0], e)
        else:
            merged.append((s, e))

    spans: list[tuple[int, int]] = []
    for s, e in merged:
        while e - s > MAX_PASSAGE:
            cut = s + _sentence_cut(text[s : s + MAX_PASSAGE])
            spans.append((s, cut))
            s = cut
            while s < e and text[s].isspace():
                s += 1
        if s < e:
            spans.append((s, e))
    return spans


def select_snippet_spans(
    text: str, spans: list[tuple[int, int]], query: str, k: int
) -> list[tuple[int, int]]:
    """BM25 选片段：只取得分>0 的；无信号时头部兜底（首段通常是导语）；
    输出按阅读顺序（char_start 升序），模型读起来是自然顺序。"""
    if not spans:
        return []
    tokens = [tokenize(text[s:e]) for s, e in spans]
    try:
        scores = BM25Okapi(tokens).get_scores(tokenize(query))
    except ZeroDivisionError:  # 全空 token 的退化输入
        scores = [0.0] * len(spans)
    order = sorted(range(len(spans)), key=lambda i: scores[i], reverse=True)
    picked = [i for i in order if scores[i] > 0][:k]
    if not picked:
        picked = list(range(min(k, len(spans))))
    return [spans[i] for i in sorted(picked)]


def _content_type(resp: httpx.Response) -> str:
    return resp.headers.get("content-type", "").split(";")[0].strip().lower()


def _meta(html: str) -> tuple[str | None, date | None]:
    """trafilatura 元数据：标题 + 发布日（ISO 前缀解析，取不到就 None）。"""
    title: str | None = None
    pub: date | None = None
    try:
        md = trafilatura.extract_metadata(html)
        if md:
            title = getattr(md, "title", None)
            raw = getattr(md, "date", None)
            if raw:
                m = re.search(r"(\d{4})-(\d{2})-(\d{2})", str(raw))
                if m:
                    pub = date(int(m[1]), int(m[2]), int(m[3]))
    except Exception:  # noqa: BLE001 —— 元数据失败不影响正文流程
        pass
    return title, pub


class PageFetcher:
    """抓取 + 归档 + 选片段。

    pages 属性是本会话抓取记录（page_id → FetchedPage）——W3 orchestrator
    落库的数据源；归档文件本身就是持久层，进程重启后仍可回查。
    """

    def __init__(
        self,
        pages_dir: str | Path = "data/pages",
        client: httpx.AsyncClient | None = None,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        on_page: Callable[[FetchedPage], Awaitable[None]] | None = None,
    ):
        self._dir = Path(pages_dir)
        self._injected = client           # 测试注入 MockTransport
        self._own: httpx.AsyncClient | None = None  # 复用连接池（P2）
        self._sleep = sleep               # 注入以便测试瞬时回放 429 退避
        self._on_page = on_page           # 抓取完成回调（W3 落库挂这里）
        self.pages: dict[str, FetchedPage] = {}

    async def fetch(self, args: FetchPageArgs) -> FetchedPage:
        t0 = time.perf_counter()
        resp = await self._get(args.url)
        resp.raise_for_status()
        if len(resp.content) > MAX_BYTES:
            raise ValueError(f"页面过大({len(resp.content)}B)，放弃抓取")

        ctype = _content_type(resp)
        html = resp.text if (ctype in ALLOWED_CONTENT_TYPES or not ctype) else ""
        if html:
            raw_text = trafilatura.extract(html, include_comments=False, favor_recall=True) or ""
        else:
            # PDF/图片/JS-only：合法空结果。不进 TOOL_ERROR——否则 researcher
            # 一轮抓两个 PDF 就被 streak 护栏误杀（P1-a 的语义关键）
            raw_text = ""
        # 换行归一化必须在归档之前完成——归档文本就是偏移锚，之后一个字符都不能动
        text = raw_text.replace("\r\n", "\n").replace("\r", "\n")

        now = datetime.now(timezone.utc)
        # uuid 后缀：同秒重抓的撞号会静默覆盖归档，旧 snippet 从此指向新正文（P0-2）
        page_id = (
            f"{hashlib.sha1(args.url.encode('utf-8')).hexdigest()[:12]}"
            f"-{now.strftime('%Y%m%d%H%M%S')}-{uuid.uuid4().hex[:6]}"
        )
        self._dir.mkdir(parents=True, exist_ok=True)
        html_path = self._dir / f"{page_id}.html"
        text_path = self._dir / f"{page_id}.txt"
        html_path.write_bytes(resp.content)  # 字节保真；二进制内容也如实归档
        # newline=""：关掉 Windows 的 \n→\r\n 翻译，盘上字节 == 内存文本；
        # 临时文件 + replace：原子落盘，并发读不会看到半截（P0-1）
        tmp = text_path.with_name(text_path.name + ".tmp")
        tmp.write_text(text, encoding="utf-8", newline="")
        tmp.replace(text_path)

        title, pub = _meta(html)
        spans = passage_spans(text)
        chosen = select_snippet_spans(text, spans, args.query, args.max_snippets)
        snippets = [
            PageSnippet(
                page_id=page_id, char_start=s, char_end=e,
                passage=text[s:e], url=args.url, published_at=pub,
            )
            for s, e in chosen
        ]
        page = FetchedPage(
            page_id=page_id, url=args.url, fetched_at=now, via="direct",
            title=title, html_path=str(html_path), text_path=str(text_path),
            text_chars=len(text), n_passages=len(spans), snippets=snippets,
            elapsed_s=round(time.perf_counter() - t0, 3),
        )
        self.pages[page_id] = page
        if self._on_page is not None:
            await self._on_page(page)  # W3：落库钩子，抓取即持久
        return page

    async def _get(self, url: str) -> httpx.Response:
        client = self._injected or self._own
        if client is None:
            self._own = httpx.AsyncClient(
                timeout=httpx.Timeout(FETCH_TIMEOUT, connect=10.0),
                follow_redirects=True, headers={"User-Agent": UA},
            )
            client = self._own
        resp = await client.get(url)
        if resp.status_code == 429:
            # 瞬时限流：按 Retry-After 退避重试一次，省掉一整个 agent 轮次；
            # 仍 429 则交给上层 TOOL_ERROR 语义（模型可见，可换源）
            try:
                wait = float(resp.headers.get("retry-after", "1"))
            except ValueError:
                wait = 1.0
            await self._sleep(min(max(wait, 0.0), 3.0))
            resp = await client.get(url)
        return resp

    def get_snippet(self, snippet_id: str) -> tuple[FetchedPage, PageSnippet] | None:
        """PageResolver 协议实现：本会话内存索引。

        重启即失联（数据仍在盘上，索引没了）——可恢复场景由 W3 的
        SQLite 实现接管，claims 层只依赖协议不依赖本类。
        """
        page_id, _, _ = snippet_id.partition(":")
        page = self.pages.get(page_id)
        if page is None:
            return None
        for s in page.snippets:
            if s.snippet_id == snippet_id:
                return page, s
        return None

    async def aclose(self) -> None:
        """进程内复用的连接池由宿主关闭（CLI 进程退出即释放；W3 由 orchestrator 管）。"""
        if self._own is not None:
            await self._own.aclose()
            self._own = None

    async def __call__(self, args: FetchPageArgs) -> str:
        """Tool handler 入口：模型看到的只有片段文本（契约三层里的唯一出口）。"""
        return (await self.fetch(args)).to_model_text()


def build_fetch_tool(
    pages_dir: str | Path = "data/pages", client: httpx.AsyncClient | None = None
) -> Tool:
    return Tool(
        name="fetch_page",
        description=(
            "读取指定 URL 的网页正文，返回与 query 最相关的 1~3 个原文片段"
            "（不是全文），每个片段带 id。web_search 结果中需要深读某个来源时"
            "使用；query 传当前子问题或搜索词，用于片段筛选。"
        ),
        args_model=FetchPageArgs,
        handler=PageFetcher(pages_dir, client),
    )
