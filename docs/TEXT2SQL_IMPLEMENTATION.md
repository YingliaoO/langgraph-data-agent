# Multi-Source Data Agent 当前 Text2SQL / SQLAgent 实现说明

## 1. 定位变化

早期 Text2SQL 是一条相对独立的工作流：

```text
模式检索
→ 查询分析
→ SQL 生成
→ SQL 校验
→ SQL 执行
→ 可视化推荐
→ 答案格式化
```

当前这条能力没有被删除，而是被封装进 `sql_agent`，成为 Data Agent 的一个 canonical tool。最大的变化是：

> 从“能够生成并执行 SQL 的小型工作流”，升级为“可被 Router/Planner 统一调度、带安全边界、统一 ToolResult/Evidence、可被 Eval 观测的 SQL 数据工具”。

## 2. 当前外层调用链

```text
User
↓
FastAPI
↓
Semantic Router
↓
data-query
↓
Generic Planner
↓
Task
↓
Tool Selection
↓
sql_agent
↓
Text2SQL internal workflow
↓
ToolResult
↓
SQL-row Evidence
↓
EvidenceContext
↓
AnswerGenerator
↓
Execution DTO
```

Router 只判断 `data-query`，不会直接把顶层 route 定义成 Text2SQL。

## 3. Schema Retrieval

物理 schema 来源是 MySQL `INFORMATION_SCHEMA`，用于获取：

- 表名
- 列名
- 列类型
- 主键
- 外键
- 表注释

业务语义可由静态 domain knowledge 补充，例如：

- 表说明
- 字段业务含义
- 关系事实

正确表述：

> 物理结构由数据库实时 schema 提供，业务语义由领域描述补充。

SQL schema retrieval 不依赖 Neo4j。

## 4. Query Analysis

Query Analysis 先把用户自然语言转成查询意图，而不是一上来写 SQL。

例如：

> “按商品品类统计销售件数，从高到低排序。”

分析可以抽象为：

```text
metric     = SUM(quantity)
dimension  = product category
join       = order_items -> products
group_by   = category
order_by   = units_sold DESC
```

意义：把“理解业务问题”和“编写 SQL”拆开，降低一次 LLM 调用同时承担多个认知任务的错误率。

## 5. SQL Generation

SQL Generation 根据：

```text
用户问题
Query Analysis
当前 schema
领域语义
```

生成候选 SQL。

例如：

```sql
SELECT
    p.category,
    SUM(oi.quantity) AS units_sold
FROM retail_order_items AS oi
JOIN retail_products AS p
  ON oi.product_id = p.product_id
GROUP BY p.category
ORDER BY units_sold DESC
LIMIT 200;
```

候选 SQL **不等于允许执行的 SQL**。

## 6. SQLGlot 语义安全校验

当前使用 SQLGlot 做 AST 级解析和语义检查，不再只依赖字符串黑名单。

作用包括：

- 判断 statement 类型
- 拒绝写操作
- 提取/校验表引用
- 提取/校验字段引用
- 检查语义歧义
- 在执行前建立 deterministic safety boundary

## 7. Read-only Policy

系统只允许只读分析语义。

例如：

```sql
DELETE FROM retail_orders;
```

即使由 LLM 生成，也必须在执行前被拒绝。

因此：

```text
Tool Selection 正确选择 sql_agent
≠
任何 SQL 都可以执行
```

policy rejection 是预期安全结果。

## 8. Table / Column Validation

LLM 可能幻觉不存在的表或字段，例如：

```sql
SELECT revenue FROM retail_sales_summary;
```

如果实际 schema 不存在对应表/字段，SQLAgent 在数据库执行前就应识别问题。

## 9. Ambiguity Validation

当多个表包含同名字段时，例如 `id`、`status`、`created_at`，未限定表别名可能产生语义歧义。

当前 safety 链会尝试在执行前发现这种“语法可能合法、业务指代不清”的问题。

## 10. LIMIT Cap

默认 SQL 查询结果设置确定性上限，例如：

```text
LIMIT 200
```

目的：

- 防止无界结果进入 HTTP payload
- 防止大量 rows 占满 LLM context
- 防止前端一次渲染过量数据

注意：LIMIT 只限制返回行数，不等于限制数据库扫描成本，所以还需要 DB timeout。

## 11. SQL Execution Timeout

SQL 执行具有独立数据库 timeout，用于防止复杂 JOIN 或错误查询长期占用数据库连接。

这与整体 Agent 100s tool budget 是不同边界：

```text
SQL DB timeout
< canonical tool timeout
< whole agent timeout
```

## 12. Controlled Repair

当前 repair 是 bounded：

```text
可恢复生成/执行错误
→ 最多一次 repair
→ 重新生成候选 SQL
→ 全量重新校验
→ 通过后才重新执行
```

不会使用无限 ReAct 重试。

原因：

- 控制 latency
- 控制 token 成本
- 限制错误漂移
- 保证修复后的 SQL 仍经过同一 safety chain

## 13. ToolResult

成功结果概念上：

```json
{
  "tool_name": "sql_agent",
  "tool_kind": "sql",
  "status": "success",
  "statement": {
    "language": "sql",
    "text": "SELECT ..."
  },
  "data": [
    {"total_orders": 12}
  ],
  "evidence": []
}
```

空结果：

```text
status=empty
```

policy/timeout/deterministic error：

```text
status=error
```

## 14. SQL Evidence

SQL row 被标准化为：

```text
kind=sql_row
source=mysql
```

例如：

```json
{
  "evidence_id": "ev_task_1_1",
  "kind": "sql_row",
  "source": "mysql",
  "content": {"total_orders": 12}
}
```

这使最终回答可以追溯到具体数据库事实，而不是只保留 SQL formatter 的一段文字。

## 15. Visualization 的当前地位

早期 Text2SQL workflow 中包含 Visualization Recommendation。当前 Data Agent 的核心职责已经迁移为统一 Evidence/Answer，所以 Visualization 不再是 SQLAgent 对外 contract 的必要部分。

图表可以由 UI 根据结构化 rows 和语义生成，但不能让 SQL 内部 formatter 成为多源请求的最终答案生成器。

## 16. Answer Formatting 的职责迁移

早期：

```text
SQL execution
→ SQL formatter
→ final answer
```

当前：

```text
SQL execution
→ ToolResult
→ Evidence
→ shared AnswerGenerator
→ final answer
```

SQL 内部 formatter 可保留兼容，但 Eval 不应把 tool-local answer 当成最终 AnswerGenerator 成功。

## 17. Trace / Eval

当前 Eval 区分：

```text
route decision
tool selection
tool execution
execution status
evidence availability
fact coverage
fallback
timing
repair count
```

这样可以区分：

- 工具选错
- 工具选对但 SQL 超时
- SQL 被策略拒绝
- SQL 成功但无数据
- SQL 成功且 Evidence 正常

## 18. 相比最初七步流程的总体改进

| 维度 | 最初流程 | 当前流程 |
|---|---|---|
| 外层定位 | 独立 Text2SQL workflow | Data Agent canonical tool |
| Router | Text2SQL 类 route | 统一 `data-query` |
| 安全 | 基础 validation | SQLGlot + read-only + schema safety |
| 结果规模 | formatter 驱动 | LIMIT + bounded public payload |
| 执行时间 | 较弱约束 | DB timeout + Tool/Agent budget |
| 修复 | validation retry | 最多一次受控 repair + revalidate |
| 输出 | SQL 自己格式化答案 | ToolResult/Evidence + shared AnswerGenerator |
| 多源组合 | 不自然 | 可与 RAG/Graph Evidence 融合 |
| 可观测 | 日志为主 | Trace + Execution DTO + Eval |

## 19. 当前不继续做的内容

除非评测证明需要，否则不新增：

- EXPLAIN cost optimizer
- semantic DSL
- query memory
- 无限 ReAct SQL self-correction
- 复杂 cost-based planner

当前 SQLAgent 的目标是“安全、可解释、可验证”，不是实现数据库优化器。
