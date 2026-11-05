# 开发说明

## 环境与检查

使用 Python 3.10–3.12，推荐项目虚拟环境中的 Python 3.12。主后端依赖在 `pyproject.toml` 统一维护，`requirements.txt` 安装当前项目及开发/摄取测试 extra；知识服务有独立依赖文件。

```powershell
python -m pip install -r requirements.txt
python -m pip check
$env:DEBUG='false'
python -m pytest -q
npm --prefix .\web ci
npm --prefix .\web run lint
npm --prefix .\web run build
git diff --check
```

服务启动和模型配置见[快速启动](QUICK_START.md)、[部署说明](DEPLOYMENT.md)。自动化检查范围见 [CI](../.github/workflows/ci.yml)。

## 建议阅读顺序

| 层次 | 入口 |
| --- | --- |
| HTTP | [chat.py](../multisource_data_agent/interfaces/http/v1/chat.py) |
| 编排 | [lg_builder.py](../multisource_data_agent/application/agents/lg_builder.py)、[multi_tool.py](../multisource_data_agent/application/agents/kg_sub_graph/agentic_rag_agents/workflows/multi_agent/multi_tool.py) |
| 三工具 | [统一执行器](../multisource_data_agent/application/agents/tool_execution/interface.py)与同目录工具实现 |
| 结果与证据 | [工具契约](../multisource_data_agent/application/agents/tool_contracts.py)、[HTTP 执行契约](../multisource_data_agent/application/agents/execution_contract.py) |
| 数据平面 | [知识服务](../kb_ingest/kb_service)、[合成数据](../demo/enterprise_retail) |
| 验证 | [tests](../tests)、[评测入口](../eval/README.md) |

## 修改时保持的约定

- Router 选择产品路径，Planner 表达证据任务，Tool Selection 面向三个领域工具。内部后端与图查询策略不扩展为一级工具。
- SQL 修复预算最多一次，候选重新经过完整只读与 Schema 校验；策略拒绝不通过普通重试绕过。
- RAG 显式传播 domain/dataset/date 条件，保持稳定来源身份；重型 schema migration 在知识服务启动阶段执行。
- 新 UI 和 Eval 消费版本化 `execution`，公共预览与内部回答上下文分别控制容量。
- 状态、Evidence、引用与错误类别改变时，维护相应契约与编排测试。具体契约见[架构](ARCHITECTURE.md)和[API](API_CONTRACT.md)。

## 行为验证

确定性测试验证契约，真实模型行为使用自备 JSONL 另跑 HTTP Eval。内部 Benchmark 不随公开版本分发，必须显式传自己的 --dataset，检索模式还需 --corpus。命令见[评测入口](../eval/README.md)。

问题与期望依据固定业务事实。评分规则变更需版本化并补正反例，保存旧原始结果；不要通过适配单次回答改变历史成绩。新增能力由实际需求或失败用例驱动。

## 提交内容

源码、合成产品演示、通用测试和技术说明可公开。内部 Benchmark、专项测试、报告、面试和审查材料只保留本地；凭据、上传、日志和临时文件保持忽略。提交前逐项检查索引，不批量暂存工作区。
