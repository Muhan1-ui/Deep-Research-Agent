"""存储与 trace 测试：落库往返 + 重启恢复（第四轮 P1-b 的最终闭环）。"""

import asyncio
import json
from datetime import date, datetime, timezone

from src.agent.claims import RawResearcherClaim, build_claims
from src.agent.schemas import Claim, Finding, RunStatus
from src.agent.storage.db import Database, SqlitePageResolver
from src.agent.tools.fetch import PageFetcher  # noqa: F401 （确认导入链）
from src.agent.tools.pages import FetchedPage, PageSnippet

S1 = "Model Context Protocol 是开放标准。"
ARCHIVE = S1 + "\n\n第二句内容，供跨页解析使用。\n"
URL = "https://example.com/mcp"
PAGE_ID = "bbb222ccc333-20261102000000-xyz789"


def make_page() -> FetchedPage:
    snippet = PageSnippet(
        page_id=PAGE_ID, char_start=0, char_end=len(S1),
        passage=S1, url=URL, published_at=None,
    )
    return FetchedPage(
        page_id=PAGE_ID, url=URL, fetched_at=datetime(2026, 11, 2, tzinfo=timezone.utc),
        via="direct", title="MCP 概览", html_path=f"{PAGE_ID}.html",
        text_path=f"{PAGE_ID}.txt", text_chars=len(ARCHIVE),
        n_passages=2, snippets=[snippet],
    )


def run(coro):
    return asyncio.run(coro)


class TestDatabase:
    def test_wal_enabled(self, tmp_path):
        db = Database(tmp_path / "t.db")
        mode = db._conn.execute("PRAGMA journal_mode").fetchone()[0]
        db.close()
        assert mode.lower() == "wal"

    def test_finding_roundtrip(self, tmp_path):
        db = Database(tmp_path / "t.db")
        claim = Claim(
            claim_id="runA:SQ1:001", sub_query_id="SQ1", text="论断",
            quote=S1, url=URL, as_of_date=date(2026, 11, 2),
            quote_locator=f"{PAGE_ID}.txt#0-{len(S1)}", extracted_by="m",
        )
        finding = Finding(
            sub_query_id="SQ1", question="问题", status=RunStatus.DEGRADED,
            search_queries=["q1", "q2"], pages_fetched=2,
            claims=[claim], error="部分失败", elapsed_s=1.5,
        )
        run(db.save_run("runA", "问题", "A1"))
        run(db.save_finding("runA", finding))
        loaded = db.load_findings("runA")
        db.close()
        assert loaded == [finding]

    def test_snippet_resolution(self, tmp_path):
        db = Database(tmp_path / "t.db")
        run(db.save_page(make_page()))
        resolver = SqlitePageResolver(db)
        located = resolver.get_snippet(f"{PAGE_ID}:0-{len(S1)}")
        assert located is not None
        page, snippet = located
        assert snippet.passage == S1 and page.title == "MCP 概览"
        assert resolver.get_snippet("ghost:1-2") is None
        db.close()

    def test_restart_recovery_closes_p1b(self, tmp_path):
        """★ 第四轮 P1-b 最终闭环：进程"重启"（新 Database + 空 fetcher）后，
        历史 snippet_id 仍可解析，claim 仍可构建。"""
        (tmp_path / f"{PAGE_ID}.txt").write_text(ARCHIVE, encoding="utf-8", newline="")
        db = Database(tmp_path / "t.db")
        run(db.save_page(make_page()))
        db.close()

        db2 = Database(tmp_path / "t.db")  # 新进程：只有盘上的库
        resolver = SqlitePageResolver(db2)
        raws = [RawResearcherClaim(text="t", quote=S1, snippet_id=f"{PAGE_ID}:0-{len(S1)}")]
        result = build_claims("runB", "SQ1", raws, resolver, extracted_by="m")  # 同步函数
        db2.close()
        assert not result.failures
        assert result.claims[0].quote == S1
        assert result.claims[0].claim_id == "runB:SQ1:001"


class TestTrace:
    def test_events_metadata_only(self, tmp_path):
        from src.agent.obs.trace import JsonlTrace

        trace = JsonlTrace("runT", tmp_path)
        trace.event("run_start", question="问题", run_id="runT")
        trace.event("sub_done", sub_query_id="SQ1", claims=3)
        trace.close()

        lines = (tmp_path / "runT.jsonl").read_text(encoding="utf-8").strip().splitlines()
        assert len(lines) == 2
        recs = [json.loads(x) for x in lines]
        assert recs[0]["event"] == "run_start" and recs[1]["claims"] == 3
        # 元数据 only：正文（片段/claim 全文）永不进 trace
        assert "passage" not in recs[0] and "quote" not in recs[1]
