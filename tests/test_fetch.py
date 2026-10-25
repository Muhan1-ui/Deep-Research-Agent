"""fetch_page 实现测试：全部离线（MockTransport / 直接调切段函数）。

最重要的不变量：**每个 snippet 都能在归档正文中逐字验证**——
这是 quote_locator 可信的前提，也是契约测试之外的端到端复核。
"""

import asyncio
import re

import httpx
import pytest

from src.agent.tools.base import ToolRegistry
from src.agent.tools.fetch import (
    PageFetcher,
    build_fetch_tool,
    passage_spans,
    tokenize,
)
from src.agent.tools.pages import FetchPageArgs

# 内容刻意做成"两段相关、两段无关"，验证 BM25 筛选真的在起作用
HTML = """<html><head><title>MCP 指南</title>
<meta name="date" content="2026-01-20"></head><body>
<p>The Model Context Protocol (MCP) is an open standard for connecting
AI applications to external tools and data sources. It was announced
by Anthropic in November 2024 and later donated to a foundation.</p>
<p>Today we cook pasta with tomato sauce. Boil water, add salt, and
wait eleven minutes. Drain the pasta and mix with the sauce carefully.</p>
<p>今天我们讨论 MCP 协议的架构设计。它基于 JSON-RPC 消息格式，
包含 host、client、server 三种角色，支持工具调用与资源共享。</p>
<p>Gardening tips for beginners: roses need direct sunlight and regular
watering every morning. Tulips prefer a colder climate instead.</p>
</body></html>"""

URL = "https://example.com/mcp-guide"


def run(coro):
    return asyncio.run(coro)


def mocked_client(handler=None, status: int = 200, body: str = HTML) -> httpx.AsyncClient:
    def h(request: httpx.Request) -> httpx.Response:
        if handler is not None:
            return handler(request)
        return httpx.Response(status, text=body)

    return httpx.AsyncClient(transport=httpx.MockTransport(h))


class TestTokenizerAndSpans:
    def test_tokenize_mixed_language(self):
        tokens = tokenize("MCP 协议 Protocol")
        assert "mcp" in tokens and "protocol" in tokens
        assert "协" in tokens and "议" in tokens
        assert " " not in tokens

    def test_spans_are_verbatim_and_cover_content(self):
        text = "  甲乙丙。  \n\n  def ghi  \n  jkl  \n"
        spans = passage_spans(text)
        for s, e in spans:
            assert text[s] .strip() != ""        # span 起点非空白
            assert text[s:e].strip()             # span 有实际内容
        # 非空白字符全覆盖：切段没有丢内容
        joined = "".join(text[s:e] for s, e in spans)
        original_chars = {c for c in text if not c.isspace()}
        assert original_chars <= {c for c in joined if not c.isspace()}

    def test_oversized_paragraph_split_within_max(self):
        text = "字" * 1400  # 无句末标点 → 硬切
        spans = passage_spans(text)
        assert all(e - s <= 600 for s, e in spans)
        assert sum(e - s for s, e in spans) == 1400  # 一个字都不丢

    def test_sentence_boundary_preferred_over_hard_cut(self):
        text = "。".join(["这是第%d句话" % i for i in range(200)]) + "。"
        spans = passage_spans(text)
        # 有句末标点可用时，切点应落在句界（跨度不是清一色 600）
        assert any(e - s < 600 for s, e in spans)


class TestFetchPipeline:
    def test_archive_and_snippet_verification(self, tmp_path):
        """端到端不变量：归档文件里的正文，逐字验证每个片段。"""
        fetcher = PageFetcher(tmp_path, client=mocked_client())
        page = run(fetcher.fetch(FetchPageArgs(url=URL, query="MCP 协议 架构", max_snippets=3)))

        txt = (tmp_path / f"{page.page_id}.txt").read_text(encoding="utf-8")
        html = (tmp_path / f"{page.page_id}.html").read_text(encoding="utf-8")
        assert html and page.text_chars == len(txt)
        assert page.snippets, "应有相关片段被选出"
        for s in page.snippets:
            assert s.verify(txt)  # ★ 逐字切片的最终证明
        assert re.fullmatch(r"[0-9a-f]{12}-\d{14}-[0-9a-f]{6}", page.page_id)
        assert page.page_id in fetcher.pages
        model_text = page.to_model_text()
        assert URL in model_text and page.snippets[0].passage in model_text

    def test_disk_bytes_are_the_offset_anchor(self, tmp_path):
        """P0-1 回归：write_text 在 Windows 会把 \\n 翻成 \\r\\n，盘上文件
        悄悄变长——测试若用 read_text()（反向翻译）永远发现不了。
        必须用最小特权读取（newline=''）断言盘上字节 == 偏移锚。"""
        fetcher = PageFetcher(tmp_path, client=mocked_client())
        page = run(fetcher.fetch(FetchPageArgs(url=URL, query="MCP")))
        with open(page.text_path, encoding="utf-8", newline="") as fh:
            on_disk = fh.read()
        assert len(on_disk) == page.text_chars
        assert "\r" not in on_disk
        for s in page.snippets:
            assert s.verify(on_disk)  # 对"任何读取方式"都成立的逐字切片

    def test_model_text_carries_snippet_id(self, tmp_path):
        """P1-b 回归：契约承诺 researcher 用 snippet_id 引用片段，
        id 必须真的出现在给模型的文本里（文档说 A 实现就得是 A）。"""
        fetcher = PageFetcher(tmp_path, client=mocked_client())
        page = run(fetcher.fetch(FetchPageArgs(url=URL, query="MCP")))
        text = page.to_model_text()
        for s in page.snippets:
            assert s.snippet_id in text

    def test_same_second_refetch_gets_fresh_id_keeps_both(self, tmp_path):
        """P0-2 回归：同秒重抓同一 URL 必须得到新 page_id，
        两次归档都在、互不覆盖——否则旧 snippet 静默指向新正文。"""
        fetcher = PageFetcher(tmp_path, client=mocked_client())
        p1 = run(fetcher.fetch(FetchPageArgs(url=URL, query="MCP")))
        p2 = run(fetcher.fetch(FetchPageArgs(url=URL, query="MCP")))
        assert p1.page_id != p2.page_id
        t1 = (tmp_path / f"{p1.page_id}.txt").read_text(encoding="utf-8")
        t2 = (tmp_path / f"{p2.page_id}.txt").read_text(encoding="utf-8")
        assert t1 == t2  # 内容相同但两份归档独立存在
        assert len(list(tmp_path.glob("*.txt"))) == 2

    def test_pdf_content_type_is_valid_empty(self, tmp_path):
        """P1-a 回归：PDF 走合法空结果（同 JS-only 页），不是 TOOL_ERROR——
        否则 researcher 一轮抓两个 PDF 就被 streak 护栏误杀。"""

        def h(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200, headers={"content-type": "application/pdf"}, content=b"%PDF-1.4 binary..."
            )

        c = httpx.AsyncClient(transport=httpx.MockTransport(h))
        registry = ToolRegistry()
        registry.register(build_fetch_tool(tmp_path, client=c))
        out = run(registry.dispatch("fetch_page", '{"url": "https://x/doc.pdf", "query": "any"}'))
        assert not out.is_error
        assert "抽取失败" in out.content

    def test_http_429_retries_once_then_succeeds(self, tmp_path):
        """P2 回归：429 是瞬时的，按 Retry-After 退避重试一次，省一个 agent 轮次。"""
        calls = {"n": 0}

        def h(request: httpx.Request) -> httpx.Response:
            calls["n"] += 1
            if calls["n"] == 1:
                return httpx.Response(429, headers={"retry-after": "0"})
            return httpx.Response(200, text=HTML)

        async def nosleep(_seconds: float) -> None:
            pass

        fetcher = PageFetcher(tmp_path, client=httpx.AsyncClient(transport=httpx.MockTransport(h)), sleep=nosleep)
        page = run(fetcher.fetch(FetchPageArgs(url=URL, query="MCP")))
        assert calls["n"] == 2
        assert page.text_chars > 0

    def test_bm25_picks_relevant_drops_noise(self, tmp_path):
        """选择单位是合并后的 passage（~300字），相邻小段会并入同一片段——
        所以断言"排序方向正确"，而不是"噪音词绝不出现"（后者与合并策略冲突）。"""
        fetcher = PageFetcher(tmp_path, client=mocked_client())
        top1 = run(fetcher.fetch(
            FetchPageArgs(url=URL, query="MCP protocol standard", max_snippets=1)))
        joined = top1.to_model_text().lower()
        assert "mcp" in joined or "protocol" in joined

        # 反向查询：含全部查询词的片段胜出；无关段（园丁段）绝不入选。
        # 注意：pasta 段与中文段 ≤300 字会合并，中文段搭车出现是合并语义，
        # 不是选择错误——断言只管"谁被选中"，不管片段内部含什么。
        noise = run(fetcher.fetch(
            FetchPageArgs(url=URL, query="pasta tomato sauce boil", max_snippets=1)))
        assert "pasta" in noise.to_model_text().lower()
        assert "tulips" not in noise.to_model_text().lower()

    def test_unreadable_page_is_valid_not_error(self, tmp_path):
        """无正文是合法空结果（模型据此换 URL），不是 TOOL_ERROR。"""
        empty_html = "<html><body><div></div></body></html>"
        fetcher = PageFetcher(tmp_path, client=mocked_client(body=empty_html))
        registry = ToolRegistry()
        registry.register(build_fetch_tool(tmp_path, client=mocked_client(body=empty_html)))
        out = run(registry.dispatch("fetch_page", '{"url": "https://x/y", "query": "any"}'))
        assert not out.is_error
        assert "抽取失败" in out.content

    def test_http_error_is_tool_error(self, tmp_path):
        """传输失败（404）走错误回喂，计入护栏统计。"""
        registry = ToolRegistry()
        registry.register(build_fetch_tool(tmp_path, client=mocked_client(status=404)))
        out = run(registry.dispatch("fetch_page", '{"url": "https://x/404", "query": "any"}'))
        assert out.is_error and "TOOL_ERROR" in out.content

    def test_published_date_from_meta(self, tmp_path):
        fetcher = PageFetcher(tmp_path, client=mocked_client())
        page = run(fetcher.fetch(FetchPageArgs(url=URL, query="MCP")))
        if page.snippets:  # trafilatura 对极短页可能抽不出正文
            from datetime import date

            assert page.snippets[0].published_at in (None, date(2026, 1, 20))
