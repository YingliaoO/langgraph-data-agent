# Multi-Source Data Agent 当前部署说明

## 1. 部署目标

当前默认部署只围绕 Multi-Source Data Agent 主线：

```text
backend
mysql
neo4j
kb_postgres
kb_ingest
```

Milvus、Redis、LightRAG、Vision/Image 不应重新加入默认 Compose。

## 2. 系统要求

推荐开发环境：

- Docker Desktop / Docker Engine + Compose
- Python 3.12 对应项目 `.venv`
- Node.js / npm（仅 Web Console）
- Git

Windows PowerShell 是当前常用验证环境。

## 3. 环境配置

复制环境配置模板：

```powershell
Copy-Item .env.example .env
```

根据当前 `settings.py` / `.env.example` 填写：

- LLM OpenAI-compatible endpoint
- Embedding endpoint
- Reranker endpoint
- MySQL
- Neo4j
- PostgreSQL/PGVector

不要提交真实 `.env`。

从旧版配置升级时，请将项目专属变量改名：`GUSTOBOT_DEBUG` → `MULTISOURCE_AGENT_DEBUG`，`GUSTOBOT_MEMORY_TURNS` → `MULTISOURCE_AGENT_MEMORY_TURNS`。可选的 CLI 兼容参数 `GUSTOBOT_MAX_MEMORY_TURNS` 和测试地址 `GUSTOBOT_BASE_URL` 分别改为 `MULTISOURCE_AGENT_MAX_MEMORY_TURNS`、`MULTISOURCE_AGENT_BASE_URL`。`DEBUG`、`LLM_BASE_URL` 等通用变量无需改名。默认 SQLite 文件名保留 `data/gustobot.db` 以兼容已有数据，不执行隐式数据库迁移。

### 修改 `.env` 后让配置生效

Compose 的 `environment` 在创建容器时注入。修改主机 `.env` 或执行 `docker compose restart backend`，不会更新已有容器的这部分环境变量；应用读取挂载的 `.env` 时，容器中旧环境变量还可能优先于新文件。

确认 `APP_DATABASE_URL` 与 `BUSINESS_DATABASE_URL` 分别指向正确的应用/业务数据库及独立账号后，重建后端容器：

```powershell
docker compose config --quiet
docker compose up -d --no-deps --force-recreate backend
curl.exe -s http://localhost:8000/ready
```

若修改的是 `KB_EMBEDDING_*`、`KB_LLM_*` 等知识服务配置，还需运行 `docker compose up -d --no-deps --force-recreate kb_ingest`，再检查 `http://localhost:8100/health` 及真实检索。只重建 backend 不会更新 kb_ingest 容器中的旧环境变量。

若同时修改了依赖或 Dockerfile，先运行 `docker compose build backend`。上述操作保留 MySQL、Neo4j 和 PGVector 的现有数据卷，不需要执行 `down -v`、重新导入数据或重置数据库密码。不要把包含凭据的完整 `docker compose config` 输出贴到公开日志中。

RAG 接受阈值更新：当前 PGVector 的默认 `KB_POSTGRES_RERANK_THRESHOLD=0.75`，依据[独立开发组校准](RAG_EVALUATION.md#接受阈值校准与误拒答回归)。沿用旧 `.env` 中 `0.8` 的用户需显式改为 `0.75`，再按上面命令重建 backend 容器；只拉取源码或 restart 不会覆盖旧环境值。cosine 门槛仍为 `KB_POSTGRES_SIMILARITY_THRESHOLD=0.5`。旧 `KB_RERANK_SCORE_THRESHOLD` 不控制 canonical PGVector 的这一门槛；不要将两者混淆或按单题结果随意调低。

## 4. 启动核心服务

```powershell
docker compose up -d
```

查看：

```powershell
docker compose ps
```

预期服务：

```text
backend
mysql
neo4j
kb_postgres
kb_ingest
```

## 5. 健康检查

### Backend liveness

```powershell
curl.exe -s http://localhost:8000/health
```

### Backend readiness

```powershell
curl.exe -s http://localhost:8000/ready
```

`/ready` 应检查关键依赖，例如：

```text
database
neo4j
knowledge_service
llm_config
```

`llm_config=ok` 表示已配置模型密钥，不会向外部模型服务发起付费认证请求。因此 `/ready` 为 200 时，真实聊天仍可能收到提供方的 401 `invalid_api_key` 或 404 `model_not_found`。前者需要在本机检查密钥与服务地址是否匹配；后者需要确认 `LLM_MODEL` 是该服务及账号实际可用的模型。更新 `.env` 后重建后端容器，再用真实聊天请求验证，切勿将密钥贴入日志或聊天。

### kb_ingest

```powershell
curl.exe -s http://localhost:8100/health
```

## 6. Enterprise Retail 初始化

```powershell
.\.venv\Scripts\python.exe .\demo\enterprise_retail\bootstrap.py
```

初始化：

- MySQL retail tables
- Neo4j retail graph
- Enterprise Retail knowledge dataset

公开快照不包含 Recipe 初始化资产；bootstrap 只写入 Enterprise Retail 合成数据。

## 7. RAG startup migration

`kb_ingest` 启动时会确保 document identity schema：

```text
(domain,dataset_id,source_id)
```

已有 legacy 表时，startup migration 可进行：

- 列回填
- duplicate cleanup
- 联合唯一索引迁移

原文与检索索引的增量列 `source_content`、`document_meta`、`search_vector` 及 GIN 索引同样在 startup 初始化。旧记录的原始正文、embedding 与身份保持不变；无法确认原文的内容标记为 `legacy_unverified`，不是自动恢复为可信原文。升级 kb_ingest 源码后需构建并重建该服务，不能仅 restart 旧镜像。

正常 `/search` 请求不应再执行重型迁移。

如果 migration 失败，推荐让服务启动失败并排查，而不是让每次 search 尝试修 schema。

## 8. Web Console

开发：

```powershell
npm --prefix .\web ci
npm --prefix .\web run dev
```

构建：

```powershell
npm --prefix .\web run build
```

Web 不属于默认五服务 backend/data compose。

## 9. 运行测试

必须使用项目 `.venv`，避免系统 Anaconda/Python 包 ABI 污染。

```powershell
$env:DEBUG='false'
.\.venv\Scripts\python.exe -m pytest -q
```

以当前环境实际输出与 CI 状态为准，不用历史数量作为当前通过证明。

如果误用系统 Python 出现 NumPy/Pandas ABI 或缺包问题，先确认解释器：

```powershell
.\.venv\Scripts\python.exe -c "import sys; print(sys.executable)"
```

## 10. Enterprise Smoke

```powershell
.\.venv\Scripts\python.exe -u .\demo\enterprise_retail\smoke.py
```

Smoke 覆盖六项检查。应以当前环境的逐项输出判定通过情况；HTTP 200、文件提交确认或单元测试通过都不能代替完整 Smoke 验收。运行方法见[快速启动](QUICK_START.md)。

## 11. 自定义 HTTP Eval

示例：

```powershell
.\.venv\Scripts\python.exe eval/run_eval.py `
  --dataset ./my-eval/cases.jsonl `
  --base-url http://localhost:8000 `
  --output-dir ./eval/results/custom `
  --timeout 600 `
  --retry-count 0
```

先自行准备 JSONL，字段见[评测入口](../eval/README.md)。内部 Benchmark 不随公开版本分发。报告应记录实际模型、timeout/retry 配置及失败结果。配置不同的运行应单独报告，避免混合比较。

## 12. 日志排查

### Backend

```powershell
docker compose logs -f backend
```

### kb_ingest

```powershell
docker compose logs -f kb_ingest
```

### MySQL

```powershell
docker compose logs -f mysql
```

### Neo4j

```powershell
docker compose logs -f neo4j
```

### PostgreSQL

```powershell
docker compose logs -f kb_postgres
```

## 13. 常见问题

### `/health` 正常但查询失败

检查 `/ready`。liveness 只说明 FastAPI 进程活着。

### RAG 查询返回 Recipe 文档

当前 Enterprise path 应带 `domain=enterprise_retail`。检查：

- KnowledgeRAGTool context
- search request filter
- searchable_documents domain

### 重复上传 weekly_campaign 出现重复数据

当前稳定 identity 应基于：

```text
(domain,dataset_id,source_id)
```

检查是否绕过 canonical ingestion API 或 dataset_id 不稳定。

### 第一次 search 很慢并出现 DDL

当前 migration 应在 startup，不应在 search path。若出现，说明运行容器可能仍加载旧代码。

### Agent 一直很慢

先区分：

```text
Router
Planner
Tool
AnswerGenerator
```

查看 trace/timings。不要直接把所有 timeout 放大。

## 14. 生产化前仍缺少的内容

当前部署不是生产级：

- Auth/RBAC
- tenant isolation
- persistent LangGraph checkpointer
- ingestion job lifecycle
- autoscaling/HA
- secret manager
- monitoring/alerting

这些能力需要独立的实现与验证，当前本地演示不具备上述生产保证。


## 生产部署覆盖模板

默认 `docker-compose.yml` **仅供本地开发/演示**：含热重载、源码挂载、演示数据库口令和关闭的 Neo4j 鉴权。宿主机端口绑定回环地址并不能代替应用认证。应用尚无生产级 Auth/RBAC，因此即使应用下面的模板，也只能在可信网络或具备认证、TLS 和访问控制的反向代理后部署，不能直接向公网提供匿名访问。

为保持公开快照文件范围不变，覆盖模板放在此处。将下列 YAML 保存到仓库外的 `compose.production.yml`。要求 Docker Compose **2.24.4 或更新版本**（使用 `!override` / `!reset`）。它与基础配置合并使用，不可单独启动。

<!-- production-compose-override:start -->
```yaml
services:
  backend:
    environment:
      DEBUG: "false"
      APP_DATABASE_URL: ${PROD_APP_DATABASE_URL:?Set the application database URL}
      BUSINESS_DATABASE_URL: ${PROD_BUSINESS_DATABASE_URL:?Set the read-only business database URL}
      AUTH_JWT_SECRET: ${AUTH_JWT_SECRET:?Set a random JWT signing secret}
      KNOWLEDGE_SERVICE_TOKEN: ${KNOWLEDGE_SERVICE_TOKEN:?Set a separate random internal service token}
      MYSQL_ROOT_PASSWORD: ""
      NEO4J_PASSWORD: ${PROD_NEO4J_PASSWORD:?Set a unique production password}
    command: ["uvicorn", "multisource_data_agent.main:application", "--host", "0.0.0.0", "--port", "8000"]
    # No source, .env, or host database bind mount. Existing DB volume keys stay unchanged.
    volumes: !override
      - type: bind
        source: ${PROD_RUNTIME_DIR:?Set an existing absolute runtime directory}
        target: /app/data
      - type: bind
        source: ${PROD_UPLOAD_DIR:?Set an existing absolute uploads directory}
        target: /app/uploads
    ports: !override
      - "127.0.0.1:8000:8000"
  neo4j:
    environment:
      NEO4J_AUTH: neo4j/${PROD_NEO4J_PASSWORD:?Set a unique production password}
      NEO4J_dbms_security_auth__enabled: "true"
      NEO4J_dbms_security_procedures_allowlist: "apoc.meta.data,apoc.schema.nodes"
      NEO4J_dbms_security_procedures_unrestricted: "apoc.meta.data,apoc.schema.nodes"
    ports: !reset []
  mysql:
    environment:
      MYSQL_ROOT_PASSWORD: ${PROD_MYSQL_ROOT_PASSWORD:?Set a distinct root password}
      MYSQL_PASSWORD: ${PROD_MYSQL_PASSWORD:?Set a unique production seed account password}
      APP_DB_PASSWORD: ${PROD_APP_DB_PASSWORD:?Set an application account password}
      BUSINESS_DB_PASSWORD: ${PROD_BUSINESS_DB_PASSWORD:?Set a read-only account password}
    ports: !reset []
  kb_postgres:
    environment:
      POSTGRES_PASSWORD: ${PROD_PG_PASSWORD:?Set a unique production password}
    ports: !reset []
  kb_ingest:
    env_file: !reset []
    environment:
      PGPASSWORD: ${PROD_PG_PASSWORD:?Set a unique production password}
      KNOWLEDGE_SERVICE_TOKEN: ${KNOWLEDGE_SERVICE_TOKEN:?Set a separate random internal service token}
    # Use the entrypoint baked into the image, not a host script.
    volumes: !override
      - type: bind
        source: ${PROD_INGEST_DATA_DIR:?Set an existing absolute ingestion directory}
        target: /app/data
    ports: !reset []
```
<!-- production-compose-override:end -->

在仓库外创建生产环境变量文件，并限制文件访问权限。沿用 `.env.example` 中需要的模型、数据库名/用户名等配置，但**不能沿用演示密码**。另需显式提供：

- `PROD_MYSQL_ROOT_PASSWORD`、`PROD_MYSQL_PASSWORD`、`PROD_NEO4J_PASSWORD`、`PROD_PG_PASSWORD`：分别生成强随机密码，不提供默认值；不要复用生产凭据，也不要提交该文件。
- `PROD_APP_DATABASE_URL`、`PROD_BUSINESS_DATABASE_URL`：分别使用独立应用账户和只读业务账户；密码对应 `PROD_APP_DB_PASSWORD`、`PROD_BUSINESS_DB_PASSWORD`，两套账号不得共用。主机为 `mysql:3306`；特殊字符须 URL 编码，禁止使用 root 账户。
- `NEO4J_USER=neo4j`、`NEO4J_URI=bolt://neo4j:7687`；`KB_PGHOST=kb_postgres`、`KB_PGPORT=5432`。服务之间使用容器网络地址。
- `PROD_RUNTIME_DIR`、`PROD_UPLOAD_DIR`、`PROD_INGEST_DATA_DIR`：预先创建、限制权限的**绝对路径**。需要已有 SQLite 数据时，运行目录必须包含原数据库文件，默认文件名仍是 `gustobot.db`；不自动迁移或重命名。
- `CORS_ORIGINS`：实际前端来源；它不是用户认证机制。生产的模型密钥通过外部文件/环境注入，不写入镜像。

先执行配置校验，**不要输出包含密码的完整渲染配置**：

```powershell
$prodEnv = 'D:\deploy\agent.production.env' # 使用自己的仓库外路径
$prodOverride = 'D:\deploy\compose.production.yml'
docker compose --env-file $prodEnv -f docker-compose.yml -f $prodOverride config --quiet
# 仅在完成网络隔离、网关认证、数据备份和凭据校验后，由部署者手动启动：
docker compose --env-file $prodEnv -f docker-compose.yml -f $prodOverride up -d --build
```

Linux/macOS 使用相应的绝对路径与 shell 变量。不要同时使用基础配置的启动命令而漏掉覆盖文件。生产配置通过 `!reset` 去除数据服务的宿主机端口和知识服务的 `.env` 加载，通过 `!override` 去除后端源码挂载；仍保留原有数据库卷键和五个服务，不自动操作已有数据。

**已有数据卷的注意事项：** 设置新的 MySQL/PostgreSQL/Neo4j 环境变量不会自动更改已初始化数据库中的密码。应先备份，由管理员在数据库中更新账户密码，再协调应用凭据切换；不能用 `down -v` 代替密码轮换，也不能把原开发卷直接当成已完成生产加固的数据。生产部署使用独立的 Compose project name 时会得到独立卷；切换前必须明确数据迁移/备份计划，不要意外切换原部署的 project name。

按当前 `langchain-neo4j==0.3.0` 的默认 schema 刷新路径，仅允许 `apoc.meta.data` 和 `apoc.schema.nodes`，两者均需获得 unrestricted 权限；`apoc.schema.nodes` 在 Neo4j 5.18 的沙箱内不可用。当前主链未启用 enhanced schema。上线前在目标 Neo4j/APOC 版本上验证 schema 刷新与图查询；如确有权限错误，核对实际需要的过程，不恢复 `apoc.*`。API 已实现 JWT subject/Session owner 隔离和独立业务只读账户检查；此模板不实现 TLS、完整 RBAC、业务数据租户/行级权限或自动密码轮换，这些仍由部署方完成。

### 名称与数据兼容

文档中旧的 `GUSTOBOT_` 变量名仅用于解释旧部署如何迁移，当前不承诺自动读取这些历史别名。按前文映射更新配置；`DEBUG`、`LLM_BASE_URL` 等通用变量不改名。`data/gustobot.db` 是保留的兼容文件路径，不是当前展示品牌，也不执行任何隐式数据库重命名。

### 构建依赖来源

前端锁文件使用官方 `registry.npmjs.org`，执行 `npm ci` 保留锁定版本。两个 Python 镜像保留官方基础镜像的 Debian 源，pip 使用 `https://pypi.org/simple`；不注入地区镜像。如果部署网络无法访问，请在本地受控环境配置代理/缓存，不提交包含凭据的镜像配置。


## 安全发布配置与升级

`APP_DATABASE_URL` 仅用于应用持久化。旧 `DATABASE_URL` 仅作为应用配置兼容别名；Text2SQL **不会**回退到它。`BUSINESS_DATABASE_URL` 必须显式配置独立 MySQL 只读账户，且默认库位于 `BUSINESS_ALLOWED_SCHEMAS`；`BUSINESS_ALLOWED_TABLES` 是显式表白名单。SQL 执行前检查实际 `SHOW GRANTS`，拒绝写权限、全库/全局 SELECT、GRANT OPTION 和不明确的角色授权，再设置会话事务只读。Schema 检索和执行使用同一个业务连接。

本地新卷首次初始化时，MySQL 加载公开合成零售 seed，再执行 `scripts/init_mysql_roles.sh`：应用账户只能管理独立 `agent_app` 库，业务账户只对五张零售表有 SELECT 权限。初始化脚本不对已有卷自动执行。已有安装必须由管理员先备份并检查账户/现有表，再显式执行角色脚本；不要删除数据卷。迁移 APP URL 不会自动搬迁旧用户/聊天数据，已有应用数据须单独迁移。历史无 owner 的 Session 不对外开放，也不会自动认领。

`AUTH_JWT_SECRET` 和 `KNOWLEDGE_SERVICE_TOKEN` 使用分别生成的随机值（至少 32 字节），不能使用示例固定值。所有 Chat、Session、History、Upload API 必须提交 `Authorization: Bearer <token>`；验证 HS256 签名、issuer、audience、exp、iat 和 sub，身份只取 sub。没有密钥时受保护接口关闭，不回退为匿名或 default_user。只读 `/health`、`/ready` 保留给健康检查，不能证明外部模型密钥有效。

管理员在可信本机离线签发短期 token：

```powershell
$env:MULTISOURCE_AGENT_ACCESS_TOKEN = python scripts/create_access_token.py --subject demo-operator
```

不要把签名密钥或访问 token 写进 Git、截图、日志、URL、VITE_ 环境变量或前端构建。Vue 控制台在页面中输入短期 Bearer token，仅保存在当前页面内存。没有新增用户注册/登录平台；生产应接入可信身份签发流程、HTTPS 和速率控制。

backend 调用 kb_ingest 时带 `X-Knowledge-Service-Key`，两个服务必须注入相同 `KNOWLEDGE_SERVICE_TOKEN`。知识服务的 `/api` 与 `/api/v1/knowledge` 保护规则一致；不要将该内部密钥交给浏览器。`FILE_UPLOAD_MAX_MB` 同时控制两个服务，每文件分块限制且原始请求体另有固定大小的 multipart 开销预算。

上传返回服务器生成的 `file_id`；Chat 只接受该用户拥有的 file_id，不再接受客户端 server file_path。旧上传响应保留 file_path 展示字段但它不是可提交的路径。上传文件不再匿名静态公开。遗留知识服务的预置 Excel 入口仅接受 `INGEST_UPLOAD_DIR` 内的文件名，不能提交任意绝对路径。

SSE `/api/v1/chat/stream` 提供 progress → final/error → done，是完整生成结果的事件流，不是模型 token streaming。成功最终消息在 final 事件前写入该用户的聊天历史；客户端断开时取消工作，不伪造 final 或继续往断开的连接写 done。
