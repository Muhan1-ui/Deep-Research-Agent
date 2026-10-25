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
