# Schema 对抗审计台账（外部审查 × 实现方复核）

> 流程：外部会话对 `src/agent/schemas.py` 做探针式对抗审查 → 实现方逐条对当前代码复验 → 修复/裁决。
> 原则：绿勾是必要非充分证据；"洞已关"的唯一凭证是对工件本身的红→绿探针。
> 本文件是开放项的唯一登记处——不靠记忆追踪。

## 第一轮（结构性：join 完整性 / 防作弊校验缺失）

| # | 探针 | 状态 | 修复落点 |
|---|---|---|---|
| 1 | citation_map 重复编号校验是死代码（条件写反） | ✅ 已关 | `_check_citation_map` + 回归测试 |
| 2 | claim_id 跨 finding 重复被 set() 静默坍缩 | ✅ 已关 | `_consistency` 展平查重 |
| 3 | 同一 claim 多条 judgment（忠实度分母重复计数） | ✅ 已关 | `_consistency` |
| 4 | ghost judgment / ghost evidence（指向不存在的 claim） | ✅ 已关 | `_consistency` |
| 5 | word_count 由 Writer 自报（"写得少"防线可自证通过） | ✅ 已关 | 改 `@property` 现算，不入存档 |
| 6 | computed_field + extra="forbid" 存档读不回 | ✅ 已关 | 派生指标全部降级普通 property + 往返测试 |

## 第二轮（度量可信性：数字能不能信）

| # | 探针 | 状态 | 说明 |
|---|---|---|---|
| ② | evidence 可指向未被报告引用的 claim → 覆盖率与忠实度证据集不一致 | ⏳ **开放，移交 W5** | 是否要求 evidence ⊆ cited 取决于 runner 在什么时机、由谁产出 hits；现在定就是拍脑袋。见 `KeyPointHit` docstring |
| ③ | hits 只按长度对齐，文本错位不报错 → 覆盖率对、归因全错 | ✅ 已关 | `_consistency` 文本多重集比对 |
| ④ | 报告可引用 verifier 判 unsupported 的 claim → 漏斗与忠实度矛盾处理同一条引用 | ✅ 已关 | `_consistency` 拦截（依据 §3.2 既有设计规则，非新口径） |
| ⑧ | 正文 3 个 [n]、citation_map 只登记 1 条 → 忠实度分母被做小 | ✅ **以约束关闭（非代码）** | citation_map 是唯一真源，编号由程序生成；不解析正文 [n]（正则误伤代码块/链接）。落点：`ReportArtifact.citation_map` 描述 + PLAN W4 DoD（writer 程序化生成映射） |
| 补1 | 忠实度静默丢弃未判定引用（2 判 1 → 报 1.0） | ✅ 已关 | `quality_flags` 增加 `judge_gap:N` 显式报数（ClaimJudgment 文档承诺的实现） |
| 补2 | AgreementStats 层计数分母不自洽（n=1 而随机1+富集2） | ✅ 已关 | 层计数只统计参与比对的样本 |

## 教训记录

- 第一轮盲区：测试照代码结构写，想不到"怎么作弊这个指标"——威胁模型驱动的用例才能抓到。
- 第二轮盲区（双方同病）：**拿间接信号代替直接验证**。实现方用"测试全绿"推断"洞全修"，审查方用"文件 mtime 没变"推断"没动"。"洞已关"必须红→绿探针对工件本身。
- 假对象层测不到 SDK 接触面：auto/extra_body 的 TypeError 只有真实 SDK + MockTransport 线体测试能抓（`TestWireBodyAssembly`）。

## 第三轮（W2 fetch_page：契约 + 实现，2026-10-26）

| # | 探针 | 级别 | 状态 | 落点 |
|---|---|---|---|---|
| 换行翻译 | Windows `write_text` 把 `\n` 翻成 `\r\n`：盘上归档 ≠ 偏移锚，locator 对任何非默认读取方式全部错位 | **P0** | ✅ | `newline=""` + 临时文件原子写 + 接口级断言 `len(open(newline='') ) == text_chars`（用最小特权读取，不再依赖读写对称） |
| 同秒撞号 | page_id 秒级时间戳，同秒重抓同 URL → 同 id → 归档被覆盖，旧 snippet 静默指向新正文 | **P0** | ✅ | page_id 追加 uuid4[:6]；同秒双抓双 id 双归档测试；修复前旧归档已全部删除（偏移不可信） |
| PDF/二进制 | content-type 未过滤，乱码进上下文污染 claim | P1 | ✅ | content-type 白名单；非白名单走合法空结果（与 JS-only 同路），不进 TOOL_ERROR 以保护 streak 语义 |
| snippet_id 失约 | 契约说"researcher 引用 snippet_id"，但 to_model_text 没输出 id | P1 | ✅ | to_model_text 携带完整 id（选"发 id"而非"按序号反查"：模型原样回传 id 比"第 i 个片段"的位置解析更稳，同 tool_call id 机制） |
| 连接池风暴 | 每次抓取新建 AsyncClient | P2 | ✅ | 进程内复用 + `aclose()`；429 按 Retry-After 退避重试一次；schemas 公开 `Contract`（不再跨模块引私有名） |
| 其余 P2 | Content-Length 流式预检 / 异步写盘 / robots | P2 | ⏳ 暂缓 | 2MB 上限内收益小；robots 以 README 合规说明代替强制拦截 |

第三种同源盲区（前两种：同作者测试、假对象层）：**读写同一套归一化**——
写盘用 `write_text`（翻译换行），测试读回用 `read_text`（反向翻译），两边
对称所以永远绿，盘上的真实字节从没被看过。对策：接口级断言必须用
"最小特权读取"（`open(newline='')`），永远不与写入方共享便利性转换。

两条实现 bug 的共性：**不报错，只让数字 silently 变假**——测试断言
"结果对不对"抓不到它们，只有"锚定不变量"（盘上字节 == 偏移锚）抓得到。

## 第四轮（claims 解析层，2026-10-26）

| # | 探针 | 级别 | 状态 | 落点 |
|---|---|---|---|---|
| claim_id 无 run_id | 两次 run / 消融三组共用 `SQ1-c001` → SQLite 主键覆盖、跨配置污染 | **P1** | ✅ | claim_id 采用 schemas 建议格式 `{run_id}:{sub_query_id}:{序号}`；双 run 唯一性测试 |
| 内存索引失联 | build_claims 长在 `fetcher.pages` 内存字典上：进程重启后归档在盘、索引全空，"任务可恢复"破功 | **P1** | ✅ | 抽出 `PageResolver` 协议（`get_snippet`），PageFetcher 内存实现满足协议，W3 的 SQLite 实现直接替换；重启模拟测试 |
| quote 未归一回原文 | 空白容忍路径下 Claim.quote 是模型的归一化形态 ≠ locator 切片，违反"逐字复制"文档 | P2 | ✅ | 定位成功后用片段切片回填 quote——quote ≡ locator 切片无条件成立（顺带修复：上轮测试断言选"切原文"恰好绕开了承诺未闭合） |
| 歧义引文静默取第一处 | 通用短句多处出现时锚到第一处，人工回查"原句在、语境错" | P2 | ✅ | `locate_quote` 三态返回（span / "ambiguous" / None），歧义→`ambiguous_quote` 失败 + 回喂"引用更长上下文" |
| as_of_date 时区 | UTC `fetched_at.date()` 比本地日历日早一天（UTC+8 的 00:00–08:00 时段） | P3 | ✅ | `.astimezone().date()` |

第四种同源盲区：**单次执行偏差**——测试照着"一次成功的执行"写，而这四条
bug 全在"第二次执行"里（第二次 run、重启后的进程、第二次出现、换一种
归一化）。对策：新增用例必须覆盖对应维度（双 run / 空索引 / 重复引文 /
空白折叠），而不是同一形状再测一遍。

## 第五轮（W3 编排层，2026-10-26）

| # | 探针 | 级别 | 状态 | 落点 |
|---|---|---|---|---|
| run 状态漏 FAILED | `total_failures_degraded` 只数 DEGRADED："4 挂 1"落库为 ok，成功率虚高 | **P1** | ✅ | 提取 `_aggregate_status()`：任一子任务非 OK → 至少 DEGRADED；单测锁定 |
| 崩溃残留 ok | save_run 即写 ok，进程被杀后该行永远"成功" | **P1** | ✅ | 初始状态 'running'（DB 层字符串）；异常路径 finish_run(FAILED)；被杀行停在 running——聚合必须 `finished_at IS NOT NULL` |
| config 硬编码 | 落库永远 "A1"，消融三组会全归 A1 且不报错 | **P1** | ✅ | run_research(config=) 参数化 + CLI 透传 + 非法值拒绝 |
| token 用量断链 | LoopResult.usage 无人传递，成本指标数据源为空 | P2 | ✅ | Finding 增 tokens_in/out；researcher 跨修复轮累计 → trace sub_done → DB（含老库 ALTER 迁移） |
| LLM client 泄漏 | 每 worker 一个 AsyncOpenAI 连接池永不关闭 | P2 | ✅ | LLMClient.aclose()（注入方管注入的）；编排层登记统一关闭 |
| search_hints 死字段 | 生成→存储→无人使用 | P2 | ✅ | 拼进 researcher 首轮消息 |
| 修复轮用量漏计 | 只统计胜出轮，检索重做被低估 | P3 | ✅ | 用量与工具统计跨全部轮次合并 |

第五种同源盲区：**不成功的执行**——三个 P1 全在失败路径（子任务失败、
进程崩溃、非默认配置），而所有测试都在造成功样本。对策：失败矩阵
（1 挂 / 全挂 / 中途异常 / 被杀后重开 / 非法配置）各一条用例，
`TestFailureMatrix` 固化。

## 自查记录（W4，2026-10-26，实现方自捕）

**迁移列序串位**：为老库补 tokens 列的 `ALTER TABLE` 把新列追加在表尾，
而 `INSERT ... VALUES(?,...)` 按建表语句的位置写入——老库上物理列序
≠ 建表列序，数值静默写进别的列（tokens_out 位置存了 elapsed）。
发现方式：真实 run 后拿 `SUM(tokens)` 与 trace 的 token 总数**交叉核对**，
两边不一致才暴露——单看任何一边都是"对的"。
修复：全部 INSERT 显式列名；新增回归测试（手工构造老表结构 → 迁移 →
按列名逐项断言）。教训：**位置参数 + 迁移过的表 = 静默串位**；
每个"数字类"落库字段，都该有一条"与独立来源交叉核对"的验证习惯。
