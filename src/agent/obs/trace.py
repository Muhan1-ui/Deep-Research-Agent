"""JSONL trace：一次 run 一个文件，只记元数据，正文永不进 trace。

正文（网页片段/claim 全文）都在归档与 SQLite 里，trace 只需要回答：
每一步发生了什么、花了多少、用了哪个模型。免费看板 = jq。
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path


class JsonlTrace:
    def __init__(self, run_id: str, trace_dir: str | Path = "data/traces"):
        self.run_id = run_id
        path = Path(trace_dir)
        path.mkdir(parents=True, exist_ok=True)
        self.path = path / f"{run_id}.jsonl"
        # newline=""：trace 也要锚定字节（换行翻译的教训，第三轮审计 P0-1）
        self._fh = open(self.path, "a", encoding="utf-8", newline="")

    def event(self, name: str, **fields) -> None:
        rec = {"ts": datetime.now(timezone.utc).isoformat(), "event": name, **fields}
        self._fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
        self._fh.flush()

    def close(self) -> None:
        self._fh.close()
