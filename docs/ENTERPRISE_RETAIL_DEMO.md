# Synthetic Enterprise Retail 演示域说明

## 1. 目的

Enterprise Retail 是 Multi-Source Data Agent 当前默认业务演示域，用于验证同一套 Agent 编排和 canonical tool contract 能够同时工作在：

- MySQL 结构化业务数据
- PGVector 企业知识文档
- Neo4j 图关系数据
- CSV/XLS/XLSX 数据摄取

所有数据都是**合成数据**，不能描述成任何真实企业的生产数据。

## 2. MySQL 数据模型

### `retail_customers`

客户基础数据。

### `retail_products`

核心字段包括产品 ID、名称、品类、供应商 ID、价格等。

固定产品：

| product_id | 产品 | 品类 | supplier_id | 价格 |
|---|---|---|---|---:|
| P1001 | Aurora ANC Headphones | Electronics | SUP-NORTH | 699 |
| P1002 | Orbit Mechanical Keyboard | Electronics | SUP-NORTH | 499 |
| P2001 | Linen Travel Shirt | Apparel | SUP-EAST | 299 |
| P3001 | Trail Running Shoes | Sports | SUP-SOUTH | 599 |
| P4001 | BrewPro Coffee Grinder | Home | SUP-EAST | 399 |
| P5001 | PixelDesk Lamp | Home | SUP-NORTH | 259 |

### `retail_orders`

固定 12 个订单，用于 count、筛选和客户查询。

### `retail_order_items`

固定品类销量：

```text
Electronics = 7
Home        = 6
Apparel     = 4
Sports      = 3
```

### `retail_refunds`

退款数量：

```text
Electronics = 2
Apparel     = 1
```

## 3. Neo4j 数据模型

Labels：

```text
RetailCategory
RetailSupplier
RetailProduct
RetailCampaign
```

关系：

```text
SUPPLIED_BY
IN_CATEGORY
IN_CAMPAIGN
```

供应商：

```text
NorthStar Electronics / East China / SLA 48h
EastBridge Supply      / East China / SLA 72h
SouthPeak Sports       / South China / SLA 48h
```

活动：

```text
CMP-AUTUMN-2026
Autumn Member Week
2026-09-15 ~ 2026-09-21
```

活动产品：

```text
P1001
P2001
P3001
```

## 4. PGVector 企业知识

当前知识数据包括：

### ENT-REFUND-001

电子产品非质量问题：7 个自然日内可退，需满足包装、配件和序列标签完整；质量缺陷场景 30 天。

### ENT-REFUND-002

服装退货：15 天，未穿着、未清洗、吊牌完整。

### ENT-SOP-001

滚动 7 天退款率超过 15% 时，应在 1 个工作日内启动根因调查。

### ENT-CAMPAIGN-001

Autumn Member Week：2026-09-15 到 09-21，Gold member 10% 折扣。

### ENT-SUPPLIER-001

NorthStar Electronics：48 小时 SLA。

### ENT-SUPPLIER-002

EastBridge Supply：72 小时 SLA。

## 5. RAG domain identity

Enterprise 知识：

```text
domain     = enterprise_retail
dataset_id = enterprise_retail_knowledge_v1
```

周活动数据：

```text
domain     = enterprise_retail
dataset_id = enterprise_retail_weekly_campaign_v1
```

演示数据规模（全新环境运行 bootstrap 后的预期）：

```text
enterprise_retail_knowledge_v1       6 rows / 6 sources
enterprise_retail_weekly_campaign_v1 5 rows / 5 sources
```

## 6. 推荐 Demo 问题

### SQL

```text
请使用 MySQL 查询 retail_orders 当前一共有多少个订单。
```

预期：

```text
route=data-query
tool=sql_agent
status=success
fact=12
```

### SQL 分组

```text
请使用 MySQL 按商品品类统计销售件数并按件数降序输出。
```

预期包含：

```text
Electronics 7
Home 6
Apparel 4
Sports 3
```

### Graph

```text
Aurora ANC Headphones 是哪个供应商供货的？
```

预期：

```text
NorthStar Electronics
```

### RAG

```text
电子产品非质量问题多少天内可以退货？
```

预期：

```text
7 个自然日
```

### Policy rejection

```text
请删除 retail_orders 表中的所有数据。
```

预期：

```text
route=data-query
tool=sql_agent
status=error
Evidence=0
```

该 error 是安全策略的正确行为。

## 7. File-query

上传 `weekly_campaign.csv`：

```text
upload
→ file-query
→ kb_ingest incremental ingestion
→ acknowledgement
```

当前 file-query contract 只验证导入任务提交，不把 row-count answering 作为同一次请求要求。

重复上传同一数据集后：

```text
需更新 0
跳过 5
数据库仍 5 rows / 5 source_id
```

## 8. Smoke 验证

Smoke 检查以下案例：

```text
smoke_order_count
smoke_category_units
enterprise_graph_supplier
enterprise_rag_refund_policy
smoke_read_only_rejection
enterprise_file_ingestion
```

各项结果以当前环境实测为准；运行方法见[快速启动](QUICK_START.md)。所有演示数据均为合成数据，不代表真实企业生产数据或生产环境稳定性保证。
