# 关键设计决策（Design Decisions）

> 以 commit `7c1d574ccde4472a66ddc7e47a8fee1bc76a59ad` 为准。本文记录当前实现背后的主要选择、收益和明确代价，供架构理解、代码评审和后续维护参考。

## 决策总览

| ID    | 决策                                 | 主要收益                         | 明确代价 / 边界                         |
|:------|:-------------------------------------|:---------------------------------|:----------------------------------------|
| DD-01 | 产品路由与数据工具选择分层           | 降低路由与数据源耦合             | Planner / Tool Selection 增加一次决策层 |
| DD-02 | 三个 canonical tools 统一执行协议    | 上层编排不依赖工具实现           | 旧工具名需要 alias 兼容                 |
| DD-03 | ToolResult 与 Evidence 分离          | 执行状态与事实依据可独立追踪     | 需要维护 Evidence 身份和映射            |
| DD-04 | Evidence-first 共享答案生成          | 支持跨源证据汇总和引用           | 证据预算与答案上下文需要控制            |
| DD-05 | SQL 采用数据库权限 + AST + allowlist | Prompt 之外形成确定性执行边界    | 需要单独维护业务账号和白名单            |
| DD-06 | RAG 由 kb_ingest 持有检索与 rerank   | 避免重复重排，统一检索后端       | 检索服务成为运行时依赖                  |
| DD-07 | Graph 预定义与动态查询统一成一个工具 | 策略可演进而不改变上层协议       | 动态查询仍需独立验证和监控              |
| DD-08 | 公开 Execution DTO 与内部 State 分离 | API 稳定、有界、减少敏感信息暴露 | DTO 需要显式版本演进                    |
| DD-09 | 有界重试与显式状态分类               | 防止无意义重试和错误统计失真     | 某些可恢复错误不会自动重试              |
| DD-10 | 最小 JWT + owner 校验                | 建立基本身份和对象边界           | 不是完整用户管理 / RBAC                 |
| DD-11 | 进程内 MemorySaver 与聊天历史分开    | 降低当前 demo 部署复杂度         | checkpoint 不跨进程持久化               |
| DD-12 | 合成数据 + 分层评测                  | 公开可复现演示且不暴露企业数据   | 不代表真实业务分布或模型泛化            |

## DD-01：产品路由与数据工具选择分层

**背景**

如果顶层 Router 直接路由到 SQL、RAG 或 Graph，产品处理路径会和某个数据库实现绑定；跨源问题也容易退化为复杂的 route 组合。

**决策**

Router 只选择四种产品路径：`general-query`、`additional-query`、`data-query`、`file-query`。只有 `data-query` 进入 Generic Planner；每个 Planner task 再由 Tool Selection 选择 canonical tool。

**收益**

- 产品请求分类与底层数据源解耦。
- 一个自然语言请求可以拆成多个数据任务。
- 更换某个数据工具时，不必重新定义产品 route。

**代价与边界**

多了一层 Planner 和 Tool Selection 决策。当前支持独立子任务并行，不是完整的依赖式 DAG 调度。

## DD-02：三个 canonical tools 统一执行协议

**背景**

SQL、RAG、Graph 的底层 client 和输出结构不同；若上层直接调用各自函数，编排层会积累数据源特定逻辑。

**决策**

统一为 `sql_agent`、`knowledge_rag`、`graph_tool` 三个 canonical tools。每个工具实现统一的 `execute(task, context, trace) -> ToolResult`；ToolRegistry 负责 canonical name 与 legacy alias，UnifiedToolExecutor 提供公共执行边界。

**收益**

- 统一超时、错误收敛、有限 transient retry 和 Trace。
- 工具内部可以独立替换数据库 client 或检索策略。
- Planner / AnswerGenerator 不需要知道底层驱动。

**代价与边界**

统一协议只统一调用和结果，不代表三个工具的内部语义相同。特定参数仍通过 context 明确传递。

## DD-03：ToolResult 与 Evidence 分离

**背景**

工具执行成功不代表工具返回的全部内容都适合直接作为最终答案依据；工具状态和事实来源是两个不同概念。

**决策**

`ToolResult` 表示执行本身，包含 tool、status、statement、data、metadata；`Evidence` 表示可用于答案生成的依据，包含 evidence ID、source、source ID、task ID 和内容等。

**收益**

- `empty` 可以是正常业务结果。
- 一个工具结果可以包含零条或多条 Evidence。
- 证据可以跨工具聚合和引用。
- 评测器可以分别检查工具是否执行、Evidence 是否可用、答案是否覆盖事实。

**代价与边界**

需要保持 evidence ID、source ID 和 task ID 的语义稳定，不能将展示 ID 当作底层业务主键。

## DD-04：Evidence-first 共享答案生成

**背景**

若 SQL、RAG、Graph 各自生成最终自然语言答案，再把文本拼接起来，容易重复、矛盾且难以追溯。

**决策**

工具返回 ToolResult / Evidence；统一构造 EvidenceContext，再由共享 AnswerGenerator 生成最终答案与 citation IDs。

**收益**

- 同一个答案可以引用多个数据源。
- 回答生成逻辑不与某个工具绑定。
- 可以对 Evidence 和引用建立独立评测。

**代价与边界**

Evidence 需要有容量预算和来源过滤；引用可解析不等于每一句自然语言都被来源完整支持，仍需要语义评估。

## DD-05：SQL 纵深防御，而不是 Prompt-only 安全

**背景**

模型可能生成写操作、越权表、错误字段或高成本查询。仅在 Prompt 中要求”只读”不能形成可靠执行边界。

**决策**

采用多层防御：

1.  `APP_DATABASE_URL` 与 `BUSINESS_DATABASE_URL` 分离，缺少业务库配置时 fail closed。
2.  使用独立业务只读账号。
3.  执行前检查真实数据库 grants。
4.  强制配置业务 Schema / Table allowlist，并排除内部应用表。
5.  SQLGlot AST 检查 statement、表和字段。
6.  LIMIT 与数据库 timeout 分别控制结果规模和执行时长。
7.  最多一次受控修复，修复后重新校验。

**收益**

即使模型输出异常 SQL，也不能仅凭模型意图取得数据库写权限或访问任意表。

**代价与边界**

业务表和 Schema 变化需要维护白名单与权限。当前不是业务数据行级 / 租户级权限系统；数据库账号和白名单仍需按部署环境正确配置。

## DD-06：RAG 检索与 rerank 由 kb_ingest 持有

**背景**

如果应用层和知识服务分别执行 rerank，可能重复调用模型、增加成本和延迟，也会让检索逻辑分散。

**决策**

canonical RAG backend 使用 kb_ingest 暴露的 PGVector 搜索接口；rerank 在知识服务内部完成。默认 dense retrieval；Hybrid + PostgreSQL full-text + RRF 为可选模式。

**收益**

- 单一检索服务边界。
- 不重复 rerank。
- domain、dataset、日期过滤与来源元数据集中处理。
- 可以独立评估候选召回和最终排序。

**代价与边界**

kb_ingest 成为 Agent 查询链路的运行时依赖。Hybrid 并非默认模式，也不保证在所有语料上优于 dense；必须用同一数据集和过滤范围实测。

## DD-07：Graph 的预定义与动态查询统一为 graph_tool

**背景**

预定义 Cypher 对已知业务查询更稳定，但覆盖有限；Text2Cypher 可以处理更多问题，但动态生成的查询需要安全校验和执行控制。

**决策**

把预定义 Cypher 与动态 Text2Cypher 作为 GraphTool 内部策略，对外只暴露 `graph_tool` 和统一 ToolResult。

**收益**

- 上层不需要维护两套 Graph 工具。
- 可以先尝试高确定性的预定义策略，再在适用场景使用动态查询。
- 策略演进不影响 Planner 和 AnswerGenerator。

**代价与边界**

动态查询的质量仍受模型、Schema 上下文和验证策略影响。不能把”支持 Text2Cypher”表述成”任意图查询均可靠”。

## DD-08：内部 LangGraph State 与公开 Execution DTO 分离

**背景**

完整 Graph State 可能包含内部对象、敏感 metadata、超长结果和不稳定字段，不适合作为前端 API。

**决策**

通过 `ExecutionDTO` 生成版本化公共视图，设置字段 allowlist、长度、层级和数量限制。

**收益**

- API 契约稳定，不随内部 State 变化而变化。
- 结果预览和 Evidence 数量有界。
- 可以对敏感字段和连接串做过滤。
- 前端能展示任务、工具状态、Evidence、引用和耗时。

**代价与边界**

新增字段需要考虑 `contract_version` 和兼容策略。公共 preview 不是完整数据库结果集。

## DD-09：有界重试与显式状态分类

**背景**

把所有异常都重试会放大成本和延迟；把空结果、策略拒绝和服务异常混为一类，会让评测指标失真。

**决策**

工具状态区分 `success`、`empty`、`error`；请求状态区分 `success`、`empty`、`partial`、`error`。只对明确 transient transport failures 做有限重试，策略、鉴权、验证、计费和 timeout 不作为普通重试目标。

**收益**

- `empty` 不会被错误统计为失败。
- 部分成功可以保留已有有效结果。
- 重试成本和行为可预测。
- Eval 可以区分工具选择错误、执行错误和证据缺失。

**代价与边界**

某些边界不清晰的 provider 错误可能不会自动重试；应通过 Trace 和错误分类定位，而不是无差别增加重试。

## DD-10：最小 JWT 身份与对象 owner 校验

**背景**

仅由请求体传入 `user_id` 无法建立可信用户身份；只按 session ID 查询也可能造成跨用户访问。

**决策**

Chat、Session、History、Upload 使用短期 JWT Bearer；服务端从签名 `sub` 获取主体，并在对象访问时校验 owner。知识服务使用独立内部服务令牌。

**收益**

- 请求体不能覆盖认证身份。
- Session、History、Upload 有基本对象级隔离。
- 服务间调用与用户身份分开。

**代价与边界**

这是最小身份验证，不包含完整注册、用户生命周期、RBAC、权限委派、令牌撤销平台或业务数据行级权限。生产部署还需可信身份签发、HTTPS、密钥轮换和流量控制。

## DD-11：当前 checkpoint 与聊天历史分开处理

**背景**

为当前演示引入外部 checkpoint 存储会增加部署依赖和运维复杂度。

**决策**

LangGraph 使用进程内 `MemorySaver`；聊天历史由关系数据库单独持久化。

**收益**

本地演示和确定性测试部署简单，聊天记录仍可持久化。

**代价与边界**

MemorySaver 不跨进程持久化，多 worker 不共享 checkpoint。聊天历史不等于完整 Graph State 恢复。若进入多实例生产，需要持久化 Checkpointer 和恢复策略。

## DD-12：合成演示数据与分层评测

**背景**

公开仓库需要可运行的端到端场景，但不应分发真实企业数据或内部 Benchmark。

**决策**

提供 Synthetic Enterprise Retail 作为演示数据；公开通用评测 Runner 和评分器，不分发内部专项语料、Golden 数据和实验档案。评测分为确定性工程测试、Agent HTTP Eval 和 RAG Retrieval Eval。

**收益**

- 仓库访客可以复现 SQL、RAG 和 Graph 场景。
- 不暴露企业数据或内部评测材料。
- 检索质量与最终回答质量不会被混成一个指标。

**代价与边界**

合成数据不代表真实业务分布。确定性测试不证明真实模型质量；本发布基线没有执行真实付费模型端到端 Smoke，也没有公开可泛化的 Benchmark 准确率。
