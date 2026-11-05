# 系统架构与执行模型

> 本文以 `7c1d574ccde4472a66ddc7e47a8fee1bc76a59ad` 为基线。图示描述当前实现，不把规划能力或未来生产化方向写成已完成能力。

## 1. 架构目标

系统面向异构数据分析请求，解决三个问题：

1.  **异构数据如何统一调用**：SQL、文档检索和图查询采用不同技术栈，但上层需要稳定的执行协议。
2.  **跨工具结果如何可信汇总**：最终答案需要保留事实来源、任务归属和引用关系。
3.  **Agent 执行如何可检查**：需要区分路由、规划、工具选择、执行状态、Evidence、回退和耗时。

核心链路：

``` text
Request
→ Semantic Router
→ Generic Planner
→ Tool Selection
→ Unified Tool Executor
→ ToolResult
→ Evidence
→ EvidenceContext
→ AnswerGenerator
→ Execution DTO
```

## 2. 系统总览

``` mermaid
flowchart TB
    subgraph Client["Client"]
        USER[User]
        WEB[Vue 3 Console]
        USER --> WEB
    end

    subgraph API["HTTP Boundary"]
        FASTAPI[FastAPI]
        AUTH[JWT Bearer Authentication]
        OWNER[Session / History / Upload Owner Checks]
        FASTAPI --> AUTH --> OWNER
    end
    WEB --> FASTAPI

    subgraph Agent["LangGraph Agent Runtime"]
        ROUTER[Semantic Router]
        GENERAL[General Response]
        CLARIFY[Additional Information]
        FILE[File Ingestion Route]
        PLANNER[Generic Planner]
        SELECT[Tool Selection]
        EXEC[Unified Tool Executor]
        OWNER --> ROUTER
        ROUTER -->|general-query| GENERAL
        ROUTER -->|additional-query| CLARIFY
        ROUTER -->|file-query| FILE
        ROUTER -->|data-query| PLANNER
        PLANNER --> TASKS[Structured Tasks]
        TASKS --> SELECT --> EXEC
    end

    subgraph Tools["Canonical Data Tools"]
        SQL[sql_agent]
        RAG[knowledge_rag]
        GRAPH[graph_tool]
        EXEC --> SQL
        EXEC --> RAG
        EXEC --> GRAPH
    end

    subgraph Data["Data Plane"]
        BIZ[(Business MySQL)]
        KBAPI[kb_ingest API]
        PG[(PostgreSQL + PGVector)]
        NEO[(Neo4j)]
        SQL --> BIZ
        RAG --> KBAPI --> PG
        GRAPH --> NEO
    end

    subgraph Synthesis["Evidence & Response"]
        TR[ToolResult]
        EV[Evidence]
        EC[EvidenceContext]
        AG[Shared AnswerGenerator]
        DTO[Versioned Execution DTO]
        SQL --> TR
        RAG --> TR
        GRAPH --> TR
        TR --> EV --> EC --> AG --> DTO
    end

    DTO --> FASTAPI
    FASTAPI --> WEB
```

## 3. 请求路径

### 3.1 四种产品路由

Router 判断产品处理路线，而不是直接选择底层数据库：

| Route              | 责任             | 后续处理                         |
|:-------------------|:-----------------|:---------------------------------|
| `general-query`    | 普通对话         | 普通回答                         |
| `additional-query` | 缺少必要信息     | 澄清或补充信息                   |
| `data-query`       | 需要查询业务数据 | Planner → Tool Selection → Tools |
| `file-query`       | 上传文件处理     | 受控文件摄取                     |

历史数据源专用 route 仅作为兼容输入，进入编排前归一到 canonical route。

### 3.2 Data-query 执行时序

``` mermaid
sequenceDiagram
    autonumber
    participant U as User / Vue
    participant API as FastAPI
    participant R as Router
    participant P as Planner
    participant S as Tool Selection
    participant X as Unified Executor
    participant T as SQL / RAG / Graph
    participant D as Data Source
    participant E as Evidence / Answer

    U->>API: POST /chat + Bearer token
    API->>API: Verify JWT subject and ownership
    API->>R: User request
    R-->>API: data-query
    API->>P: Plan request
    P-->>API: Structured tasks
    loop Each independent task
        API->>S: Select canonical tool
        S-->>API: sql_agent / knowledge_rag / graph_tool
        API->>X: execute(tool, task, context)
        X->>T: Unified execute contract
        T->>D: Query / retrieve
        D-->>T: Rows / documents / graph records
        T-->>X: ToolResult
        X-->>API: ToolResult + trace
    end
    API->>E: Normalize Evidence and build context
    E-->>API: Answer + citation IDs
    API->>API: Persist final assistant message
    API-->>U: Answer + bounded Execution DTO
```

当前支持独立子任务并行执行和证据聚合。Planner 的 `depends_on` 是任务关系描述，不代表系统已实现前序结果驱动后续输入的阻塞式 DAG scheduler。

## 4. Router、Planner、Tool Selection 的职责边界

### Semantic Router：产品处理路线

Router 将请求分成普通对话、澄清、数据查询和文件摄取。它不承担 SQL / RAG / Graph 的细粒度选择。

### Generic Planner：任务拆解

Planner 将复杂问题转换为结构化任务。任务描述包括 task ID、问题、工具提示和 `depends_on` 等字段。

例如：

``` json
[
  {
    "task_id": "task_1",
    "question": "统计 Aurora ANC Headphones 的销量",
    "tool_hint": "auto",
    "depends_on": []
  },
  {
    "task_id": "task_2",
    "question": "查询该产品的供应商",
    "tool_hint": "auto",
    "depends_on": []
  },
  {
    "task_id": "task_3",
    "question": "检索电子产品退货政策",
    "tool_hint": "auto",
    "depends_on": []
  }
]
```

### Tool Selection：每个任务选择数据能力

Tool Selection 从三个 canonical tools 中选择：

| Tool            | 典型问题                         |
|:----------------|:---------------------------------|
| `sql_agent`     | 统计、筛选、聚合、排序、业务明细 |
| `knowledge_rag` | 政策、SOP、制度、文档事实        |
| `graph_tool`    | 实体关系、供应商、品类、活动关联 |

工具选择是语义决策，不是简单的关键词映射。

## 5. 统一工具执行边界

三个 canonical tools 对外采用相同接口：

``` python
async def execute(
    task: str,
    context: dict | None = None,
    *,
    trace=None,
) -> ToolResult:
    ...
```

`ToolRegistry` 负责 canonical name 和历史 alias 的解析；`UnifiedToolExecutor` 负责统一执行入口、超时、有限瞬态重试、错误收敛和 Trace 事件。它不负责替代 Router 或 Planner。

### ToolResult

``` text
tool_name
tool_kind
status: success | empty | error
task
statement
data
answer
evidence
evidence_ids
metadata
```

`empty` 是合法业务结果，不等同于异常。工具错误保留为工具级结果，允许上层判断是否仍有可用证据。

## 6. 三种数据工具

### 6.1 SQL Agent

``` text
Task
→ Schema Retrieval
→ Query Analysis
→ SQL Generation
→ SQLGlot AST / Semantic Validation
→ Business Schema & Table Allowlist
→ Read-only DB Permission Check
→ LIMIT / DB Timeout
→ MySQL
→ bounded repair（最多一次，修复后重新校验）
→ ToolResult
```

关键边界：

- `APP_DATABASE_URL` 与 `BUSINESS_DATABASE_URL` 分离；业务连接缺失时不回退应用数据库。
- 使用独立业务只读账号；执行前检查实际数据库 grants。
- 业务 Schema / Table 必须显式配置白名单，内部应用表被拒绝。
- SQLGlot 对查询 AST、statement 类型、表和字段引用进行检查。
- 候选 SQL 不等于可执行 SQL；修复后的 SQL 仍需重新走安全校验。
- LIMIT 限制返回规模，数据库 timeout 限制执行时间；二者解决不同问题。

### 6.2 Knowledge RAG

``` text
Task + domain / dataset / as_of
→ kb_ingest API
→ PGVector candidate retrieval
→ optional PostgreSQL full-text retrieval
→ optional RRF fusion
→ server-side rerank
→ Documents
→ ToolResult + document Evidence
```

当前 canonical RAG backend 是 PostgreSQL / PGVector，重排由 `kb_ingest` 服务负责，应用层不再重复 rerank。默认检索模式为 dense；Hybrid + RRF 是可选模式。

文档身份按 `(domain, dataset_id, source_id)` 组织；检索上下文可携带 domain、dataset 和显式日期过滤。原文 Evidence 是回答依据，辅助改写内容不替代原文。

### 6.3 Graph Tool

``` text
Task
→ Predefined Cypher strategy
→ dynamic Text2Cypher strategy（适用时）
→ Cypher validation / execution
→ Neo4j records
→ ToolResult + graph Evidence
```

预定义查询与动态 Text2Cypher 对外统一为 `graph_tool`。上层不需要感知内部采用哪一种策略。动态查询能力不意味着允许任意 Cypher 写操作。

## 7. Evidence Lineage 与答案生成

工具结果描述执行，Evidence 描述可供回答使用的依据。

| Tool            | Evidence kind  | Source     |
|:----------------|:---------------|:-----------|
| `sql_agent`     | `sql_row`      | `mysql`    |
| `knowledge_rag` | `document`     | `pgvector` |
| `graph_tool`    | `graph_record` | `neo4j`    |

Evidence 包含 evidence ID、source ID、task ID、内容和必要的检索/排序元数据。任务范围内的稳定 ID 用于将答案引用关联回工具执行结果。

统一回答链：

``` text
ToolResult(s)
→ normalized Evidence(s)
→ EvidenceContext
→ AnswerGenerator
→ answer + citation_ids
```

因此，多源问题可以在同一回答中结合 SQL 行、知识文档和图记录；不是把各工具的自然语言答案直接拼接。

## 8. HTTP Execution DTO

HTTP 不直接暴露完整 LangGraph State。`execution_contract.py` 将内部工具结果转换为版本化、字段白名单、长度和数量受限的公共 DTO。

``` json
{
  "contract_version": "1",
  "status": "success | empty | partial | error",
  "tasks": [],
  "tool_results": [],
  "evidence": [],
  "citation_ids": [],
  "timings": {},
  "fallback_reason": "none"
}
```

请求状态与工具状态分层：

- Tool：`success` / `empty` / `error`
- Request：`success` / `empty` / `partial` / `error`

至少一个任务成功且至少一个任务失败时才是 `partial`；单纯出现合法 `empty` 不会自动变成 partial。

## 9. 安全边界

``` mermaid
flowchart LR
    CLIENT[Vue Client]
    TOKEN[Short-lived JWT]
    API[FastAPI Auth + Owner Check]
    APPDB[(APP_DATABASE_URL\nSessions / History / Upload Metadata)]
    SQL[sql_agent]
    BIZDB[(BUSINESS_DATABASE_URL\nDedicated Read-only Account)]
    ALLOW[Schema + Table Allowlist]
    RAG[knowledge_rag]
    SVC[kb_ingest\nInternal Service Token]
    PG[(PGVector)]
    FILE[file_id + Owner-bound Metadata]

    CLIENT -->|Bearer| API
    TOKEN --> API
    API --> APPDB
    API --> FILE
    API --> SQL
    SQL --> ALLOW --> BIZDB
    API --> RAG
    RAG -->|Internal token| SVC --> PG
```

| 边界      | 当前实现                                                       |
|:----------|:---------------------------------------------------------------|
| HTTP 身份 | 短期 JWT Bearer；身份来自签名 `sub`                            |
| 对象授权  | Session、History、Upload 做 owner 校验；请求体不能覆盖用户身份 |
| 应用数据  | `APP_DATABASE_URL` 保存应用数据                                |
| 业务查询  | `BUSINESS_DATABASE_URL` 使用独立只读账号和显式白名单           |
| 知识服务  | 独立内部服务令牌                                               |
| 文件      | 服务端生成存储名，以有归属的 `file_id` 解析受控路径            |
| 公共错误  | 返回安全错误类别、提示和 trace ID，不回显内部异常              |

当前 JWT 是最小身份验证与对象 owner 隔离，不是完整用户管理或 RBAC；业务数据尚无租户级 / 行级权限。公网部署还需要可信身份签发、HTTPS、密钥轮换、流量控制和相应的运维策略。

## 10. 运行时与可靠性

默认 Compose 包含五个服务：

| 服务          | 职责                                                   |
|:--------------|:-------------------------------------------------------|
| `backend`     | FastAPI、LangGraph、Router / Planner / Tools、Chat API |
| `mysql`       | 合成业务结构化数据                                     |
| `neo4j`       | 商品与供应商关系                                       |
| `kb_postgres` | 文档、向量和全文检索数据                               |
| `kb_ingest`   | 摄取、Embedding、检索与 Rerank                         |

执行预算和重试策略用于限制请求成本与等待时间。仅对明确 transient transport failure 做 bounded retry；策略拒绝、鉴权、验证、计费和 timeout 不混入普通重试。

SSE 当前表达 `progress`、`final`、`error`、`done` 事件，不模拟 Token Streaming。最终 Assistant 消息在完成后持久化。

## 11. 持久化边界

LangGraph checkpoint 当前使用进程内 `MemorySaver`。Chat History 另有关系数据库持久化，但两者不是同一份状态：聊天记录可持久化不代表完整 Graph State 可在重启后恢复。多 worker 共享 checkpoint 和完整恢复属于后续生产化工作。

文件摄取接口返回任务提交结果；提交成功不代表后台处理已经完成或文档已经可检索。

## 12. 评测边界

项目区分三层验证：

1.  **确定性工程测试**：接口契约、安全边界、状态传播和评测逻辑。
2.  **Agent HTTP Eval**：路由、工具执行、Evidence、回答和耗时。
3.  **RAG Retrieval Eval**：召回、排序、来源和检索成本。

检索命中不等于最终回答正确；HTTP 200 不等于业务查询成功；Citation 可解析不等于所有声明都被来源支持。

内部 Benchmark、专项语料和运行档案不随公开仓库分发。当前发布验证未执行真实付费模型端到端 Smoke。

## 13. 代码入口

| 模块                   | 入口                                                                        |
|:-----------------------|:----------------------------------------------------------------------------|
| Route 语义             | `multisource_data_agent/application/agents/routing.py`                      |
| Tool 契约              | `multisource_data_agent/application/agents/tool_contracts.py`               |
| 统一工具执行           | `multisource_data_agent/application/agents/tool_execution/interface.py`     |
| Execution DTO          | `multisource_data_agent/application/agents/execution_contract.py`           |
| SQL 数据库边界         | `multisource_data_agent/infrastructure/core/business_database.py`           |
| HTTP 身份与 owner 校验 | `multisource_data_agent/interfaces/http/security.py`                        |
| RAG 工具               | `multisource_data_agent/application/agents/tool_execution/knowledge_rag.py` |
| Graph 工具             | `multisource_data_agent/application/agents/tool_execution/graph_tool.py`    |
| 安全验证记录           | `docs/SECURITY_RELEASE.md`                                                  |
