"""SQLite 存储：WAL + 单写者锁 + 各表直接为 Finding/EvalRecord 服务。

设计取舍（写在这里防后人"优化"错方向）：
- 用同步 sqlite3 而非 aiosqlite：写入都是微秒级小事务，事件循环里同步执行
  的代价可忽略；引一个异步驱动换来的复杂度不值得（W7 若成瓶颈再换）；
- WAL 第一天就开：asyncio 并发写不加锁必撞 database is locked；
- 所有写穿 asyncio.Lock（单写者），读不走锁（WAL 允许并发读）；
- pages/snippets 两张表就是 PageResolver 的持久实现——第四轮审计 P1-b
  （进程重启后内存索引失联）在这里闭环。
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
from datetime import date, datetime
from pathlib import Path

from src.agent.schemas import Finding, RunStatus
from src.agent.tools.pages import FetchedPage, PageSnippet

_SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
  run_id     TEXT PRIMARY KEY,
  question   TEXT NOT NULL,
  config     TEXT NOT NULL,
  status     TEXT NOT NULL,
  created_at TEXT NOT NULL,
  finished_at TEXT,
  error      TEXT
);
CREATE TABLE IF NOT EXISTS findings (
  run_id        TEXT NOT NULL,
  sub_query_id  TEXT NOT NULL,
  question      TEXT NOT NULL,
  status        TEXT NOT NULL,
  search_queries TEXT NOT NULL DEFAULT '[]',  -- json array
  pages_fetched INTEGER NOT NULL DEFAULT 0,
  tokens_in     INTEGER NOT NULL DEFAULT 0,
  tokens_out    INTEGER NOT NULL DEFAULT 0,
  claims        TEXT NOT NULL DEFAULT '[]',   -- json array（Claim.model_dump 列表）
  error         TEXT,
  elapsed_s     REAL,
  PRIMARY KEY (run_id, sub_query_id)
);
CREATE TABLE IF NOT EXISTS pages (
  page_id     TEXT PRIMARY KEY,
  url         TEXT NOT NULL,
  title       TEXT,
  fetched_at  TEXT NOT NULL,
  via         TEXT NOT NULL,
  html_path   TEXT NOT NULL,
  text_path   TEXT NOT NULL,
  text_chars  INTEGER NOT NULL,
  n_passages  INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS snippets (
  page_id      TEXT NOT NULL,
  char_start   INTEGER NOT NULL,
  char_end     INTEGER NOT NULL,
  passage      TEXT NOT NULL,
  url          TEXT NOT NULL,
  published_at TEXT,
  PRIMARY KEY (page_id, char_start, char_end)
);
"""


class Database:
    def __init__(self, path: str | Path = "data/agent.db"):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(self.path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        self._conn.executescript(_SCHEMA)
        # 轻量迁移：老库补列（第二执行思维：已有数据不能因升级报废）
        for stmt in (
            "ALTER TABLE findings ADD COLUMN tokens_in INTEGER NOT NULL DEFAULT 0",
            "ALTER TABLE findings ADD COLUMN tokens_out INTEGER NOT NULL DEFAULT 0",
        ):
            try:
                self._conn.execute(stmt)
            except sqlite3.OperationalError:
                pass  # 列已存在
        self._conn.commit()
        self._write_lock = asyncio.Lock()  # 单写者：所有写操作穿这把锁

    # ------------------------------ 写 ------------------------------ #
    async def save_run(self, run_id: str, question: str, config: str) -> None:
        """初始状态写 'running'（DB 层字符串，不在 RunStatus 枚举里）：
        进程被杀时这行永远停在 running 而不是伪装成 ok——
        聚合成功率必须 WHERE finished_at IS NOT NULL。"""
        async with self._write_lock:
            self._conn.execute(
                "INSERT OR REPLACE INTO runs VALUES (?,?,?,?,?,NULL,NULL)",
                (run_id, question, config, "running",
                 datetime.now().astimezone().isoformat()),
            )
            self._conn.commit()

    async def finish_run(self, run_id: str, status: RunStatus, error: str | None = None) -> None:
        async with self._write_lock:
            self._conn.execute(
                "UPDATE runs SET status=?, finished_at=?, error=? WHERE run_id=?",
                (status.value, datetime.now().astimezone().isoformat(), error, run_id),
            )
            self._conn.commit()

    async def save_finding(self, run_id: str, finding: Finding) -> None:
        async with self._write_lock:
            self._conn.execute(
                "INSERT OR REPLACE INTO findings VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (
                    run_id, finding.sub_query_id, finding.question,
                    finding.status.value,
                    json.dumps(finding.search_queries, ensure_ascii=False),
                    finding.pages_fetched,
                    finding.tokens_in, finding.tokens_out,
                    json.dumps([c.model_dump(mode="json") for c in finding.claims],
                               ensure_ascii=False),
                    finding.error, finding.elapsed_s,
                ),
            )
            self._conn.commit()

    async def save_page(self, page: FetchedPage) -> None:
        async with self._write_lock:
            self._conn.execute(
                "INSERT OR REPLACE INTO pages VALUES (?,?,?,?,?,?,?,?,?)",
                (
                    page.page_id, page.url, page.title,
                    page.fetched_at.isoformat(), page.via,
                    page.html_path, page.text_path,
                    page.text_chars, page.n_passages,
                ),
            )
            for s in page.snippets:
                self._conn.execute(
                    "INSERT OR REPLACE INTO snippets VALUES (?,?,?,?,?,?)",
                    (
                        s.page_id, s.char_start, s.char_end, s.passage,
                        s.url, s.published_at.isoformat() if s.published_at else None,
                    ),
                )
            self._conn.commit()

    # ------------------------------ 读（WAL 并发读，不加锁） ------------------------------ #
    def load_findings(self, run_id: str) -> list[Finding]:
        from src.agent.schemas import Claim
        rows = self._conn.execute(
            "SELECT * FROM findings WHERE run_id=?", (run_id,)
        ).fetchall()
        findings = []
        for r in rows:
            findings.append(Finding(
                sub_query_id=r["sub_query_id"], question=r["question"],
                status=RunStatus(r["status"]),
                search_queries=json.loads(r["search_queries"]),
                pages_fetched=r["pages_fetched"],
                tokens_in=r["tokens_in"], tokens_out=r["tokens_out"],
                claims=[Claim.model_validate(c) for c in json.loads(r["claims"])],
                error=r["error"], elapsed_s=r["elapsed_s"],
            ))
        return findings

    def close(self) -> None:
        self._conn.close()


class SqlitePageResolver:
    """PageResolver 的持久实现：跨进程/重启可解析历史 snippet_id。

    返回的 FetchedPage 只携带被查询的那一个 snippet（解析 claim 用不到
    同页其余片段）；字段全部来自落库事实，不猜。
    """

    def __init__(self, db: Database):
        self._db = db

    def get_snippet(self, snippet_id: str) -> tuple[FetchedPage, PageSnippet] | None:
        page_id, _, span = snippet_id.partition(":")
        row = self._db._conn.execute(
            "SELECT * FROM pages WHERE page_id=?", (page_id,)
        ).fetchone()
        if row is None:
            return None
        try:
            start_s, end_s = span.split("-")
            start, end = int(start_s), int(end_s)
        except ValueError:
            return None
        srow = self._db._conn.execute(
            "SELECT * FROM snippets WHERE page_id=? AND char_start=? AND char_end=?",
            (page_id, start, end),
        ).fetchone()
        if srow is None:
            return None
        snippet = PageSnippet(
            page_id=page_id, char_start=start, char_end=end,
            passage=srow["passage"], url=srow["url"],
            published_at=date.fromisoformat(srow["published_at"]) if srow["published_at"] else None,
        )
        page = FetchedPage(
            page_id=page_id, url=row["url"], title=row["title"],
            fetched_at=datetime.fromisoformat(row["fetched_at"]), via=row["via"],
            html_path=row["html_path"], text_path=row["text_path"],
            text_chars=row["text_chars"], n_passages=row["n_passages"],
            snippets=[snippet],
        )
        return page, snippet
