"""--mock 运行模式：无 key、无网络跑通 A2，且内部校验一条都没被跳过。"""

import asyncio
import re
import socket

from src.agent.config import Settings
from src.agent.orchestrator.run import run_research
from src.agent.schemas import RunStatus


def test_mock_env_flag(monkeypatch):
    """MOCK=1 走环境变量；_env_file=None 隔离仓库 .env。"""
    monkeypatch.setenv("MOCK", "1")
    assert Settings(_env_file=None, llm_api_key="").mock is True
    monkeypatch.setenv("MOCK", "0")
    assert Settings(_env_file=None, llm_api_key="").mock is False


def test_mock_a2_report_without_keys_or_network(tmp_path, monkeypatch):
    # 只放行 asyncio 事件循环自带的 loopback socketpair；任何外网连接都失败
    real_connect = socket.socket.connect

    def no_network(sock, address, *args, **kwargs):
        host = address[0] if isinstance(address, tuple) else address
        if host in ("127.0.0.1", "::1", "localhost"):
            return real_connect(sock, address, *args, **kwargs)
        raise AssertionError(f"mock 模式不应发起外网连接: {address!r}")

    monkeypatch.setattr(socket.socket, "connect", no_network)

    settings = Settings(
        mock=True,
        llm_api_key="",          # 关键：无 key
        tavily_api_key="",       # 关键：无搜索 key
        database_path=str(tmp_path / "agent.db"),
        trace_dir=str(tmp_path / "traces"),
        pages_dir=str(tmp_path / "pages"),
    )

    result = asyncio.run(
        run_research("MCP 协议的核心架构", settings=settings, config="A2", mock=True)
    )

    # 产出：报告 + 非零引用
    assert result.report is not None
    assert len(result.report.citation_map) > 0
    assert result.total_claims > 0
    assert all(f.status is RunStatus.OK for f in result.findings)
    # 所有抓取都来自 mock 固定资料
    assert all(
        c.url.startswith("https://example.invalid/")
        for f in result.findings for c in f.claims
    )
    # 引用完整性：正文每个 [n] 都能在 citation_map 中找到，map 的 claim_id 都真实存在
    prose = [s for s in result.report.sections if s.heading != "来源"]
    body = "\n".join(s.body for s in prose)
    cited = {int(n) for n in re.findall(r"\[(\d+)\]", body)}
    assert cited and cited <= {int(n) for n in result.report.citation_map}

    # 三个子问题各读一份资料 → 9 条 claim 是 9 句互不相同的内容
    assert result.total_claims == 9
    prose_lines = [ln for s in prose for ln in s.body.splitlines() if ln.strip()]
    assert len(prose_lines) == 9
    assert len(prose_lines) == len(set(prose_lines))  # 按正文行去重后行数不变
    assert len(result.report.citation_map) >= 3
    assert result.report.deduped == 0  # 资料本就不同，没有重复可归并
    all_ids = {c.claim_id for f in result.findings for c in f.claims}
    assert set(result.report.citation_map.values()) <= all_ids

    # 校验没被跳过：A2 的每条 claim 都有核验结果，且不是"未核验"兜底
    assert len(result.verifications) == result.total_claims
    assert all(not v.unverified for v in result.verifications)
    assert all(v.checked_by == "mock" for v in result.verifications)
    # 句级锚定真实执行：quote 必须等于归档正文的 locator 切片
    # （build_claims 已按此回填；这里从落库事实再验一次）
    from src.agent.storage.db import Database
    db = Database(settings.database_path)
    persisted = db.load_findings(result.run_id)
    db.close()
    for f in persisted:
        for c in f.claims:
            path, _, span = c.quote_locator.rpartition("#")
            start, end = map(int, span.split("-"))
            with open(path, encoding="utf-8", newline="") as fh:
                assert fh.read()[start:end] == c.quote

    # LLM 真身全程是 mock（models_seen 来自响应 model 字段）
    assert set(result.models_seen.values()) == {"mock"}
