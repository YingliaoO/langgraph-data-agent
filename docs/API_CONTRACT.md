# Multi-Source Data Agent API 契约说明

## 1. 设计目标

API 契约的目标不是把内部 LangGraph State 原样暴露给客户端，而是提供一个稳定、版本化、可供 UI 和 Eval 长期消费的结构化执行结果。

Chat API 的响应可以概括为：

```json
{
  "message": "自然语言答案",
  "session_id": "...",
  "message_id": "...",
  "route": "data-query",
  "route_logic": "...",
  "sources": [],
  "execution": {},
  "metadata": {}
}
```

客户端应把 `execution` 作为正式执行契约；`metadata.eval_trace`、`metadata.agent_state` 等属于兼容或调试字段。

## 2. 主要接口边界

当前对话主入口：

```text
POST /api/v1/chat/
```

路由能力查询：

```text
GET /api/v1/chat/routes
```

运行状态：

```text
GET /health
GET /ready
```

文件上传入口由 backend upload API 负责；知识摄取与检索由 `kb_ingest` 内部 API 负责。`kb_ingest` 是内部数据服务，不建议让前端绕过 Chat/Upload 主边界直接依赖其底层检索结构。

## 3. Execution DTO v1

```json
{
  "contract_version": "1",
  "status": "success",
  "tasks": [],
  "tool_results": [],
  "evidence": [],
  "citation_ids": [],
  "timings": {},
  "fallback_reason": "none"
}
```

### 3.1 `contract_version`

当前版本：

```text
1
```

前端和 Eval 应保留版本判断能力，不能假设未来所有字段永远不变。

### 3.2 请求级 `status`

允许：

```text
success
empty
partial
error
```

聚合语义：

| 子工具结果 | 请求级 status |
|---|---|
| success only | `success` |
| success + empty | `success` |
| empty only | `empty` |
| error only | `error` |
| error + empty | `error` |
| success + error | `partial` |

`partial` 专门表示部分工具成功、部分工具失败，不用于普通空结果。

## 4. `tasks`

任务来自 Generic Planner。

```json
{
  "task_id": "task_1",
  "question": "统计 retail_orders 当前订单总数",
  "tool_hint": "auto",
  "depends_on": []
}
```

字段：

- `task_id`：请求内稳定任务 ID。
- `question`：任务的明确数据问题。
- `tool_hint`：通常为 `auto`，允许上层表达有限提示。
- `depends_on`：任务依赖描述；当前不能据此声称已有完整阻塞式 DAG scheduler。

## 5. `tool_results`

单工具 status 只允许：

```text
success
empty
error
```

示例：

```json
{
  "tool_name": "sql_agent",
  "tool_kind": "sql",
  "status": "success",
  "task_id": "task_1",
  "task": "统计 retail_orders 当前订单总数",
  "statement": {
    "language": "sql",
    "text": "SELECT COUNT(order_id) AS total_orders FROM retail_orders LIMIT 200"
  },
  "result_count": 1,
  "rows_preview": [
    {"total_orders": 12}
  ],
  "records_preview": [],
  "documents_preview": [],
  "evidence_ids": ["ev_task_1_1"],
  "error_code": null,
  "error_category": null,
  "repair_count": 0
}
```

不同工具 preview：

| tool_kind | preview 字段 |
|---|---|
| `sql` | `rows_preview` |
| `graph` | `records_preview` |
| `rag` | `documents_preview` |

## 6. `evidence`

Evidence 是最终答案依据，不等同于 ToolResult 原始返回。

示例：

```json
{
  "evidence_id": "ev_task_1_1",
  "kind": "sql_row",
  "source": "mysql",
  "source_id": "mysql:task_1:row:1",
  "content": {"total_orders": 12},
  "score": null,
  "rank": 1,
  "task_id": "task_1",
  "metadata": {}
}
```

Canonical mapping：

```text
sql_agent      → kind=sql_row      source=mysql
knowledge_rag  → kind=document     source=pgvector
graph_tool     → kind=graph_record source=neo4j
```

### 6.1 Evidence identity

三个概念必须区分：

- `evidence_id`：本次执行中的证据 ID，如 `ev_task_1_1`。
- `source_id`：底层稳定来源 ID，如文档 ID 或数据库 row identity。
- `citation_ids`：最终答案声明实际引用了哪些 evidence。

## 7. RAG Evidence metadata

公开 `execution.evidence.metadata` 仅返回经过白名单与限长处理的来源信息，例如：

```text
domain
dataset_id
document_id / document_source_id
document_version
effective_from / effective_to / scope
section_title
chunk_index / chunk_count
start_char / end_char
source_content_hash
content_origin
```

当前 Enterprise Retail RAG 默认通过 `domain=enterprise_retail` 隔离历史 Recipe 文档。

原始 ToolResult/Evidence 内部保留 retrieval/rerank 分数，公开视图中的 `score` 和 `rank` 用于展示，不是回答正确性的概率。`content_origin=original` 表示可恢复的来源原文，`legacy_unverified` 表示无法核验为原文的历史文本。HTTP 文档预览限长，不等于回答层只能使用前 500 字。

知识服务 `/api/v1/knowledge/search` 的可选 `candidate_k`、`retrieval_mode`、`domain/dataset_id` 和 `as_of` 属于检索接口；Chat API 不据此新增自动日期推断或任意知识域切换能力。参数与边界见 [RAG 专项说明](RAG_EVALUATION.md)。

## 8. `citation_ids`

```json
[
  "ev_task_1_1",
  "ev_task_2_1"
]
```

`citation_ids` 必须引用当前响应中真实存在的 `evidence_id`，不能引用未公开 evidence，也不能直接用数据库表名或 source_id 代替。

## 9. `timings`

可能包含：

```json
{
  "total_ms": 52160.39,
  "router_ms": 38003.07,
  "planner_ms": 4051.03,
  "tool_execution_ms": 7478.64,
  "summarize_ms": 2049.48
}
```

字段依赖 trace 是否足够完整，因此客户端必须允许缺省。

`tool_execution_ms` 为已配对终态调用的墙钟跨度；`tool_execution_sum_ms` 为各调用累计观测时间，`tool_completed_sum_ms` 为有完成终态的累计时间。并行累计时间可以大于墙钟跨度，不能相加解释用户等待。缺少终态不伪造耗时。这些是 timings 映射的增量字段，contract_version 仍为 1。

Eval 中还区分：

- completed tool latency：有配对完成终态的执行时间，不等于 ToolResult 状态一定是 success；未完成的超时不计入。
- observed tool duration：包含 timeout/error 在内，实际观察到的等待时间。

避免 timeout case 被平均值静默排除。

## 10. `fallback_reason`

默认：

```text
none
```

可能出现例如：

```text
router_timeout
graph_strategy_fallback
tool_execution_fallback
```

fallback 不是 status。一次请求可以最终 `success`，同时记录发生过 fallback。

## 11. 公共响应边界

Execution DTO 采用 allowlist 和 preview 限制，不应直接暴露完整内部 state。

当前原则：

- rows/records/documents preview 有数量上限。
- RAG 文本内容有长度上限。
- SQL/Cypher statement 有长度上限。
- public evidence 有数量上限。
- `result_count` 保留真实数量，preview 截断不等于数据丢失。

不得暴露：

- API key
- password
- credential
- authorization header
- connection string
- 完整 prompt
- 完整模型 request
- 无界 LangGraph state

## 12. 错误语义

### 12.1 Policy rejection

例如用户请求：

> “删除 retail_orders 全部数据。”

正确结果是：

```text
route = data-query
tool = sql_agent
status = error
error_category = policy / validation
Evidence = []
```

这属于安全策略成功工作，不是 transport failure。

### 12.2 Timeout

Tool timeout 应记录：

```text
status = error
error_category = timeout
completed latency = null
observed duration ≈ timeout budget
```

### 12.3 Empty

“查询不存在的订单”返回 `empty`，不是 error。

### 12.4 Partial

多工具请求中部分成功、部分失败才返回 `partial`。

## 13. Health / readiness

```text
GET /health
```

用于 liveness。

```text
GET /ready
```

用于依赖 readiness。当前典型检查包括：

- database
- Neo4j
- knowledge service
- LLM configuration presence

`/health=200` 不代表所有依赖均可用。

## 14. File-query 契约

文件上传与数据查询是两个阶段：

```text
file upload
→ file-query
→ ingestion submission acknowledgement
→ 后台 kb_ingest 处理
```

当前没有把 file-query success 伪造成 `knowledge_rag` tool execution。上传成功只说明任务已提交，不保证后台完成或立即可检索。


## 身份、上传与事件流（安全发布）

所有 Chat / Session / History / Upload 请求使用 `Authorization: Bearer <short-lived JWT>`。API 验证签名及 issuer/audience/exp/iat/sub；用户身份取 sub。请求里的 user_id 仅作兼容检查，若与身份不同返回 403。访问他人的 Session/History/Upload 返回 404；Session owner 不可修改。首次 Chat 不传 session_id，使用响应中的服务器 ID 继续会话。健康检查不要求用户 token。

上传仍是 `POST /api/v1/upload/file`，返回 file_id；随后 Chat 发送 `{"message":"导入数据集","file_id":"<upload ID>"}`。不接受客户端 server file_path。CSV/XLSX/XLS 的文件和 multipart 请求体均有限额；路径/文件归属在每次访问时检查。

HTTP 错误仅返回安全 `code/category/message/trace_id`（通常位于 detail），校验错误不回显输入。Agent 错误仍保留原有 HTTP execution 契约，但公开诊断不包含内部异常字符串。完整异常只记录服务端。

`POST /api/v1/chat/stream` 是 SSE **进度及最终结果事件**：`progress` 后返回一个 `final` 或 `error`，正常连接以 `done` 结束；每条包含同名 event 和 JSON data.type。final.content 是完整答案，metadata 保留 route/sources/execution/trace_id。不按单词拆分，不宣称 Token Streaming。最终 Assistant 消息先持久化再发送 final。断开连接时传播取消；断开的连接无法保证收到 done，不产生假成功。
