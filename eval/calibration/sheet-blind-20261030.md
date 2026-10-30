# judge 校准盲标表（30 条）

- **盲标**：本表不含 judge 标签，也不含上一轮的标注；请只看 quote 判 claim。
- 判定口径：supported=完全支持；partial=只支持一部分；unsupported=不支持或矛盾。
- 每块把 `[ ]` 改成 `[x]`，只能勾一个；有分歧在 note 写一句。

## 1
- claim: 选型取决于现有基础设施而非理论性能基准，pgvector 是否够用要看实际约束位置。
- quote: The answer isn’t about which is technically superior, it’s about understanding where your constraints actually lie.
- url: https://zenvanriel.com/ai-engineer-blog/pgvector-vs-dedicated-vector-db
- 人工: [ ] supported  [ ] partial  [ ] unsupported
- note: 

## 2
- claim: 选择退出声明是保留或授予许可的声明，旨在以标准化、可互操作的方式表明某些权利（未）被授予用于特定AI相关用途，且适用于未知或未来的行为者；它是公共信号而非双边协议。
- quote: Opt-out declarations, as supported by TDM·AI, are statements of reservation or permission. They are meant to communicate, in a standardised and interoperable way, that certain rights are (not) granted for specific AI-related uses of content.
- url: https://docs.tdmai.org/opt-out-opt-in-and-content-licensing
- 人工: [ ] supported  [ ] partial  [ ] unsupported
- note: 

## 3
- claim: 模型覆盖：vLLM 的模型注册表收录了 400+ 个架构族，新增模型通常只需写 50-200 行 Python 类，这是它模型覆盖最广的主要原因。
- quote: Model support: 400+ architecture families registered in vLLM's model registry. Adding a new HuggingFace model is typically a 50-200 line Python class. This is the main reason vLLM has the broadest model coverage.
- url: https://inferenceengineering.tech/learn/vllm-vs-sglang-vs-tensorrt-llm
- 人工: [ ] supported  [ ] partial  [ ] unsupported
- note: 

## 4
- claim: 欧盟《人工智能法案》中通用人工智能（GPAI）模型提供者的义务自2025年8月2日起开始适用。
- quote: These obligations entered into application on 2 August 2025.
- url: https://digital-strategy.ec.europa.eu/en/policies/guidelines-gpai-providers
- 人工: [ ] supported  [ ] partial  [ ] unsupported
- note: 

## 5
- claim: 自2026年8月2日起，欧盟委员会的执法权开始适用，可就GPAI模型提供者义务的履行进行执法，包括处以罚款。
- quote: From 2 August 2026, the Commission’s enforcement powers enter into application. The Commission will enforce compliance with the obligations for providers of GPAI models, including with fines.
- url: https://digital-strategy.ec.europa.eu/en/policies/guidelines-gpai-providers
- 人工: [ ] supported  [ ] partial  [ ] unsupported
- note: 

## 6
- claim: Elastic 于 2021 年将代码转向非 OSI 认可的 SSPL 与 Elastic License v2，2024 年才新增 AGPL 选项，且不删除任何既有许可选项。
- quote: In 2021, we made the hard decision to move the Open Source portions of Elasticsearch and Kibana source code to non-OSI approved software licenses - SSPL and Elastic License v2, as a way to reduce the risk of market confusion.
- url: https://www.elastic.co/pricing/faq/licensing
- 人工: [ ] supported  [ ] partial  [ ] unsupported
- note: 

## 7
- claim: 法律上，训练数据许可市场已被原告主张为"存在或应当存在"，且法院认为新生许可市场的损害主张足以通过驳回动议（Kadrey）及简易判决阶段的救济门槛（Bartz），成为核心争点。
- quote: In Kadrey, plaintiffs argued that AI-generated outputs could substitute for the original works and that a licensing market for training data exists or should exist. The court found the market harm allegations sufficient to survive dismissal
- url: https://arxiv.org/html/2502.15858v2
- 人工: [ ] supported  [ ] partial  [ ] unsupported
- note: 

## 8
- claim: 该首批禁止规定针对八类AI系统，除少数例外，对公共和私营主体均为绝对禁令。
- quote: The AI Act’s initial prohibitions target eight categories of AI systems, identified below. Save for narrow exceptions, these prohibitions are categorical and apply to both public and private entities.
- url: https://www.quinnemanuel.com/the-firm/publications/initial-prohibitions-under-eu-ai-act-take-effect
- 人工: [ ] supported  [ ] partial  [ ] unsupported
- note: 

## 9
- claim: 这一点不同于授权许可：许可无论例外是否存在都适用；选择退出并不授予权利，而是通过撤销例外来保留权利。
- quote: This is different from licensing, which would apply regardless of the exception. Opt-outs do not grant rights; they reserve them by revoking the exception.
- url: https://docs.tdmai.org/opt-out-opt-in-and-content-licensing
- 人工: [ ] supported  [ ] partial  [ ] unsupported
- note: 

## 10
- claim: BM25 的关键改进之一是词频饱和：词频与相关性并非线性关系，单个词的贡献不会超过某个阈值，达到阈值后不再线性增长。
- quote: 但是BM25洞察到：词频和相关性之间的关系是非线性的，具体来说，每一个词对于文档相关性的分数不会超过一个特定的阈值，当词出现的次数达到一个阈值后，其影响不再线性增长，而这个阈值会跟文档本身有关。
- url: https://blog.csdn.net/laobai1015/article/details/120143102
- 人工: [ ] supported  [ ] partial  [ ] unsupported
- note: 

## 11
- claim: Kadrey法院承认"间接"市场替代为可诉损害，理由是LLM能以极短时间与创造力生成数百万竞争性作品，造成市场稀释，并会严重削弱人类创作的激励——这正是版权法旨在防止的损害。
- quote: Judge Chhabria in Kadrey is particularly illustrative when he endorsed ‘‘indirect’’ market substitution as legally cognizable harm, reasoning that LLMs are unique because they can generate millions of competing works with minimal time and creativity
- url: https://arxiv.org/html/2502.15858v2
- 人工: [ ] supported  [ ] partial  [ ] unsupported
- note: 

## 12
- claim: OLTP 数据库因 ACID 事务保障的需求，在对大型数据集执行分析型查询时通常会遇到性能瓶颈，这是选型的核心矛盾。
- quote: 由于这些要求，OLTP 数据库在对大型数据集执行分析型查询时，通常会遇到性能瓶颈。
- url: https://clickhouse.com/docs/zh/get-started/migrate/postgres/overview
- 人工: [ ] supported  [ ] partial  [ ] unsupported
- note: 

## 13
- claim: 成员国须指定国家主管机关（通知机关和市场监督机关）、向委员会通报并公开联系方式；且须确保国家层面至少设立一个AI监管沙盒并投入运行，期限为2027年8月2日（截止日经调整后为2027年9月2日）。
- quote: Member States: Member States shall ensure that their competent authorities have established at least one AI regulatory sandbox at national level. It should be operational by this date.
- url: https://artificialintelligenceact.eu/implementation-timeline
- 人工: [ ] supported  [ ] partial  [ ] unsupported
- note: 

## 14
- claim: 从源码构建时，应使用 --disable-gil 配置选项来构建 free-threaded Python 解释器。
- quote: the --disable-gil configure option
should be used to build a free-threaded Python interpreter
- url: https://docs.python.org/3/howto/free-threading-python.html
- 人工: [ ] supported  [ ] partial  [ ] unsupported
- note: 

## 15
- claim: RFC 9002 定义了 QUIC 的丢包检测与拥塞控制机制。
- quote: This document describes loss detection and congestion control mechanisms for QUIC.
- url: https://www.rfc-editor.org/rfc/rfc9002.html
- 人工: [ ] supported  [ ] partial  [ ] unsupported
- note: 

## 16
- claim: 随着 PEP 779 被接受，从 Python 3.14 起 free-threaded 解释器不再被视为实验性，但仍不是默认解释器构建。
- quote: the free-threaded interpreter is no longer considered experimental starting in Python 3.14, although it is not yet the default interpreter build
- url: https://py-free-threading.github.io
- 人工: [ ] supported  [ ] partial  [ ] unsupported
- note: 

## 17
- claim: 编译优化方面，TensorRT 编译器会遍历计算图，为每个算子挑选最佳内核，并识别可融合的算子组合，融合成单个内核以减少内存搬运和内核启动开销。
- quote: The TensorRT compiler can sweep through the graph to choose the best kernel for each operation and available GPU. Crucially, it can also identify patterns in the graph where multiple operations are good candidates for being fused into a single kernel. This reduces the required amount of memory movement and the overhead of launching multiple GPU kernels.
- url: https://developer.nvidia.com/blog/optimizing-inference-on-llms-with-tensorrt-llm-now-publicly-available
- 人工: [ ] supported  [ ] partial  [ ] unsupported
- note: 

## 18
- claim: 权利归属不清、权利人身份难以识别，构成“孤儿作品问题”；法定许可因无需逐一识别权利人而消除该问题。
- quote: It avoids the need to identify individual rightsholders, thereby eliminating the orphan works problem.
- url: https://legalblogs.wolterskluwer.com/copyright-blog/european-parliament-study-recommends-statutory-licensing-as-the-optimal-copyright-framework-for-ai-training
- 人工: [ ] supported  [ ] partial  [ ] unsupported
- note: 

## 19
- claim: SHA-256 通过新的仓库格式扩展实现：启用该扩展的仓库用 SHA-256 而非 SHA-1 命名对象，同时对象名与对象内对其他对象的所有引用都切换到新哈希函数。
- quote: We introduce a new repository format extension. Repositories with this extension enabled use SHA-256 instead of SHA-1 to name their objects. This affects both object names and object content — both the names of objects and all references to other objects within an object are switched to the new hash function.
- url: https://git-scm.com/docs/hash-function-transition
- 人工: [ ] supported  [ ] partial  [ ] unsupported
- note: 

## 20
- claim: 就法律确定性而言，新加坡和欧盟对AI企业最有利，允许较高确定性的训练；中国法律确定性也很高，但规则限制性强得多；日本和英国较为不确定；美国、加拿大、以色列等只有宽泛的合理使用/合理利用例外而无专门TDM例外，指引最少。
- quote: In China, legal certainty is very high as well but the rules are much more restrictive. In Japan and the UK, things are a bit more uncertain. Countries with broad exceptions for fair use/fair dealing, and with no specific TDM exceptions (United States, Canada, and Israel), currently provide the least guidance as to whether uses are permitted by law.
- url: https://link.springer.com/article/10.1007/s40319-025-01569-6
- 人工: [ ] supported  [ ] partial  [ ] unsupported
- note: 

## 21
- claim: pgvector 将文档与向量放在同一 Postgres 表、同一事务中，写入、更新、删除均保持原子性与一致性。
- quote: With pgvector, your documents and their embeddings live in the same Postgres table, in the same transaction.
- url: https://encore.dev/articles/pgvector-vs-pinecone
- 人工: [ ] supported  [ ] partial  [ ] unsupported
- note: 

## 22
- claim: pgvector 的优势是复用 PG 成熟的权限、备份、监控、主从、审计、ORM 生态，学习成本极低，中小规模下开发与运维成本最低。
- quote: - 复用 PG 成熟权限、备份、监控、主从、审计、ORM 生态，学习成本极低
- 支持向量检索 + 多表JOIN + 事务，适合订单、用户等业务附带相似度查询场景
- 中小规模（≤500 万向量）下开发、运维成本最低，无需新增中间件组件
- url: https://www.cnblogs.com/itqinls/p/20832126
- 人工: [ ] supported  [ ] partial  [ ] unsupported
- note: 

## 23
- claim: ClickHouse 的高吞吐批量写入模型使其不适合需要即时一致性的交易型负载，这也是 UPDATE 性能差异的关键原因：PostgreSQL 优先一致性，ClickHouse 优先批处理速度。
- quote: This model is optimized for high-throughput writes but makes it unsuitable for transactional workloads where immediate consistency is required. This is a key reason for the performance difference in UPDATE operations: PostgreSQL prioritizes consistency, while ClickHouse prioritizes batch processing speed.
- url: https://kestra.io/resources/data/postgres-vs-clickhouse
- 人工: [ ] supported  [ ] partial  [ ] unsupported
- note: 

## 24
- claim: Python 3.13 随 Free Threading 实现引入了延迟引用计数，3.14 又新增了 PyUnstable_Object_EnableDeferredRefcount(obj) 函数。
- quote: Python 3.13 added deferred reference counting with the implementation of Free Threading. Python 3.14 added PyUnstable_Object_EnableDeferredRefcount(obj) function:
- url: https://vstinner.github.io/free-threading-deferred-reference-counting.html
- 人工: [ ] supported  [ ] partial  [ ] unsupported
- note: 

## 25
- claim: 第113条第三段(c)项已被第(EU) 2026/1744号条例取代：第三章第1、2、3节（第6(5)条除外）按新日期适用，即第6(2)条与附件三为2027年12月2日。
- quote: Regulation (EU) 2026/1744 replaces Article 113, third paragraph, point (c). Chapter III, Sections 1, 2 and 3, with the exception of Article 6(5), now apply from:
| High-risk route | New application date | 
|---|---|
| Article 6(2) and Annex III | 2 December 2027 |
- url: https://www.pureglobal.com/news/eu-ai-act-omnibus-high-risk-dates-2028
- 人工: [ ] supported  [ ] partial  [ ] unsupported
- note: 

## 26
- claim: 可在 Git 3.0 之前通过设置 `git config --global init.defaultObjectFormat sha256` 或导出 `GIT_DEFAULT_HASH=sha256` 将 SHA-256 设为默认；克隆无需额外标志，克隆会复制远端仓库的格式。
- quote: To make SHA-256 your default before Git 3.0 does, set git config --global init.defaultObjectFormat sha256, or export GIT_DEFAULT_HASH=sha256 for a single shell. Both worked on 2.51.0. Cloning needs no flag at all: a clone of invoice-api came out as sha256 because a clone copies the format of its remote.
- url: https://devtoollab.com/blog/git-sha256
- 人工: [ ] supported  [ ] partial  [ ] unsupported
- note: 

## 27
- claim: HTTP/3 起源于 HTTP 语义在 QUIC 传输之上的映射文档，最初的草案名为 draft-shade-quic-http2-mapping，后被采纳为 draft-ietf-quic-http 的基础。
- quote: *  Adopted as base for draft-ietf-quic-http
- url: https://datatracker.ietf.org/doc/draft-ietf-quic-http/34
- 人工: [ ] supported  [ ] partial  [ ] unsupported
- note: 

## 28
- claim: 由于 QUIC 不保证有序传输，HTTP/3 用 QPACK（RFC 9204）替换 HPACK 进行字段压缩。
- quote: Because HPACK ([HPACK]) relies on in-order transmission of compressed field sections (a guarantee not provided by QUIC), HTTP/3 replaces HPACK with QPACK ([QPACK]).
- url: https://www.rfc-editor.org/rfc/rfc9114.html
- 人工: [ ] supported  [ ] partial  [ ] unsupported
- note: 

## 29
- claim: 定位上，TensorRT-LLM 是面向 NVIDIA GPU（桌面或数据中心）大型语言模型的开源库，提供模块化 Python 运行时、PyTorch 原生模型编写和稳定生产 API。
- quote: NVIDIA TensorRT™ LLM is an open-source library built to deliver high-performance, real-time inference optimization for large language models (LLMs) on NVIDIA GPUs—whether on a desktop or in a data center. It includes a modular Python runtime, PyTorch-native model authoring, and a stable production API.
- url: https://developer.nvidia.com/tensorrt-llm
- 人工: [ ] supported  [ ] partial  [ ] unsupported
- note: 

## 30
- claim: 在混合检索中，BM25 与稠密检索通过 RRF 融合后，在所有指标与所有数据子集上都优于两种单项方法，论文因此建议将混合检索作为任何 RAG 部署的最小可行基线。
- quote: Combining BM25 and dense retrieval via Reciprocal Rank Fusion improves over both constituent methods across all metrics and all dataset subsets.
- url: https://arxiv.org/html/2604.01733v1
- 人工: [ ] supported  [ ] partial  [ ] unsupported
- note: 
