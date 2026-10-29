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

## 第六轮（W4 Verifier/Writer，2026-10-26）

| # | 探针 | 级别 | 状态 | 落点 |
|---|---|---|---|---|
| 报告无来源列表 | 37 条引用在正文只是 [1]..[37]，读者点不开——W4 DoD 只完成一半 | **P1** | ✅ | build_report 末尾追加"## 来源"（按 URL 分组、编号聚合），sections 同步 |
| 未核验伪装成 partial | fallback 造的 partial 与真 partial 在 writer 输入表逐字相同：报告把"不知道"标成"知道一点"，pass_rate/忠实度口径被污染 | **P1** | ✅ | Verification.unverified=True 独立字段；输入表渲染"未核验"；统计层把它当数据缺口而非 partial |
| Verifier 部分响应不重试 | 模型截断只回 1/6 条 → 其余 5 条无声降级，最坏 11/12 | P2 | ✅ | `len(got)==len(expected)` 才 break；重试后仍缺才 fallback |
| cites∪正文 允许"声明未展示" | 声明了但正文没写的编号进忠实度分母 | P2 | ✅ | **正文 [[n]] token 成为唯一成员资格**（显示层事实）；cites 退出协议仅兼容保留 |
| trace token 双计 | sub_done 与 run_done 同名字段，jq 全量求和翻倍 | P3 | ✅ | run_done 改 `total_tokens_*`；README 注明按 event 过滤 |
| degraded 样本口径 / 正则误伤提示 | 0.3~0.5 元含修复轮成本；choices[0] 会被 \[(\d+)\] 误伤 | P3 | ✅ | README 样例注记；citation_map 描述写死"统计引用只从本表读" |

第六轮盲区形态：**交付物视角**——前五轮都在看"数据对不对"，这轮两个 P1
都在"用户/读者看到什么"（报告可读性、标注语义）。对策：验收清单里
除了指标，还要有"以读者身份读一遍报告"。

## 第七轮（第六轮修复的复核，2026-10-26）

复核第六轮六项：四项闭环（来源清单、未核验渲染、部分响应重试、cites 退出），
两处"字段加了但派生层没跟上"——同一形态第三次（word_count 自报 → judge_gap → unverified）。

| # | 探针 | 级别 | 状态 | 落点 |
|---|---|---|---|---|
| 未核验仍计入 partial | `Verification.unverified` 已存在，但 `funnel()` 只读 `v.label`：未核验照样算进 `claims_partial` → `verifier_pass_rate` 把"不知道"记成"通过"。第六轮台账写的"统计层当数据缺口"当时并没有兑现；W5 若照此建指标，口径回不去 | **P1** | ✅ | 抽出 `count_labels()` 作为四桶计数的**唯一实现**——`funnel()` 与 trace 的 `verify_done` 都调它，"谁读它"与"谁写它"不可能再分叉；`describe()` 打印"｜未核验 N"；恒等式 `supported+partial+unsupported+unverified == 核验条数` 有用例 |
| word_count 被来源清单污染 | 来源段长度随引用数增长，而 word_count 正是用来证明"A2 忠实度高不是因为写得少"——被引用数反噬成混淆变量（真实报告里来源段约占 34%） | **P1** | ✅ | `REFERENCE_HEADING` 常量由 writer/schemas 共用；`word_count` 只计非来源 section（无 sections 的旧记录回退整段） |
| 单方括号 = 零引用 | cites 退出协议后 `[[n]]` 是唯一成员资格：模型写成 `[1]` 就零引用、run 直接降级 | P2 | ✅ | `_looks_like_bare_citations()`（有 `[n]` 且无 `[[`）→ 一次格式修正重试；重试仍失败则保留正文、refusals 如实声明"无可校验引用" |
| retry 文案仍要 cites | 协议已删，提示词还在教模型写一个被忽略的字段 | P3 | ✅ | `_RETRY_HINT` 改为"JSON 对象且含 sections 数组" |

第七轮盲区形态：**承诺与消费的错位**——不变量写在字段说明里，派生指标
（funnel / word_count）却没消费它。对策：新字段的"谁写它"必须和"谁读它"
一起交付；只有写入方没有读取方，就是新一次自欺。

~~仍登记的开放项：真实产物尚未用修复后代码重跑~~ —— 已关闭：run-20261026111859
即修复后代码的重跑，`含 ## 来源 = True`（复核段见下）。

**真实 run 复核（run-20261026111859-058208，A2）**：报告已带 `## 来源`（24 条引用、
按 URL 分组），W4 DoD 闭环。同一次 run 又暴露了两件事：
① trace 的 `verify_done` 仍按 `v.label` 数（21 partial 里混着未核验）——
已由 `count_labels()` 单一实现收口；
② 6 个子问题里 SQ4 **FAILED**（0 claims，却烧了 103k input tokens），
`_aggregate_status` 如实降级为 degraded——失败路径第一次在真实数据上被验证。
成本实测：566k in / 17k out ≈ ¥0.6（空闲时段）/ ¥1.3（高峰），
高于 README 的 0.3~0.5 元，需按"含修复轮与失败子任务"重写口径。

## 第八轮（W5 评测 runner，2026-10-29）

| # | 项 | 状态 | 落点 |
|---|---|---|---|
| 开放项 ② 关闭 | evidence ⊆ citation_map 成为 schema 硬校验 + judge 上游过滤——覆盖率与忠实度证据集合一 | ✅ | `EvalRecord._consistency` + `judge_key_points`（表外编号过滤，空则如实降级未覆盖） |
| token 总账失踪 | planner/verifier/writer 的用量从未计入（只有 researcher 分账） | ✅ | `_UsageTrap` 包装全部 LLM 调用，run 总账 = 全链路 |
| A1 无可比产物 | 消融要求三组都有报告；A1 此前不出报告 | ✅ | Writer 下放 A1/A2 共用（A1 无核验→全部可引用） |
| 枚举渲染泄漏 | 汇总表出现 `\| AblationConfig.A2 \|`——只有字符串断言能抓，人眼会当"小毛病"放过 | ✅ 自查 | 渲染统一 `.value`；教训：**对"给人看的产物"，断言要按读者的眼睛写** |

W5 交付：judge（temp=0 显式传入、跨厂商独立、批次部分响应重试、漏判=数据缺口）、
A0 基线（复用同一套测量机械：snippet 句柄→句级锚定→程序化 citation_map）、
build_record（复现三元组 + 成对指标 + 漏斗）、eval_records 落库、汇总表落盘。
真实批跑前置条件：.env 配置 JUDGE_API_KEY / JUDGE_MODEL（必须跨厂商）。

## 第九轮（W5 runner 审计，2026-10-30）

| # | 探针 | 级别 | 状态 | 落点 |
|---|---|---|---|---|
| token 总账漏 verifier/writer | 求和发生在 verifier/writer 调用之前——与上一条 commit message 的卖点直接矛盾，且无任何 token 断言所以 182 绿里隐形 | **P0** | ✅ | 求和移到 writer 之后；mocked_a2_factory 带 usage；`test_token_ledger_covers_all_roles` 断言 总账==各角色之和 且 ⊃ researcher 分账 |
| judge "evidence": null 打断整批 | `item.get("evidence", [])` 对键存在值为 null 返回 None → TypeError；run_batch 无单题容错 → 一题崩全批白跑 | **P0** | ✅ | evidence 白名单（只认 int/float 列表，null/字符串/布尔全拒）+ covered_* 只认真布尔（"false"→bool()==True 的假阳性堵死）+ run_batch 单题 try/except 记 FAILED 占位进分母 + `--report-only` 从库重渲染 |
| judge_vendor 回落 "unknown" 静默放行 | JUDGE_BASE_URL 留空时 judge 实际回落生产 base_url，同厂商跑批自检却报 OK | **P1** | ✅ | `judge_vendor=vendor_of(judge_base_url or llm_base_url)`——回落真名让自检必须报警（有回归测试） |
| 覆盖率解析失败与"真的没写"不可区分 | 保守回退记全 False，静默同化 | P1 | ✅ | `EvalRecord.judge_notes` + `cover_parse_failed` 进 quality_flags（与 judge_gap 同类的缺口显式报数） |
| 成本口径漏 strong/judge 两档 | planner/writer 的 strong 模型另一价；judge token 完全没进账；未登记价目计 0 与"免费"不可分 | P1 | ✅ | cost 分拆三档（researcher=生产价 / rest=strong 价 / judge 独立价）；`judge_tokens_in/out` 独立字段；报告注明未登记价目清单 |
| prompt_version 被 judge 的 j1 顶替 | W6 改生产 prompt 后复现三元组会撒谎 | P2 | ✅ | 拆 `PRODUCER_PROMPT_VERSION="p1"`；judge 版本随 `judge_model#j1` 标签传递 |
| 零引用时检索命中被一并归零 | 违反"漏斗①与写作解耦"的自家原则 | P2 | ✅ | 覆盖判定独立于引用判定照跑（citation_map 为空也能评检索命中，有单测） |
| 均值分母 / None 渲染 | 失败样本不进均值分母无标注；None 印成 "None" | P2 | ✅ | 新增"有报告n"列 + n/a 格式化 + 交叉读脚注 |

第九轮盲区形态（继续"承诺与消费的错位"）：**commit message 先于验证**——
"收全链路 token"写进了提交说明，实现只覆盖了半条链路，而测试套件里
没有一条 token 断言可以反驳它。对策：**卖点必须有名有姓的测试**，
写不进测试的卖点不写进 commit message。
