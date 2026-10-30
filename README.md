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

# 多 Agent 研究管线：规划 → 并行检索 → 句级锚定 claims → 落库
python -m src.agent research "MCP 协议的核心架构与生态现状" --concurrency 4

# A2 = 完整系统（Verifier 逐条核验 + Writer 程序化引用报告）
python -m src.agent research "SSE 与 WebSocket 在 LLM 流式输出的取舍" --config A2

# Gradio 演示界面（浏览器打开 http://127.0.0.1:7860）
python app/gradio_ui.py
```

研究明细：`data/agent.db`（SQLite WAL）与 `data/traces/<run_id>.jsonl`（元数据 trace）；网页归档在 `data/pages/`。
注意：trace 里 token 在 `sub_done`（逐子任务 `tokens_*`）与 `run_done`（全量 `total_tokens_*`）各记一次，jq 全量求和会双计——按 event 过滤。成本实测（deepseek-flash，2026-10）：一次完整 A2 研究 ≈ 550k~570k input tokens ≈ **¥0.6（空闲）/ ¥1.3（高峰）**——已含修复轮重检索与失败子任务的开销（失败的子问题几乎和成功的一样贵，这正是 W7 缓存与模型分级要解决的）。

> 抓取合规：fetch_page 为低频研究用途直连抓取，未实现 robots.txt 强制拦截；使用时请遵守目标站点的服务条款与访问频率约束。

## 路线图

- [x] W0 三套 schema 定稿（claim / findings / eval record）
- [x] W1 原生 tool-calling loop + web_search，CLI 出带来源答案（loop 111 行）
- [x] W2 fetch_page 片段管线：切段+BM25、双层归档、偏移锚定（四轮对抗审计修复见 docs/audit）
- [x] W3 Planner + 并行 Researcher + SQLite WAL 落库 + JSONL trace（真实环境：6 子问题并行、54 claims、3 分钟）
- [x] W4 Verifier + Writer（程序化 citation_map）+ A2 配置 + Gradio demo（真实环境：51 claims → 核验 35 supported → 报告引用 37）
- [x] W5 评测 runner：`python -m eval.runner`（judge 跨厂商 temp=0、A0/A1/A2 批跑、漏斗+成对指标、eval_records 落库、汇总表落盘）；50 题数据集人工校对进行中
- [x] W6 消融全矩阵（10 题 × A0/A1/A2，数据集 v4d1c7846）：A0→A2 忠实度 0.50→0.70、覆盖率 0.78→0.90；judge 重测一致性 30/30，盲标表随仓库待补（docs/w6-report.md）
