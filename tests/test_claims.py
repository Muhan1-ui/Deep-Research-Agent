"""claims 解析层测试：snippet_id → quote_locator 的唯一通道。

两条最强断言：
1. 端到端锚定：locator 在盘上归档（newline='' 最小特权读取）切出的字符串
   逐字等于 quote——句级锚定 + 回填后这无条件成立；
2. 所有用例都覆盖"第二次执行"维度（双 run / 重启 / 歧义 / 归一化回填）——
   第四轮审计的教训：bug 全在第二次执行里，单轮测试天然照不到。

偏移量全部由代码计算，不手数。
"""

from datetime import date, datetime, timezone

from src.agent.claims import RawResearcherClaim, build_claims
from src.agent.tools.fetch import PageFetcher
from src.agent.tools.pages import FetchedPage, PageSnippet

# S2 内嵌换行+缩进：模拟真实片段里的软换行，供空白容忍/回填用例使用
S1 = "Model Context Protocol 是开放标准。"
S2 = "它基于 JSON-RPC 2.0，包含\n  host、client、server 三种角色。"
ARCHIVE = S1 + "\n\n" + S2 + "\n2025 年底规范捐赠给 Linux 基金会治理。\n"

P1 = (0, len(S1))
P2 = (len(S1) + 2, len(S1) + 2 + len(S2))
URL = "https://example.com/mcp"
PAGE_ID = "aaa111bbb222-20261101000000-abc123"


def make_fetcher_with_page(tmp_path, pub: date | None = None) -> PageFetcher:
    """手工构造带归档文件的 fetcher（无网络）：page 记录 + 盘上锚定文本。"""
    text_path = tmp_path / f"{PAGE_ID}.txt"
    with open(text_path, "w", encoding="utf-8", newline="") as fh:
        fh.write(ARCHIVE)

    def snip(span: tuple[int, int]) -> PageSnippet:
        return PageSnippet(
            page_id=PAGE_ID, char_start=span[0], char_end=span[1],
            passage=ARCHIVE[span[0]:span[1]], url=URL, published_at=pub,
        )

    page = FetchedPage(
        page_id=PAGE_ID, url=URL, fetched_at=datetime(2026, 11, 1, tzinfo=timezone.utc),
        via="direct", title="MCP 概览",
        html_path=str(tmp_path / f"{PAGE_ID}.html"),
        text_path=str(text_path), text_chars=len(ARCHIVE),
        n_passages=2, snippets=[snip(P1), snip(P2)],
    )
    fetcher = PageFetcher(tmp_path)
    fetcher.pages[PAGE_ID] = page
    return fetcher


def sid(which: int) -> str:
    span = (P1, P2)[which]
    return f"{PAGE_ID}:{span[0]}-{span[1]}"


def disk_slice(locator: str) -> str:
    """按 locator 从盘上归档切出字符串（最小特权读取）。"""
    path, _, span = locator.partition("#")
    start, end = (int(x) for x in span.split("-"))
    with open(path, encoding="utf-8", newline="") as fh:
        return fh.read()[start:end]


class TestBuildClaims:
    def test_happy_path_and_disk_anchor(self, tmp_path):
        """locator 切盘上文件必须逐字等于 quote——句级锚定的最终证明。"""
        fetcher = make_fetcher_with_page(tmp_path, pub=date(2026, 1, 20))
        raws = [
            RawResearcherClaim(text="MCP 是开放标准", quote=S1, snippet_id=sid(0)),
            RawResearcherClaim(text="MCP 基于 JSON-RPC 2.0", quote=S2, snippet_id=sid(1)),
        ]
        result = build_claims("run1", "SQ1", raws, fetcher, extracted_by="deepseek-flash")
        assert not result.failures
        assert [c.claim_id for c in result.claims] == ["run1:SQ1:001", "run1:SQ1:002"]
        for c in result.claims:
            assert c.url == URL and c.extracted_by == "deepseek-flash"
            assert c.as_of_date == date(2026, 1, 20)  # 发布日优先
            assert disk_slice(c.quote_locator) == c.quote  # ★ 端到端锚定

    def test_claim_id_unique_across_runs_and_configs(self, tmp_path):
        """P1-a：两次 run / 消融两组不得共用 claim_id（SQLite 主键与跨组
        数据污染都栽在这里）。"""
        fetcher = make_fetcher_with_page(tmp_path)
        raws = [RawResearcherClaim(text="t", quote=S1, snippet_id=sid(0))]
        r1 = build_claims("runA", "SQ1", raws, fetcher, extracted_by="m")
        r2 = build_claims("runB", "SQ1", raws, fetcher, extracted_by="m")
        assert r1.claims[0].claim_id != r2.claims[0].claim_id
        assert r1.claims[0].claim_id == "runA:SQ1:001"

    def test_restart_with_empty_resolver_reports_unknown(self, tmp_path):
        """P1-b：进程重启模拟——归档仍在盘上（数据在），内存索引为空。
        全部解析失败但结构不崩；恢复能力由 W3 的 SQLite 实现接管。"""
        make_fetcher_with_page(tmp_path)  # 先把归档写上盘
        empty = PageFetcher(tmp_path)     # 新进程：pages 为空
        assert empty.get_snippet(sid(0)) is None
        raws = [RawResearcherClaim(text="t", quote=S1, snippet_id=sid(0))]
        result = build_claims("run1", "SQ1", raws, empty, extracted_by="m")
        assert result.failures[0].reason == "unknown_snippet"
        assert (tmp_path / f"{PAGE_ID}.txt").exists()  # 数据还在，只是索引没了

    def test_whitespace_collapsed_quote_backfilled_to_verbatim(self, tmp_path):
        """P2-a：模型把换行+缩进折叠成单空格——定位成功后 quote 回填为
        归档原文切片，"逐字复制"承诺无条件成立（quote ≡ locator 切片）。"""
        fetcher = make_fetcher_with_page(tmp_path)
        collapsed = " ".join(S2.split())  # "\n  " → " "
        assert collapsed != S2  # 用例前提：模型输出与原文确实有空白差异
        raws = [RawResearcherClaim(text="三种角色", quote=collapsed, snippet_id=sid(1))]
        result = build_claims("run1", "SQ1", raws, fetcher, extracted_by="m")
        assert not result.failures
        claim = result.claims[0]
        assert claim.quote == S2                    # 回填为含真实换行的原文
        assert "\n" in claim.quote                  # 不是模型的归一化形态
        assert disk_slice(claim.quote_locator) == claim.quote  # 三点一线

    def test_ambiguous_quote_fails_with_guidance(self, tmp_path):
        """P2-b：通用短句在片段中出现两次 → ambiguous_quote（不静默锚第一处），
        detail 告诉模型引用更长上下文。"""
        dup_archive = "系统返回正常。写入新数据。系统返回正常。流程结束。\n"
        text_path = tmp_path / "dup.txt"
        with open(text_path, "w", encoding="utf-8", newline="") as fh:
            fh.write(dup_archive)
        snippet = PageSnippet(
            page_id=PAGE_ID, char_start=0, char_end=len(dup_archive.rstrip("\n")),
            passage=dup_archive.rstrip("\n"), url=URL, published_at=None,
        )
        page = FetchedPage(
            page_id=PAGE_ID, url=URL, fetched_at=datetime(2026, 11, 1, tzinfo=timezone.utc),
            via="direct", title=None, html_path="x", text_path=str(text_path),
            text_chars=len(dup_archive.rstrip("\n")), n_passages=1, snippets=[snippet],
        )
        fetcher = PageFetcher(tmp_path)
        fetcher.pages[PAGE_ID] = page

        raws = [RawResearcherClaim(text="状态", quote="系统返回正常。", snippet_id=snippet.snippet_id)]
        result = build_claims("run1", "SQ1", raws, fetcher, extracted_by="m")
        assert result.failures[0].reason == "ambiguous_quote"
        assert "更长" in result.failures[0].detail
        assert not result.claims

    def test_as_of_date_falls_back_to_fetch_date(self, tmp_path):
        fetcher = make_fetcher_with_page(tmp_path, pub=None)
        raws = [RawResearcherClaim(text="t", quote=S1, snippet_id=sid(0))]
        result = build_claims("run1", "SQ1", raws, fetcher, extracted_by="m")
        # astimezone 后取本地日期（测试机本地时区下 2026-11-01T00:00Z 不跨日）
        assert result.claims[0].as_of_date in (date(2026, 10, 31), date(2026, 11, 1))

    def test_unknown_snippet_id_is_failure_not_crash(self, tmp_path):
        fetcher = make_fetcher_with_page(tmp_path)
        raws = [RawResearcherClaim(text="t", quote="q", snippet_id="ghost:1-2")]
        result = build_claims("run1", "SQ1", raws, fetcher, extracted_by="m")
        assert not result.claims
        assert result.failures[0].reason == "unknown_snippet"

    def test_paraphrased_quote_is_failure_not_guesswork(self, tmp_path):
        """模型改写了引文 → 定位失败 → 数据返回，绝不猜近似位置。"""
        fetcher = make_fetcher_with_page(tmp_path)
        raws = [RawResearcherClaim(
            text="MCP 是开放标准", quote="MCP 是一个开放的标准协议", snippet_id=sid(0))]
        result = build_claims("run1", "SQ1", raws, fetcher, extracted_by="m")
        assert result.failures[0].reason == "quote_not_located"

    def test_failures_report_raw_index_for_retry(self, tmp_path):
        """failure.index 指向原始输出位置：W3 拿它回喂 researcher 重试。"""
        fetcher = make_fetcher_with_page(tmp_path)
        raws = [
            RawResearcherClaim(text="好", quote=S1, snippet_id=sid(0)),
            RawResearcherClaim(text="坏", quote="编造的引文", snippet_id=sid(0)),
        ]
        result = build_claims("run1", "SQ1", raws, fetcher, extracted_by="m")
        assert len(result.claims) == 1 and len(result.failures) == 1
        assert result.failures[0].index == 1
        assert result.failures[0].reason == "quote_not_located"
        # 成功项的编号保持原始位置（:001 好，:002 缺席），不因失败而错位
        assert result.claims[0].claim_id == "run1:SQ1:001"
