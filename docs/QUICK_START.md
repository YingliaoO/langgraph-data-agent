# 快速启动

默认场景为 Synthetic Enterprise Retail。以下在仓库根目录执行，需要 Python 3.10–3.12（推荐 3.12）、Docker Compose；前端需要 Node.js/npm。

## 安装与配置

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
python -m pip check
Copy-Item .env.example .env
```

编辑 `.env`，填写有效的 LLM、Embedding、Reranker 和数据库配置。模型调用可能收费；不要提交密钥。若已存在 `.env`，保留自己的配置并对照模板补齐，不直接覆盖。

Linux/macOS 使用 `source .venv/bin/activate` 和 `cp .env.example .env`。依赖以 `pyproject.toml` 为主，`requirements.txt` 安装当前项目及测试所需 extra。

## 启动与检查

```powershell
docker compose up -d --build
docker compose ps
curl.exe -s http://localhost:8000/health
curl.exe -s http://localhost:8000/ready
curl.exe -s http://localhost:8100/health
```

Compose 提供 backend、mysql、neo4j、kb_postgres、kb_ingest 五个服务，Vue 独立启动。端口默认只绑定本机；热重载、演示口令与 Neo4j 鉴权设置只适合本地开发。DEBUG 默认关闭；本地调试时可在 `.env` 显式设置 `DEBUG=True` 和 `MULTISOURCE_AGENT_DEBUG=True`。生产部署必须使用[安全覆盖配置](DEPLOYMENT.md#生产部署覆盖模板)，不能直接使用默认 Compose。

`/health` 只表示进程存活；`/ready` 检查关键依赖和模型配置是否存在，不证明模型账号认证通过或任意查询成功。配置变更与服务排查见[部署说明](DEPLOYMENT.md)。

## 初始化合成数据

先阅读[数据说明](ENTERPRISE_RETAIL_DEMO.md)。初始化脚本会写入 MySQL 零售表、Neo4j 零售图和 PGVector 知识集。首次使用建议采用隔离的本地演示环境；已有数据环境应先备份并检查脚本，不当成只读健康检查。

```powershell
python .\demo\enterprise_retail\bootstrap.py
python -u .\demo\enterprise_retail\smoke.py
```

Smoke 检查订单数、品类销量、供应商、退货政策、SQL 写入拒绝和文件摄取提交六项。目标为六项符合预期，实际是否通过以本次输出为准。

## 在控制台提问

```powershell
npm --prefix .\web ci
npm --prefix .\web run dev
```

在终端显示的本地地址打开 Vue 页面。依次尝试：

| 问题 | seed 中可核对的预期事实 |
| --- | --- |
| 请使用 MySQL 查询 retail_orders 当前一共有多少个订单。 | 12 个订单 |
| 按商品品类统计销售件数，从高到低列出。 | Electronics 7、Home 6、Apparel 4、Sports 3 |
| Aurora ANC Headphones 由哪个供应商供货？ | NorthStar Electronics |
| 电子产品没有质量问题，收货后多久可以申请退货？ | 7 个自然日，并保留原文条件 |

这些是预期事实，不是新一轮实测成绩。查看回答旁的工具、SQL/Cypher、Evidence、引用与耗时；真实历史截图见[界面与示例](SHOWCASE.md)。

## 直接调用 Chat API

PowerShell 将 JSON 编码为 UTF-8 字节，避免中文与命令行引号解析问题：

```powershell
$payload = @{
    message = "请使用 MySQL 查询 retail_orders 当前一共有多少个订单。"
    session_id = "quickstart-$([guid]::NewGuid().ToString('N'))"
} | ConvertTo-Json -Compress
$body = [System.Text.Encoding]::UTF8.GetBytes($payload)
$response = Invoke-RestMethod -Uri "http://localhost:8000/api/v1/chat/" -Method Post -ContentType "application/json; charset=utf-8" -Body $body -TimeoutSec 600
$response | ConvertTo-Json -Depth 20
```

核对 `route`、`message`、`execution.status`、`execution.tool_results` 和 `execution.evidence`。HTTP 200 不代表任务成功；客户端等待上限也不会改变服务端预算。接口定义见[API 契约](API_CONTRACT.md)及本地 `http://localhost:8000/docs`。

## 安全与文件摄取检查

在隔离演示环境测试“请删除 retail_orders 表中的所有数据”，预期为策略拒绝、SQL 工具 error 且无 Evidence。普通超时或任意 error 不能当作正确拒绝。

前端支持 CSV/XLSX/XLS，可使用 `demo/enterprise_retail/weekly_campaign.csv`。file-query success 表示提交后台摄取任务，不保证向量化已完成；需要查看知识服务状态并在完成后另行检索。重复上传的身份与去重语义见[数据说明](ENTERPRISE_RETAIL_DEMO.md)。

## 工程检查与 Eval

```powershell
$env:DEBUG='false'
python -m pytest -q
npm --prefix .\web run lint
npm --prefix .\web run build
```

真实 HTTP Eval 的命令、计费及分母区别见[评测入口](../eval/README.md)。以本机当前环境的实际输出为准，不能以其他运行代替验证。

遇到慢请求先查看阶段耗时，区分规划、工具、模型生成与答案汇总；不要仅凭总耗时归因数据库。模型配置修改后需按[部署说明](DEPLOYMENT.md)重建相应服务容器。
