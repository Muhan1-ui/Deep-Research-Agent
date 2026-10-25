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
