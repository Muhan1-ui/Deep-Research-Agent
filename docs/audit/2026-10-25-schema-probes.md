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

## 第十轮（首次真实跨模型冒烟，2026-10-30）

配置：judge=deepseek-v4-pro（同厂商换模型，避开同模型自偏好；自检如实报同厂商）。

| # | 发现 | 级别 | 状态 | 落点 |
|---|---|---|---|---|
| 成功率 0% 与有报告 n=1 并列、degraded 题贴 no_report | succeeded 要求全绿，报告明明在——W6 全表成功率会长期 20~50% 且第一眼被打回 | **P1** | ✅ | 拆两列：有报告率/全绿率；flag 语义三分：no_report（真没报告）/ degraded（有报告子任务不全）/ run_failed |
| GBK 控制台打 ¥ → UnicodeEncodeError → exit 1 | "一条命令出报告"的最后一厘米：报告落盘成功但脚本层失败 | **P0** | ✅ | `_safe_print`：UnicodeEncodeError → reconfigure(errors='replace') 重打（fake stdout 测试锁住） |
| git status 遗留 eval/reports/ | 每跑一次留一个未跟踪文件 | P3 | ✅ | .gitignore 收整个目录 |
| v4-pro 价目未登记 | judge 成本计 0 | P3 | ⏳ | 价目表留注释占位——按账单核对后填，不猜价 |
| 132s/题 × 50×3 ≈ 5.5h | W6 全矩阵耗时 | P3 | ✅ | run_batch 加 `--jobs`（题间并发，默认 1 不影响既有测试） |

三组真实冒烟结果（e001，全部 degraded 如实记录）：A0 忠实度 0.667/覆盖 0.333、
A1 0.897/1.0、A2 0.857/0.667——**1 题样本无统计意义，但漏斗已经在讲真话**：
A2 的 Verifier 通过率 100%（33/33）而 writer 只引 64%（21/33）——第一轮消融
信号显示瓶颈可能在 writer 的引用取舍而非 verifier。judge 零解析失败，
v4-pro 输出格式干净。

## 第十一轮（W6 前置：数据集外核 + 复现三元组收口，2026-10-30）

| # | 项 | 状态 | 说明 |
|---|---|---|---|
| e017 AI Act 时间线 | **外核抓到硬伤**：Digital Omnibus（Regulation (EU) 2026/1744，2026-05 临时协议）将附件 III 高风险推迟至 2027-12-02、附件 I 至 2028-08-02——原要点（2026-08/2027-08）在 as_of 2026-11 下已错，coverage 判分会全污染 | ✅ 修复 | 要点改为"主线 + 2026 年修订推迟"六条，notes 明确只答原时间线记 0 |
| e015 TR v. Ross | 原要点正确，且 2025-10 第三巡回法院已**维持原判**（首个 AI 训练 fair use 联邦上诉判例）——补进要点（上诉维持比地区判决更耐久） | ✅ 增强 | |
| e019 Redis AGPL | 外核通过：Redis 8（2025-05-01）AGPLv3 三许可、Valkey 分叉、Elastic 2024 加 AGPLv3 全部属实 | ✅ | |
| resp.model 钉版本 | deepseek-flash 是别名会漂移；`_UsageTrap` 按角色捕获 resp.model 真身 → `models_seen` → `model_snapshots`/`judge_model` 真身优先、settings 兜底 | ✅ | 复现三元组最后一格钉死；测试断言全角色真身 |
| judge 同厂商同模型 | 自检如实报两条（同厂商+同模型）；自偏好偏差增强 → **W6 人工抽检从可选变必须** | 接受 | 用户取舍：省钱；校准兜底 |

教训：**出题的人核不了自己的时效性要点**——e017 的作者（用户）与外核方
（搜索）分离才抓到 Digital Omnibus。评测集的 as_of_date 是防腐烂锚点，
但"锚点时刻的事实核查"仍需要独立来源。

## 第十二轮（W6 全矩阵 + 漏斗桶修复，2026-10-30，发现方=用户/真实数据）

| # | 项 | 状态 | 说明 |
|---|---|---|---|
| 漏斗四桶恒等式对 A0/A1 破裂 | 43 条 claim 四桶全 0（0+0+0+0≠43）：无核验记录的 claim 一条不进桶，通过率分母却是抽取数——全部被记成"没通过" | ✅ db810e6 | 无核验 claim 计入未核验；通过率分母=核验条数，无核验阶段返回 None→n/a；恒等式从此对**所有配置**成立 |
| A0 格式能力混入研究能力 | 单 agent 跑满 10 轮工具后不按 JSON 收尾 → 记 0 论断 FAILED | ✅ 4cc243a | 一次"只补格式、不重新检索"的重试——公平基线（token 总账同步更新） |
| report-only 混表 | sample.yaml 与 W6 记录混进同一张汇总 | ✅ db810e6 | --dataset-version / --latest-only；报告首行多 sha 并列 |
| judge 校准诚实边界 | 未做真人盲标（时间）；30 条 ×3 重测 temp=0 全一致（可复现，分歧是口径非抖动）；agent 演练 90% 仅作工具链验证 | 接受 | 盲标表随仓库提交，日后可补；judge 已换 glm-5.3-flash（跨厂商，第十一轮的同厂商担忧解除） |

W6 全矩阵（10 题，dataset v4d1c7846）：A0→A1→A2 忠实度 0.504→0.578→0.705、
覆盖率 0.781→0.796→0.903、报告率 70%→100%→100%、成本 ¥0.73→3.09→4.13。
读法（已写进 docs/w6-report.md）：多 agent 协作主要救"有没有报告"和"检索覆盖"
（检索 57%→85%），核验环节才真正抬忠实度（+12.7pp，成本 +34%）。
已登记的解读限制：①各配置忠实度分母不可比（43 vs 383 条 claim，A0 写得少且保守，
趋势不可读成"更爱胡说"）；②A0/A1 引用率与 A2 不是同一把尺（可引用性默认放宽）；
③A2 全绿率 0% 的 degraded 全部来自 quote 锚定剔除——解析层在正确拒绝不可锚定引文，
是特性不是故障。

第十二轮教训：**不变量要声明为全称**。"四桶之和==抽取数"的测试早在第七轮就有，
但只对 A2 形状的 fixture 断言；A0/A1 的"设计好的残缺"在 mock 里合法，
在真实跑批里是看得见的矛盾。修法不是加一条测试，而是把恒等式升级为
所有配置下的结构不变量——单测里的残缺样本也必须满足它。

## 第十三轮（W6 报告复核 + 缺失数据偏差的结构性防线，2026-10-30）

| # | 项 | 状态 | 说明 |
|---|---|---|---|
| 缺失数据偏差 | 用户发现并写进报告 §4.2/威胁效度 #2：A0 失败的 e015/e019 恰是 A1 忠实度最差两题（0.45/0.30），不进 A0 均值却拉低 A1——自有样本均值让 A0 vs A1 反转 5 分；共同 8 题打平（0.6285 vs 0.6287） | ✅ 用户发现 | 复核方从 eval_records 独立重算，数字逐位吻合（直接验证工件，不读报告） |
| 共同题集进代码 | 报告脚注防不住 50 题正式跑批重蹈——口径必须长在产物里 | ✅ 本轮 | render_report 渲染"共同题集"表（≥2 配置时自动出现），分母差异（主表 n vs 共同 n）可见；测试锁死 |
| A0 三次运行方差 | 报告率 70%/90%/80%、覆盖 0.70–0.81——单 agent 方差显著大于多 agent（报告按 ±10% 区间读） | 已入 §3.3 | 本身就是消融发现：分解并行同时买了稳定性 |
| A0 剩余失败=句柄幻觉 | unknown_snippet（编造不存在的片段 id）——"记不住看过什么"而非"研究不出来"；落锚失败 46%→20%→12%（格式重试后） | 已入 §3.4 | 失败理由枚举（ResolveFailure）第一次在真实数据上完成归因 |

结论修正（比原结论更值钱）：分解+并行买的是**覆盖率与稳定性**（共同 8 题
+9.2pp / 报告率 70%→100%），核验层买的才是**忠实度**（共同 8 题 +7.6pp，
成本 +34%）。跨配置均值必须用共同题集或失败记 0——现已是渲染层的
结构保证，不再依赖作者自觉。

复核方法注记：本轮所有结论均以"对 eval_records 独立重算"验证，
与报告数字逐位一致——间接信号（报告写得对）不作为证据。

## 第十四轮（共同题集表的边界，2026-10-30，发现方=外部审计复算 a9ffc89）

| # | 探针 | 状态 | 落点 |
|---|---|---|---|
| P1 缺失数据偏差下沉一层 | 共同题集按"有报告"划集，但零引用报告的忠实度是 None——`_mean` 静默跳过，n=2 的均值实际只算 1 题 | ✅ | `_metric_cell`：均值被跳过时附 `(n=k)` 有效样本数，缺口可见 |
| P2 重跑按次数加权 | 不带 --latest-only 时同题多跑被加权（e019 权重稀释到 1/24）——结构防线不该依赖调用方记得加参数 | ✅ | `_dedupe`：渲染层内部按 (config, question_id) 保留 run_id 最新一条 |
| P3 空集静默消失 | 某配置全挂时共同题集表整个不渲染，读者无感知 | ✅ | 空集输出说明行："跨配置均值不可比" |
| P4 测试强度 | 原测试只数行数不断 n 值 | ✅ | n 值断言 + P1/P2/P3 各一条用例 |

衍生语义碰撞（修复中抓到）：同题不同 judge 档会被去重——混用告警的语义
据此澄清为"跨题混档"（那才是真实风险），测试改为跨题构造。

第十四轮教训：**偏差会顺着修复的边界下沉**。堵住跨配置后它出现在跨题
（P1），堵住口径后它出现在权重（P2）——每修一层都要问"这一层的
None/重复/空集去了哪里"，而不是宣布问题关闭。共同题集的下一层边界
（judge 侧的 None、key_point 缺失）暂无证据，50 题跑批后复核。
