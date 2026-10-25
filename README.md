# Deep Research Agent

输入一个研究问题 → 自动分解、并行检索、交叉验证 → 输出带引用来源的结构化报告。

- 项目计划（6 周核心 + buffer、架构、评测与消融方案）：[PLAN.md](./PLAN.md)
- 数据契约与指标口径：`src/agent/schemas.py`（W0 定稿，口径定义写在文件头注释）
- 状态：**W1 ✅ 原生 tool-calling loop + web_search + CLI（A0 模式）**

## 开发

```bash
uv sync                          # 或: python -m venv .venv && pip install -e . pytest
uv run python -m src.agent.schemas   # 契约自检：手算一遍指标口径
uv run pytest -q                     # 全部单测（无网络、无 API key）
```

## 使用（需要先配置 .env）

```bash
cp .env.example .env               # 填入 LLM_API_KEY（必需）和 TAVILY_API_KEY（搜索）

# A0：单 Agent 消融基线（搜索→作答）
python -m src.agent ask "Rust 2024 Edition 有什么变化？" --max-turns 8

# 多 Agent 研究管线（W3）：规划 → 并行检索 → 句级锚定 claims → 落库
python -m src.agent research "MCP 协议的核心架构与生态现状" --concurrency 4
```

研究明细：`data/agent.db`（SQLite WAL）与 `data/traces/<run_id>.jsonl`（元数据 trace）；网页归档在 `data/pages/`。

> 抓取合规：fetch_page 为低频研究用途直连抓取，未实现 robots.txt 强制拦截；使用时请遵守目标站点的服务条款与访问频率约束。

## 路线图

- [x] W0 三套 schema 定稿（claim / findings / eval record）
- [x] W1 原生 tool-calling loop + web_search，CLI 出带来源答案（loop 111 行）
- [x] W2 fetch_page 片段管线：切段+BM25、双层归档、偏移锚定（四轮对抗审计修复见 docs/audit）
- [x] W3 Planner + 并行 Researcher + SQLite WAL 落库 + JSONL trace（真实环境：6 子问题并行、54 claims、3 分钟）
- [ ] W4 Verifier + Writer + Gradio demo（录演示视频）
- [ ] W5 50 题评测集 + runner + 四层漏斗
- [ ] W6 消融 A0/A1/A2 + 人工抽检
