# Deep Research Agent — 项目计划 v2

> 多智能体深度研究与报告生成系统
> 周期：2026-10-25 → 2027-01-17（**6 周核心 + 6 周 buffer**）
> 每周投入 ~15h，其中 **3~4h 刷题/八股，雷打不动**（笔试过不了一切归零）
> 目标：2027 春招 Agent 应用开发岗主力项目；Python 后端岗辅助项目

---

## 0. v2 改版说明

| 改动 | 原因 |
|---|---|
| 主线压缩到 6 周，W7 起全部 buffer | 扣除毕设/春节/春招准备，真实可用 120~140h；原 12 周满排是按在职工程师估的 |
| 消融指标改为忠实度×覆盖率成对报告 + claim 数 | 防"A2 靠少写刷忠实度"，否则结论不成立 |
| 新增 W0：先定三套 schema 再写代码 | 指标设计决定 schema，schema 决定代码；W5 才发现要返工四周 |
| 评测集 100→50 题；人工抽检缩到 30~50 条 claim | 面试官问方法论不问题数；400 条人工判定是时间黑洞 |
| 前端定死 Gradio，React 移出主线 | 5 天时间盒必超；对简历数字零贡献 |
| MCP 改为导出式封装（半天），主链路进程内直连 | 主系统做 MCP client 只增加 IPC 与调试复杂度 |
| trace 自建 JSONL，Langfuse 降为可选项 | 同样的数字，零运维；Langfuse 留作 buffer 一天适配 |
| 博客从 W12 提前到 W4/W6 | 普通本科的卡点在简历筛选，曝光要从做出 demo 就开始积累 |
| 新增 kill criteria | 到点执行，防止沉没成本绑架 |

---

## 1. 项目定位

**一句话**：输入研究问题 → 自动分解、并行检索、交叉验证 → 输出带引用的结构化报告。

**成功标准**：
- [ ] 端到端：50 题评测集任务成功率 ≥ 90%
- [ ] 消融可信：忠实度与覆盖率**成对**报告（含 F1），并列出每组 claim 数 / 报告字数；A2 相对 A1 忠实度提升 ≥ 10pp 且覆盖率跌幅可见、可解释
- [ ] 评测可信：30~50 条 claim 人工抽检（富集非 supported 样本），观测一致率 ≥ 85% + 混淆矩阵
- [ ] 可复现：每题带 `as_of_date`；README 提供 `--mock` 模式，无 API key 也能 10 分钟跑通
- [ ] 成本：单任务 ≤ 1 元；全程预算上限 500 元（预计实际 100~300 元）

---

## 2. 技术选型

| 层 | 选择 | 说明 |
|---|---|---|
| 语言 | Python 3.12 + uv | |
| LLM 接口 | OpenAI SDK（base_url 切换厂商） | 开发期用 GLM-4-Flash 免费额度；正式跑批用 DeepSeek |
| 模型分级 | researcher/verifier 用便宜模型，planner/writer 用强模型 | 降本一个数量级，有数字支撑 |
| **评测 judge** | **与 Verifier/Writer 不同厂商**（如 GLM 判 DeepSeek 系统） | 消同源偏差，一句话的面试加分 |
| 搜索 | Tavily（抽象层可切 SearXNG/博查） | 配额警戒见 §8；搜索结果全量缓存 |
| 网页抽取 | **默认自抓 + trafilatura；Jina Reader 仅作增强** | Jina 免费限速紧，不能当主路径 |
| 片段筛选 | 切段 + BM25（rank_bm25），每页保留 1~3 个相关片段 | **W2 就实现，不做全文进上下文** |
| 编排 | 自研 asyncio 状态机（无框架） | 核心竞争力，不解释 |
| 状态 | SQLite + WAL（第一天就开）+ 单写入队列 | asyncio 并发写入会 locked |
| trace | 自建 JSONL（任务id/步骤/模型/token/耗时/url 元数据） | 正文不进 trace；20 行汇总脚本 |
| 可观测 | Langfuse 可选（buffer，一天适配） | |
| UI | **Gradio + 流式过程展示** | W4 出 demo；React 属于开源后自选 |
| MCP | 工具导出为 MCP server（buffer，半天） | 能挂 Claude Desktop 演示即可 |
| API | FastAPI + SSE | 后端味来源之一 |
| 测试/CI | pytest + GitHub Actions，**录制回放 fixture，不调真 LLM** | CI 免费且稳定 |

**已删除**（相对 v1）：React 前端、LangGraph 对比实验、Langfuse 主线依赖、100 题评测集、kappa≥0.6 硬 DoD。

---

## 3. 系统架构

```
            Gradio UI（流式展示研究过程 + 报告）
                        │
            FastAPI + SSE（后台长任务，可恢复）
                        │
        Orchestrator（纯代码状态机，无 LLM 参与）
        plan → research(并行) → verify → write
                        │
     Planner · Researcher×N · Verifier · Writer
                        │
        Agent Runtime（自研：loop/重试/超时/校验/并行）
                        │
     工具层（进程内直连：web_search / fetch_page+片段筛选）
                        │
     SQLite(WAL) 状态 · JSONL trace · 三级缓存 · 原文落盘
```

### 3.1 数据流

1. **Planner**：问题 → **无依赖的平行子问题 3~6 个**（`[{id, question, search_hints}]`）。~~depends_on~~ 已删；DAG 分层并发为 stretch
2. **Researcher ×N 并行**（信号量限流）：`search → 选 top-k → fetch_page(切段+BM25 选 1~3 片段) → 提取论断`；输出 `{sub_query_id, claims: [{text, quote, url, as_of_date}]}`；**原文落盘不进上下文不进 trace**
3. **Verifier**：逐条判 supported / partial / unsupported（对照 quote），跨源矛盾检测
4. **Writer**：supported 直接引用 `[n]`；**partial 保留但显式标注不确定性**（防 Verifier 误杀把报告写空）；文末 References
5. 失败降级：子任务失败标记缺失不阻塞；任务级状态落库可恢复

### 3.2 关键设计决策（面试必问）

| 决策 | 理由 |
|---|---|
| 编排纯代码，LLM 只在节点内 | 可控、可测、可复现；"怎么保证 agent 可靠性"的直接答案 |
| 传 quote 原文而非摘要 | 事实链可追溯，忠实度可机器评估 |
| 每页只留相关片段 | 成本/延迟/上下文三重控制，且 W2 就定死避免返工 |
| partial 保留 + 标注 | "Verifier 误杀了怎么办"的答案：不写空报告，降级呈现 |
| judge 跨厂商 | "LLM-as-judge 可信吗"的答案：排除同源同错 |
| 工具进程内直连，MCP 仅导出 | 主链路不为一层 IPC 牺牲调试性；MCP 是互操作导出层 |

---

## 4. 目录结构

```
deep-research-agent/
├── src/agent/
│   ├── runtime/        # agent loop、重试、并行、上下文管理
│   ├── orchestrator/   # 状态机、任务恢复
│   ├── agents/         # planner / researcher / verifier / writer
│   ├── tools/          # search / fetch+片段筛选 / mcp_export.py(buffer)
│   ├── storage/        # SQLite(WAL) DAO + JSONL trace
│   └── schemas.py      # ★ W0 定死：claim / findings / eval record
├── src/api/            # FastAPI + SSE + 长任务管理
├── app/gradio_ui.py
├── eval/
│   ├── dataset/        # 50 题 yaml（含 as_of_date / key_points）
│   ├── judge/          # 跨厂商 judge prompt 与评分器
│   ├── runner.py       # 一键批跑 + 漏斗指标
│   └── reports/
├── tests/              # mock 单测 + 录制回放 fixture
├── examples/           # 示例报告 2~3 篇
├── docs/               # 架构图、博客底稿
└── PLAN.md
```

---

## 5. 评测方案

### 5.1 评测集（50 题，每题带 `as_of_date`）

事实核查 10 ｜ 对比分析 12 ｜ 时间线 8 ｜ 多维度调研 13 ｜ 争议/多观点 7。
字段：`question / key_points(5~8) / type / difficulty / as_of_date`。强模型生成候选 → 人工逐题校对。避免高时效题（防腐烂），或明确记录快照。

### 5.2 指标（防刷分设计）

**成对主指标**：引用忠实度 × 要点覆盖率，同时报告 **F1**；消融表**必须**列出每组 claim 数 / 报告字数 / 覆盖率。只报"忠实度 +10pp"不成立——面试官一句"是不是报告变薄了"就穿。

**漏斗指标**（失败归因的原料，W5 runner 就要分层记录）：
检索命中率（key_points 在检索阶段是否被覆盖）→ claims 抽取数 → verifier 通过率 → writer 引用率。W7 优化凭这四个数字定位短板，不凭感觉改 prompt。

### 5.3 人工抽检（30~50 条 claim）

- claim 级三分类（supported/partial/unsupported），**故意富集非 supported 样本**（否则抽不到）
- 报**观测一致率 + 混淆矩阵**；kappa 顺手算，若偏低在 README 注明"类别不平衡时 kappa 系统性低估，故以富集抽样一致率为主"
- 这套说法本身就是面试弹药，不需要 AC1/PABAK 全家桶

### 5.4 消融矩阵（各跑 50 题）

| 配置 | 说明 |
|---|---|
| A0 | 单 Agent 一次生成（= W1 CLI 模式加开关，近乎免费） |
| A1 | 多 Agent，无 Verifier |
| A2 | 完整系统 |

---

## 6. 排期

### W0（10/25–10/26，本周末）：schema 定稿，不写业务代码

- [ ] 定死三套 schema：`claim` / `findings` / `eval record`（含漏斗四层字段）
- [ ] 定死指标口径（成对报告规则）
- [ ] **问导师：毕设能否与本项目同方向**（若能，时间杠杆翻倍，答辩材料与面试材料共用）
- [ ] 注册账号：LLM 厂商、Tavily、GitHub

### 核心六周（W1–W6）

| 周 | 日期 | 目标 | DoD |
|---|---|---|---|
| W1 | 10/27–11/2 | 原生 tool-calling loop + web_search | CLI 出带来源答案；loop <200 行；A0 基线模式埋好开关 |
| W2 | 11/3–11/9 | loop 加固 + fetch_page **片段筛选** | 注入工具失败可自恢复；每页只留 1~3 片段；SQLite 开 WAL |
| W3 | 11/10–11/16 | Planner + Researcher 并行 + 落库 + JSONL trace | 一题自动拆 ≥3 子问题并行；状态全程可查 |
| W4 | 11/17–11/23 | Verifier + Writer + **Gradio demo** | 端到端出带引用报告；**录 2 分钟演示视频**；博客 1 初稿《我为什么不用 LangGraph，自己写了 200 行 Agent Loop》 |
| W5 | 11/24–11/30 | 50 题 + runner + 漏斗指标 | `python -m eval.runner` 一条命令出报告 |
| W6 | 12/1–12/7 | 消融 A0/A1/A2 + 人工抽检 | 对比表（含覆盖率与 claim 数）；**博客 2 发布**《引用忠实度怎么机器评估》 |

**W4 末手里就有可演示的东西**——被毕设挤占、面试提前来，都不慌。

### Buffer 六周（12/8–1/17），按优先级从上往下做，随时可停

1. 三级缓存 + 模型分级 → 出降本数字
2. **后端味补丁（约 2 天）**：任务可恢复（状态机 + 断线重连 + 幂等重试）；locust 压 SSE 接口出 "50 并发 P95 = X 秒"
3. README 打磨（mock 模式、架构图、结果表）+ CI（录制回放）+ 示例报告
4. MCP 导出封装（半天，挂 Claude Desktop 演示）
5. Langfuse 适配（可选一天）
6. 面试话术整理 + 简历 bullet 定稿
7. Stretch（可全砍）：评测集扩 100 / DAG 分层并发 / Reflector / 本地 RAG

### Kill criteria（到点执行，不犹豫）

| 时间点 | 条件 | 动作 |
|---|---|---|
| 11/23（W4 末） | 端到端还没跑通 | 砍 Verifier 复杂规则，先保证能出报告 |
| 12/14 | 手里还没有一组对比数字 | 砍掉 buffer 里所有非评测项，全部时间给评测 |
| 1/11 | README 还没写完 | 直接交，博客后补 |

### 测试与审计分工（W1 复审后确立）

同源盲区：代码与测试出自同一作者时，盲区一致地漏（"judge 跨厂商"原则的镜像）。
对策是两层防御，互补而非替代：

| 层 | 谁做 | 范围 | 时机 |
|---|---|---|---|
| 行为层 | 实现方（随代码交付） | 行为/错误路径/不变量测试（如存档往返）；修 bug 必附回归测试 | 每次交付，全绿为准出 |
| 对抗层 | 外部会话/顾问 | 对里程碑 diff 做对抗审查：① 测试同源漏了什么 ② 每个指标怎么作弊 | W2/W4/W6 交付后 |

对抗层发现的问题 → 实现方修复 + 补回归测试；审查探针清单存入 `docs/audit/`（这套流程本身就是面试素材）。

---

## 7. 曝光与求职策略（普通本科的关键补丁）

- **博客 W4 就开始**，不等到收尾：《我为什么不用 LangGraph，自己写了 200 行 Agent Loop》《引用忠实度怎么机器评估：Verifier 的设计与代价》——带数字的实践文是中文社区稀缺品，也是面试逐字稿
- GitHub：小步提交（conventional commits），绿墙本身是简历；README 让陌生人 10 分钟跑起来（mock 模式）
- 投递三层池：AI 创业公司/垂直行业 AI（主战场）+ 大厂（彩票）+ 寒假实习（11–12 月就投，bar 低于正式批）
- 每周 3~4h 刷题八股**不可挪用**——项目是敲门砖，笔试是门槛

---

## 8. 风险与预案

| 风险 | 预案 |
|---|---|
| **时间被毕设/面试挤占（最大风险）** | 6 周核心 + buffer 结构 + kill criteria；W4 末必有可演示物 |
| Tavily 配额（50 题×3 组×4 次 ≈ 600/轮） | 搜索全量缓存（跨迭代复用）；配额过半即切 SearXNG；或第二个 key |
| judge 不稳 | 跨厂商 + temperature=0 + rubric 迭代；抽检一致性就是校准证据 |
| 简历写大讲不透 | 简历最多 4 条 bullet，每条守住 5 分钟追问；砍模块优先于加模块 |
| 范围蔓延 | 本文件即基线；新想法一律进 buffer 第 7 项 |

---

## 9. 简历产出与面试映射

> - 独立实现多智能体深度研究系统：Planner-Researcher-Verifier-Writer 架构，原生 tool-calling 循环（重试/超时/并行/错误恢复），子任务 asyncio 并行，网页按 query 做切段筛选控制上下文
> - 自建 50 题评测集，跨厂商 LLM-as-judge + claim 级人工抽检（一致率 X%，富集抽样设计），忠实度×覆盖率成对评估 + 四层漏斗归因
> - 消融实验验证 Verifier 价值（忠实度 X%→Y%，覆盖率变化 Z%）；缓存+模型分级降本 W%（有 trace 数据）
> - （后端岗）FastAPI+SSE 流式长任务：状态机 + 断线重连 + 幂等；locust 压测 50 并发 P95 = X 秒

| 模块 | 挡住的面试题 |
|---|---|
| 自研 loop | LangChain 内部机制 / 工具调用失败怎么办 |
| 纯代码编排 | 怎么保证可靠性与可复现性 |
| Verifier + partial 保留 | 怎么控制幻觉 / 误杀怎么办 |
| 成对指标 + 富集抽检 | 怎么评测 / judge 可信吗 / 怎么排除"写得少刷分" |
| 片段筛选 + 缓存 + 分级 | 成本控制 / 上下文管理 |
| 可恢复长任务 + 压测 | （后端）幂等 / 状态机 / 高并发 |
