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
python -m src.agent "Rust 2024 Edition 有什么变化？" --max-turns 8
```

A0 = 单 Agent 消融基线（W6 用）；答案打到 stdout，运行统计打到 stderr。

## 路线图

- [x] W0 三套 schema 定稿（claim / findings / eval record）
- [x] W1 原生 tool-calling loop + web_search，CLI 出带来源答案（loop 111 行；51 项单测含交叉审计补漏）
- [ ] W2 loop 加固 + fetch_page 片段筛选（切段 + BM25）
- [ ] W3 Planner + Researcher 并行 + SQLite 落库 + JSONL trace
- [ ] W4 Verifier + Writer + Gradio demo（录演示视频）
- [ ] W5 50 题评测集 + runner + 四层漏斗
- [ ] W6 消融 A0/A1/A2 + 人工抽检
