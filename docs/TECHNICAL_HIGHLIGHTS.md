# 技术亮点与实现依据

> 本文整理基线版本 `7c1d574ccde4472a66ddc7e47a8fee1bc76a59ad` 中已经实现、可从代码或验证记录复核的工程能力。规划项和已知限制单独标注。

## 一页摘要

| 亮点             | 解决的问题                                  | 关键实现                                                                 | 证据入口                                                     |
|:-----------------|:--------------------------------------------|:-------------------------------------------------------------------------|:-------------------------------------------------------------|
| 多源 Agent 编排  | 一个问题可能需要 SQL、文档和图关系          | Router / Planner / Tool Selection 分层                                   | `routing.py`、`ARCHITECTURE.md`                              |
| 统一工具边界     | 不同数据工具返回格式和错误语义不一致        | ToolRegistry、UnifiedToolExecutor、ToolResult                            | `tool_execution/interface.py`、`tool_contracts.py`           |
| Evidence Lineage | 最终答案难以追溯到具体数据来源              | task-scoped Evidence ID、EvidenceContext、citation IDs                   | `tool_contracts.py`、`execution_contract.py`                 |
| Text2SQL 防护    | LLM 生成的 SQL 可能越权、写入或访问错误数据 | SQLGlot、只读数据库账号、Schema/Table allowlist、timeout、bounded repair | `business_database.py`、`TEXT2SQL_IMPLEMENTATION.md`         |
| RAG 检索治理     | 检索结果可能跨域、重复或来源不清            | PGVector、domain/dataset/as_of 过滤、可选 Hybrid + RRF、服务端 rerank    | `knowledge_rag.py`、`RAG_EVALUATION.md`                      |
| Graph 策略封装   | 固定查询覆盖有限，动态查询需要控制边界      | Predefined Cypher + Text2Cypher 统一成 graph_tool                        | `graph_tool.py`                                              |
| 公共执行契约     | 内部状态不适合直接暴露给前端                | 版本化、字段 allowlist、长度/数量受限 Execution DTO                      | `execution_contract.py`                                      |
| 安全与可复现验证 | Agent API 和数据源需要明确权限边界          | JWT owner check、DB 分离、内部服务令牌、77 项安全测试                    | `security.py`、`business_database.py`、`SECURITY_RELEASE.md` |

## 1. 多源任务编排：分离产品路由与数据源选择

### 问题

如果 Router 直接将请求路由到 SQL、RAG 或 Graph，产品入口就会和具体数据源耦合。遇到跨源问题时，也难以表达多个独立任务。

### 实现

``` text
Semantic Router
  ├─ general-query
  ├─ additional-query
  ├─ file-query
  └─ data-query
       ↓
  Generic Planner
       ↓
  Tool Selection (per task)
       ↓
  sql_agent / knowledge_rag / graph_tool
```

- Router 决定产品处理路线。
- Planner 将复杂问题拆成结构化任务。
- Tool Selection 对每个任务选择 canonical tool。
- 独立任务可以并行执行，最终统一聚合证据。

### 工程价值

产品请求分类不再与具体数据源绑定；跨源请求可以被拆成多个独立任务，再由任务级工具选择决定数据访问方式。这使新增或替换数据工具时，不必同步改写产品入口路由。

### 当前边界

当前支持独立子任务并行和 Evidence 聚合。`depends_on` 是任务描述字段，尚未实现前序任务结果驱动后续输入的完整 DAG scheduler。

## 2. 统一工具协议：ToolRegistry + UnifiedToolExecutor

### 问题

SQL、RAG 和 Graph 的 client、返回值、错误和 Trace 形态不同。如果 Planner 直接依赖各工具实现，编排层会与数据源细节耦合。

### 实现

三个 canonical tools 采用统一执行接口：

``` python
async def execute(
    task: str,
    context: dict | None = None,
    *,
    trace=None,
) -> ToolResult:
    ...
```

`ToolRegistry` 负责 canonical tool 和兼容 alias；`UnifiedToolExecutor` 统一执行入口、timeout、有限 transient retry、错误收敛和 Trace 事件。

### 工程价值

编排层只依赖统一的执行接口和 `ToolResult`，不直接依赖 MySQL、PGVector、Neo4j 的 client 细节。工具内部可以独立演进，公共执行边界则负责统一处理超时、有限瞬态重试、错误收敛和 Trace。

### 代码入口

- `multisource_data_agent/application/agents/tool_execution/interface.py`
- `multisource_data_agent/application/agents/tool_contracts.py`

## 3. Evidence Lineage：让答案回到来源，而不是只返回文本

### 问题

只返回自然语言答案会丢失 SQL 行、文档来源和图记录，无法让用户或评测器检查答案依据。

### 实现

``` text
ToolResult
  └─ Evidence[]
       ├─ evidence_id
       ├─ source / source_id
       ├─ task_id
       ├─ content
       ├─ rank / score
       └─ metadata
            ↓
       EvidenceContext
            ↓
       AnswerGenerator
            ↓
       answer + citation_ids
```

- `ToolResult` 表达工具执行结果。
- `Evidence` 表达可用于答案生成的依据。
- `source_id` 保留底层来源身份；`evidence_id` 用于当前执行中的引用关联。
- Evidence 聚合后由共享 AnswerGenerator 生成答案。
- HTTP DTO 只暴露有界预览和安全字段。

### 工程价值

执行状态与事实依据可以分别检查：`ToolResult` 用于判断工具是否成功执行，`Evidence` 用于判断有哪些来源事实能够支持答案。SQL 行、知识文档和图记录由此可以进入同一证据聚合与引用流程。

### 代码入口

- `multisource_data_agent/application/agents/tool_contracts.py`
- `multisource_data_agent/application/agents/execution_contract.py`

## 4. Text2SQL：模型生成与数据库执行权限分离

### 问题

Prompt 不能构成数据库安全边界。模型可能生成写操作、越权表引用、错误字段或高成本查询。

### 实现

``` text
Natural Language
→ Schema Retrieval
→ Query Analysis
→ SQL Generation
→ SQLGlot AST Validation
→ Business Schema/Table Allowlist
→ Read-only DB Grants Check
→ LIMIT + DB Timeout
→ Execute
→ Optional one-time repair
→ Re-validate before retrying execution
```

纵深防御包含：

1.  `APP_DATABASE_URL` 与 `BUSINESS_DATABASE_URL` 分离，缺少业务库配置时 fail closed。
2.  SQL Agent 使用独立业务只读账号。
3.  执行前检查实际 grants，拒绝非只读权限和超出范围的授权。
4.  显式业务 Schema / Table allowlist，内部应用表不在允许范围。
5.  SQLGlot AST 校验 statement、表和字段引用。
6.  LIMIT 控制返回规模，数据库 timeout 控制执行时间。
7.  最多一次受控修复；修复结果重新经过同一安全链。

### 工程价值

模型输出只作为待验证的 SQL 候选，不直接获得数据库执行权限。数据库账号权限、实际 grants 检查、Schema / Table allowlist 与 AST 校验共同构成执行边界；受控修复后的 SQL 也必须重新经过校验。

### 代码入口

- `multisource_data_agent/infrastructure/core/business_database.py`
- `multisource_data_agent/application/agents/text2sql/`
- `docs/TEXT2SQL_IMPLEMENTATION.md`

## 5. RAG：把检索、重排和回答质量分开治理

### 问题

向量召回分数不是答案置信度。混合检索如果没有统一过滤和重排，也可能引入重复候选或错误排序。

### 实现

- canonical backend：PostgreSQL / PGVector。
- domain、dataset 和显式日期过滤由请求上下文传递。
- 默认 dense retrieval。
- 可选 Hybrid 模式：PGVector + PostgreSQL full-text candidates，经 RRF 融合。
- reranking 留在 `kb_ingest`，应用层不重复 rerank。
- 稳定文档身份：`(domain, dataset_id, source_id)`。
- 原文 Evidence 是依据，辅助改写文本不替代原文。

### 评测方法

分别报告 Candidate Recall、Recall@K、MRR@K、Precision@K、HTTP 失败、fallback 和耗时。RAG Retrieval Eval 不等于完整 Agent Answer Eval。

### 工程价值

检索召回、候选排序和最终答案质量分别评估，避免把“召回到相似文档”直接等同于“回答正确”。检索指标用于检查候选覆盖与排序，Agent HTTP Eval 则进一步检查工具执行、Evidence、引用和回答结果。

### 代码入口

- `multisource_data_agent/application/agents/tool_execution/knowledge_rag.py`
- `kb_ingest/kb_service/services/search.py`
- `docs/RAG_EVALUATION.md`
- `docs/EVALUATION.md`

## 6. Graph Tool：预定义查询与动态查询共用执行边界

### 问题

预定义 Cypher 更可控，但覆盖面有限；动态 Text2Cypher 灵活，但需要额外的校验和错误处理。

### 实现

两种策略统一封装为 `graph_tool`：

- Predefined Cypher：通过查询描述和模板匹配选择已知查询。
- Text2Cypher：在适用场景下生成动态查询。
- 统一返回 `ToolResult` 和 Graph Evidence。
- 上层不暴露策略内部差异。

### 工程价值

预定义查询和动态 Text2Cypher 在工具内部作为不同执行策略，对外维持单一 `graph_tool` 契约。策略调整不会迫使 Planner 和答案生成层维护两套 Graph 工具协议。

### 代码入口

- `multisource_data_agent/application/agents/tool_execution/graph_tool.py`
- `multisource_data_agent/application/agents/kg_sub_graph/`

## 7. Execution DTO：内部状态与公共 API 分离

### 问题

完整 LangGraph State 可能包含内部 metadata、原始错误、过大的 rows/documents 和不适合公共暴露的字段。

### 实现

`ExecutionDTO` 是版本化 HTTP 边界：

- 固定 `contract_version`。
- request status：`success` / `empty` / `partial` / `error`。
- tool status：`success` / `empty` / `error`。
- 只暴露 allowlist metadata。
- 限制 statement、cell、document、preview 和 evidence 数量。
- 敏感字段与连接串做过滤。
- 返回 tasks、tool_results、evidence、citation_ids 和 timings。

### 工程价值

内部 LangGraph State 与公共 HTTP 契约解耦。版本化 DTO 对字段、长度和数量设置边界，减少内部状态变化对前端的影响，也避免把完整内部对象直接暴露给客户端。

### 代码入口

- `multisource_data_agent/application/agents/execution_contract.py`
- `docs/API_CONTRACT.md`

## 8. 安全和工程验证：把边界变成自动化测试

发布基线 `7c1d574` 的验证记录：

| 项目                        | 结果                                                                                   |
|:----------------------------|:---------------------------------------------------------------------------------------|
| Python 3.10                 | 394 passed / 0 failed / 0 skipped                                                      |
| Python 3.12                 | 394 passed / 0 failed / 0 skipped                                                      |
| 新增安全测试                | 77 项                                                                                  |
| 前端 lint / build           | 通过                                                                                   |
| npm audit                   | 0 漏洞                                                                                 |
| Wheel 构建、安装、隔离导入  | 通过                                                                                   |
| Docker 镜像与五服务隔离验证 | 通过，详见发布记录                                                                     |
| GitHub Actions              | [通过记录](https://github.com/YingliaoO/langgraph-data-agent/actions/runs/42011295876) |

这些结果证明确定性测试和指定发布检查通过，不证明真实付费模型的端到端答案质量。本轮未执行真实 LLM / Embedding / Reranker 完整 Smoke。

## 实现与验证的复核路径

建议按以下顺序检查代码与验证记录：

1.  阅读 [ARCHITECTURE.md](ARCHITECTURE.md)，了解 Router、Planner、Tool Selection、统一执行边界和 Evidence 汇总链路。
2.  阅读 [TEXT2SQL_IMPLEMENTATION.md](TEXT2SQL_IMPLEMENTATION.md)，核对 SQL 生成后的 AST 校验、数据库权限和查询限制。
3.  阅读 [RAG_EVALUATION.md](RAG_EVALUATION.md) 与 [EVALUATION.md](EVALUATION.md)，区分检索评测和完整 Agent HTTP 评测。
4.  阅读 [SECURITY_RELEASE.md](SECURITY_RELEASE.md)，核对安全回归测试、构建和发布验证结果。

当前能力边界以 [KNOWN_LIMITATIONS.md](KNOWN_LIMITATIONS.md) 和架构文档中的说明为准。确定性测试通过不代表真实模型端到端答案质量已经得到验证。
