# Evaluation 架构与指标

[运行入口](../eval/README.md) · [RAG 方法](RAG_EVALUATION.md) · [API 契约](API_CONTRACT.md)

## 验证层次

| 验证 | 检查什么 | 不能证明什么 |
| --- | --- | --- |
| 确定性工程测试 | 使用 mock 或临时数据检查安全、接口、状态传播和评分规则 | 真实模型回答质量 |
| Agent HTTP Eval | 完整 Chat 链路的路由、工具、执行、证据、回答和耗时 | 全业务覆盖或生产可靠性 |
| RAG Retrieval | 知识服务的召回、排序、来源与成本 | 完整 Agent 回答正确 |

HTTP 路径为 Router → Planner → Tool Selection → SQL/RAG/Graph → Evidence → Answer。评分优先读取公开 execution；Trace 用于诊断过程，不替代业务证据。

本仓库公开通用 Runner、评分器和工程测试，不分发内部 Benchmark 输入、实验报告或运行档案。使用者需自备 JSONL 和独立源事实，不能从本次模型回答反推 Golden。

## 指标

| 指标 | 原理 |
| --- | --- |
| HTTP Success / Agent Completed | 正常响应与流程结束分别统计，不等于业务成功 |
| Route Accuracy | canonical route 与预期一致 |
| Tool Selection Accuracy | 预期工具覆盖比例，多选仍可能覆盖预期 |
| Tool Exact Match | 工具集合完全匹配 |
| Tool Execution Coverage | 预期工具实际执行，不能只看 dispatch |
| Non-error / All Success | success 或 empty 与全部严格 success 分开 |
| Execution Status / Evidence Availability | 终态及证据有无符合事先声明 |
| Expected Fact Literal Occurrence in Evidence | 预期事实在合并 Evidence 中字面出现，不保证完整语义关联 |
| Answer Literal Fact Coverage | 题内预期事实覆盖比例，再对适用题取均值 |
| Declared Fact Assertions | 声明数值、来源和行集合检查；断言点与整题全部通过分别统计 |
| Fallback / Error / Latency | 回退、错误与观测等待，不充当回答正确率 |

无工具请求不进入工具分母，没有预期事实不进入事实分母；null 不算成功或失败。正确 empty 不等于 error，正确策略拒绝可为预期 error，超时不能冒充拒绝。

answer_faithfulness 是历史字面覆盖兼容字段，不是完整 Semantic Faithfulness。预期词出现不等于逐句 Groundedness，引用可解析也不证明来源支持全部声明。

## 声明检查与门槛

[fact_assertions.py](../eval/evaluators/fact_assertions.py) 支持 answer_fact、evidence_fact、evidence_scalar、evidence_row、evidence_rows，分别检查表述、来源、单列聚合、同行字段和完整集合。别名须显式声明，冲突别名不能挑值通过。否定、矛盾、条件和额外声明仍需人工语义校准；review_checks 只是复核提示。

启用 --fail-on-regression 后，违反路由、工具集合、执行、证据、断言、引用或回退声明等检查会失败，并保留结果：

- PASS：没有自动阻断或警告，不代表所有语义正确。
- WARN：字面差异或回退等需复核项。
- FAIL：违反声明、意外错误或运行/相应快照检查不完整。

expected_fallback=true 要求发生，false 要求不发生；未声明默认不允许，allow_fallback=true 表示允许但不要求，不能与 expected_fallback=false 同用。允许或预期的实际回退仍列运行关注警告。

## 耗时与可追溯性

HTTP 非正常状态与没有响应的 transport error 分开。Trace 配对 start 与 done/error/timeout，区分完成耗时和观测等待；缺终态不伪造完成时间。并行调用的累计时间不等于墙钟等待，失败和慢样本应保留，小样本 P95 不是生产 SLA。

Runner 保存输入哈希、源码指纹、参数及可获得的模型观测，但不能冻结远端模型或数据库。原始响应、Trace、环境与源码快照应留在本地，发布前另查敏感信息及历史提交。命令见[运行入口](../eval/README.md)。
