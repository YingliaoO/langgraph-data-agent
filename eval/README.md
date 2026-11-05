# Evaluation Runner

[方法与指标](../docs/EVALUATION.md) · [RAG 方法](../docs/RAG_EVALUATION.md)

公开部分提供通用 Runner、评分器与工程测试。内部数据集、专项语料、实验报告和原始档案不随公开版本分发。

## 提供自己的 JSONL

每行是一道题及事先声明的检查。下面仅说明字段格式，不是随仓库发布的数据集或测量结果；问题和事实须根据自己的源数据填写：

```json
{"id":"your_case","query":"查询已接入数据中的记录总数","expected_route":"data-query","expected_tools":["sql_agent"],"expected_execution_status":"success","expected_evidence_available":true,"expected_key_facts":["由独立源数据确定的预期事实"],"expected_fallback":false}
```

可选 fact_assertions 检查来源、数值、同行字段或完整集合。可选 expected_error_categories 限定预期错误。规则以 [load_cases](run_eval.py)、[声明断言](evaluators/fact_assertions.py)和 [RAG schema](evaluators/rag_quality.py) 为准。

## Chat HTTP

先按[快速启动](../docs/QUICK_START.md)配置服务，并自备 ./my-eval/cases.jsonl。以下为操作示例，不是已执行结果：

```powershell
python -u eval/run_eval.py --dataset ./my-eval/cases.jsonl --base-url http://localhost:8000 --output-dir ./eval/results/custom --timeout 600 --retry-count 0 --fail-on-regression
```

必须显式传 --dataset；源码保留的历史默认路径所指文件不随公开版本分发。Runner 参数不修改服务端预算或 SDK 配置。真实请求可能产生模型费用，失败和慢样本也应保留。

## Retrieval 与 Acceptance

retrieval 直接请求知识服务，不经过完整 Agent。显式提供自己的 --dataset 与 --corpus，先通过现有摄取接口将原文与稳定身份写入隔离知识域；完整性不符应停止，不能对空库产生成绩。

```powershell
python -u eval/run_eval.py --mode retrieval --dataset ./my-eval/rag_cases.jsonl --corpus ./my-eval/corpus.jsonl --retrieval-base-url http://localhost:8100 --output-dir ./eval/results/custom-retrieval --candidate-k 10 --top-k 3 --compare-hybrid --timeout 120 --retry-count 0
```

RAG case 需要 expected_sources 与 rag_eval：domain、dataset_id、answerable、split（dev/test）、http_compatible。无答案题另声明拒答短语，不强行固定检索状态。corpus 提供 domain、dataset_id、source_id、title、content，可选版本与适用日期。初始化内部专项的脚本不随公开版本分发。

acceptance 模式支持使用独立开发输入校准接受门槛，再检查留出回归。也须显式传自己的 --dataset 与 --corpus，参数以 `python eval/run_eval.py --help` 为准。分数不是答案概率，不能用测试组反向调阈值。

## 结果与工程检查

Runner 保存逐题结果、汇总、环境、报告和源码快照。默认结果目录被 Git 忽略，自选目录需自行忽略；产物可能包含问题、回答及来源原文，不要直接公开。

--review-run 只对保存结果复核门槛，不发 HTTP，也不重新执行事实评分。Golden 修正后的事实重评应另存并标明复用执行，不能冒充新 Benchmark。[compare_runs.py](compare_runs.py)只比较满足输入与版本条件的已有运行，不调用模型。

```powershell
$env:DEBUG='false'
python -m pytest -q
```

公开工程测试与依赖内部资料的本地专项分开。工程测试不提供内部成绩，也不能证明模型回答质量。CI 状态以 GitHub Actions 为准。
