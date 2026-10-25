"""fetch_page 契约测试：W2 实现之前先把数据形状钉死（W0 原则复用）。"""

from datetime import date, datetime

import pytest
from pydantic import ValidationError

from src.agent.tools.pages import FetchPageArgs, FetchedPage, PageSnippet

ARCHIVE = "Rust 1.85 稳定了 2024 Edition。" * 3  # 45 个 codepoint


def snippet(**over) -> PageSnippet:
    base = dict(
        page_id="abc123def456-20261101120000",
        char_start=0,
        char_end=15,
        passage=ARCHIVE[0:15],
        url="https://example.com/rust",
        published_at=date(2026, 1, 20),
    )
    base.update(over)
    return PageSnippet(**base)


def fetched(**over) -> FetchedPage:
    base = dict(
        page_id="abc123def456-20261101120000",
        url="https://example.com/rust",
        fetched_at=datetime(2026, 11, 1, 12, 0, 0),
        via="direct",
        title="Rust 1.85 Release",
        html_path="data/pages/abc123def456-20261101120000.html",
        text_path="data/pages/abc123def456-20261101120000.txt",
        text_chars=len(ARCHIVE),
        n_passages=3,
        snippets=[snippet()],
        elapsed_s=0.8,
    )
    base.update(over)
    return FetchedPage(**base)


class TestPageSnippet:
    def test_locator_format_matches_claim_contract(self):
        # Claim.quote_locator 在 W0 定为 `{路径}#{起}-{止}`，这里必须逐字对齐
        loc = snippet().locator
        assert loc == "data/pages/abc123def456-20261101120000.txt#0-15"

    def test_passage_must_be_verbatim_slice(self):
        # 改写过的"片段"（长度 ≠ 跨度）构造即拒——切片不是摘要
        with pytest.raises(ValidationError, match="逐字切片"):
            snippet(char_end=20, passage=ARCHIVE[0:15])  # 跨度 20 但 passage 15 字

    def test_reversed_or_empty_span_rejected(self):
        with pytest.raises(ValidationError, match="偏移非法"):
            snippet(char_start=10, char_end=10, passage="")

    def test_verify_against_archive(self):
        s = snippet(char_start=15, char_end=30, passage=ARCHIVE[15:30])
        assert s.verify(ARCHIVE) is True
        assert s.verify("完全不同的正文" * 5) is False

    def test_snippet_id_format(self):
        assert snippet().snippet_id == "abc123def456-20261101120000:0-15"


class TestFetchedPage:
    def test_snippet_from_other_page_rejected(self):
        with pytest.raises(ValidationError, match="不属于本页"):
            fetched(snippets=[snippet(page_id="otherpage-20261101120000")])

    def test_out_of_range_offset_rejected(self):
        # 偏移指向归档正文之外 = 造假偏移，当场抓住
        with pytest.raises(ValidationError, match="越界"):
            fetched(text_chars=5, snippets=[snippet()])

    def test_model_text_contains_only_snippets(self):
        text = fetched().to_model_text()
        assert "Rust 1.85 Release" in text
        assert "https://example.com/rust" in text
        assert ARCHIVE[0:15] in text
        assert "[片段1/1" in text          # 格式：[片段i/n · id=...]
        assert snippet().snippet_id in text  # id 必须真的发给模型（P1-b）

    def test_model_text_without_snippets(self):
        assert "抽取失败" in fetched(snippets=[], n_passages=0).to_model_text()


class TestFetchPageArgs:
    def test_max_snippets_capped_at_3(self):
        with pytest.raises(ValidationError):
            FetchPageArgs(url="https://x", query="q", max_snippets=4)

    def test_query_required(self):
        with pytest.raises(ValidationError):
            FetchPageArgs(url="https://x", query="")
