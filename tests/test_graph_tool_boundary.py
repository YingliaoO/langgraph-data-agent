import asyncio
from pathlib import Path
from typing import Any, Dict, List

from multisource_data_agent.application.agents.tool_contracts import Evidence, Statement, ToolResult
from multisource_data_agent.application.agents.tool_execution.graph_tool import (
    GraphTool,
    PredefinedCypherStrategy,
    create_graph_tool_node,
)


def run(coro):
    return asyncio.run(coro)


class FakeStrategy:
    def __init__(self, strategy_name: str, result: ToolResult | None = None, error: Exception | None = None):
        self.strategy_name = strategy_name
        self.result = result
        self.error = error
        self.calls = 0
        self.contexts: List[Dict[str, Any]] = []

    async def execute(self, task: str, *, context: Dict[str, Any]) -> ToolResult:
        self.calls += 1
        self.contexts.append(dict(context))
        if self.error is not None:
            raise self.error
        assert self.result is not None
        return self.result


class FakeGraph:
    def __init__(self, records=None, error: Exception | None = None):
        self.records = list(records or [])
        self.error = error
        self.calls = []

    def query(self, query: str, params=None):
        self.calls.append((query, dict(params or {})))
        if self.error is not None:
            raise self.error
        return list(self.records)


class FakeMatcher:
    def __init__(self, *, match_name: str | None = None, params=None, score: float = 0.9):
        self.match_name = match_name
        self.params = dict(params or {})
        self.score = score

    def match_query(self, question: str, top_k: int = 1):
        if not self.match_name:
            return []
        return [{"query_name": self.match_name, "similarity": self.score}]

    def extract_parameters(self, question: str, query_name: str, llm=None):
        return dict(self.params)


def graph_result(strategy: str, *, status: str = "success", rows=None, error: str | None = None):
    rows = list(rows or [])
    metadata = {"strategy": strategy, "errors": [error] if error else []}
    if error:
        metadata["error"] = error
    return ToolResult(
        tool_name="legacy_graph_backend",
        tool_kind="graph",
        status=status,
        task="query",
        statement=Statement(language="cypher", text="MATCH (n) RETURN n LIMIT 1"),
        data=rows,
        evidence=[Evidence(kind="graph_record", content=row, source="neo4j") for row in rows],
        metadata=metadata,
    )


def test_explicit_predefined_uses_one_semantic_graph_tool():
    predefined = FakeStrategy("predefined", graph_result("predefined", rows=[{"菜名": "宫保鸡丁"}]))
    dynamic = FakeStrategy("text2cypher", graph_result("text2cypher", rows=[{"unused": True}]))
    tool = GraphTool(predefined_strategy=predefined, text2cypher_strategy=dynamic)

    result = run(tool.execute("宫保鸡丁怎么做", {"strategy": "predefined"}))

    assert result.tool_name == "graph_tool"
    assert result.tool_kind == "graph"
    assert result.status == "success"
    assert result.metadata["strategy"] == "predefined"
    assert result.metadata["requested_strategy"] == "predefined"
    assert result.metadata["fallback_used"] is False
    assert predefined.calls == 1
    assert dynamic.calls == 0


def test_auto_falls_back_from_predefined_error_to_text2cypher():
    predefined = FakeStrategy(
        "predefined",
        graph_result("predefined", status="error", error="no template match"),
    )
    dynamic = FakeStrategy(
        "text2cypher",
        graph_result("text2cypher", rows=[{"菜名": "鱼香肉丝"}]),
    )
    tool = GraphTool(predefined_strategy=predefined, text2cypher_strategy=dynamic)

    execution = run(tool.execute_with_details("复杂多跳查询"))

    assert execution.result.status == "success"
    assert execution.strategy == "text2cypher"
    assert execution.result.metadata["fallback_used"] is True
    assert [item["strategy"] for item in execution.attempts] == ["predefined", "text2cypher"]
    assert [item["status"] for item in execution.attempts] == ["error", "success"]
    assert predefined.calls == 1
    assert dynamic.calls == 1


def test_auto_does_not_call_dynamic_after_completed_empty_predefined_query():
    predefined = FakeStrategy("predefined", graph_result("predefined", status="empty"))
    dynamic = FakeStrategy("text2cypher", graph_result("text2cypher", rows=[{"unexpected": True}]))
    tool = GraphTool(predefined_strategy=predefined, text2cypher_strategy=dynamic)

    result = run(tool.execute("不存在的菜", {"strategy": "auto"}))

    assert result.status == "empty"
    assert result.metadata["strategy"] == "predefined"
    assert result.metadata["fallback_used"] is False
    assert dynamic.calls == 0


def test_explicit_text2cypher_does_not_probe_predefined():
    predefined = FakeStrategy("predefined", graph_result("predefined", rows=[{"unused": True}]))
    dynamic = FakeStrategy("text2cypher", graph_result("text2cypher", rows=[{"路径": "A-B-C"}]))
    tool = GraphTool(predefined_strategy=predefined, text2cypher_strategy=dynamic)

    result = run(tool.execute("查询多跳关系", {"strategy": "dynamic"}))

    assert result.status == "success"
    assert result.metadata["strategy"] == "text2cypher"
    assert predefined.calls == 0
    assert dynamic.calls == 1


def test_all_strategy_errors_return_error_with_attempts():
    predefined = FakeStrategy("predefined", error=RuntimeError("template backend failed"))
    dynamic = FakeStrategy("text2cypher", error=RuntimeError("dynamic backend failed"))
    tool = GraphTool(predefined_strategy=predefined, text2cypher_strategy=dynamic)

    result = run(tool.execute("查询关系"))

    assert result.status == "error"
    assert result.metadata["fallback_used"] is True
    assert result.metadata["strategy"] == "text2cypher"
    assert len(result.metadata["attempts"]) == 2
    assert "template backend failed" in result.metadata["error"]
    assert "dynamic backend failed" in result.metadata["error"]


def test_predefined_strategy_executes_parameterized_template_and_evidence():
    graph = FakeGraph(records=[{"菜名": "宫保鸡丁", "口味": ["香辣"]}])
    matcher = FakeMatcher(match_name="dish_flavor", params={"dish_name": "宫保鸡丁"})
    strategy = PredefinedCypherStrategy(
        graph=graph,
        predefined_cypher_dict={
            "dish_flavor": "MATCH (d:Dish {name: $dish_name}) RETURN d.name AS 菜名"
        },
        matcher=matcher,
        llm=object(),
    )

    result = run(strategy.execute("宫保鸡丁什么口味", context={}))

    assert result.status == "success"
    assert result.statement is not None
    assert result.statement.parameters == {"dish_name": "宫保鸡丁"}
    assert graph.calls == [
        (
            "MATCH (d:Dish {name: $dish_name}) RETURN d.name AS 菜名",
            {"dish_name": "宫保鸡丁"},
        )
    ]
    assert result.evidence[0].source == "neo4j"
    assert result.evidence[0].metadata["strategy"] == "predefined"


def test_compatibility_node_preserves_old_route_label_but_returns_graph_tool_result():
    predefined = FakeStrategy("predefined", graph_result("predefined", rows=[{"主食材": "鸡肉"}]))
    dynamic = FakeStrategy("text2cypher", graph_result("text2cypher", rows=[]))
    tool = GraphTool(predefined_strategy=predefined, text2cypher_strategy=dynamic)
    node = create_graph_tool_node(
        tool=tool,
        strategy="predefined",
        legacy_node_name="predefined_cypher",
    )

    state = run(node({
        "task": "宫保鸡丁主料是什么",
        "query_name": "predefined_cypher",
        "query_parameters": {"query": "main_ingredients_of_dish", "parameters": {"dish_name": "宫保鸡丁"}},
        "steps": ["tool_selection"],
        "eval_trace": None,
    }))

    assert node.__name__ == "predefined_cypher"
    assert len(state["tool_results"]) == 1
    assert state["tool_results"][0].tool_name == "graph_tool"
    assert state["tool_results"][0].metadata["strategy"] == "predefined"
    assert state["steps"] == ["execute_predefined_cypher"]
    assert state["cyphers"][0]["records"] == [{"主食材": "鸡肉"}]


def test_main_workflow_uses_graph_tool_boundary_not_legacy_graph_nodes():
    source = Path(
        "multisource_data_agent/application/agents/kg_sub_graph/agentic_rag_agents/workflows/multi_agent/multi_tool.py"
    ).read_text(encoding="utf-8")

    assert "create_cypher_query_node" not in source
    assert "create_predefined_cypher_node" not in source
    assert "GraphTool(" in source

    # Phase 2 no longer mounts two compatibility GraphTool nodes.  The main
    # workflow exposes exactly the three canonical execution nodes through the
    # UnifiedToolExecutor; GraphTool owns predefined/Text2Cypher strategy
    # selection behind the single ``graph_tool`` node.
    assert "create_default_tool_executor" in source
    assert source.count("create_unified_tool_node(") == 3
    assert 'create_unified_tool_node(core_executor, "sql_agent")' in source
    assert 'create_unified_tool_node(core_executor, "knowledge_rag")' in source
    assert 'create_unified_tool_node(core_executor, "graph_tool")' in source
    assert 'main_graph_builder.add_node("graph_tool", graph_tool)' in source
    assert 'main_graph_builder.add_node("cypher_query"' not in source
    assert 'main_graph_builder.add_node("predefined_cypher"' not in source


def test_dynamic_strategy_rechecks_read_only_policy_after_correction():
    source = Path("multisource_data_agent/application/agents/tool_execution/graph_tool.py").read_text(encoding="utf-8")
    assert "validate_no_writes_in_cypher_query(executed_statement)" in source
    assert '"reason": "write_clause_rejected"' in source


def test_eval_recognizes_semantic_graph_tool():
    source = Path("eval/evaluators/metrics.py").read_text(encoding="utf-8")
    assert '"graph_tool"' in source


def test_validate_cypher_output_normalizes_openai_compatible_string_lists():
    import importlib.util

    module_path = Path(
        "multisource_data_agent/application/agents/kg_sub_graph/agentic_rag_agents/components/text2cypher/validation/models.py"
    )
    spec = importlib.util.spec_from_file_location("graph_validation_models_for_test", module_path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    ValidateCypherOutput = module.ValidateCypherOutput

    output = ValidateCypherOutput.model_validate({
        "errors": "None",
        "filters": '[{"node_label":"Dish","property_key":"name","property_value":"宫保鸡丁"}]',
    })

    assert output.errors is None
    assert output.filters is not None
    assert len(output.filters) == 1
    assert output.filters[0].node_label == "Dish"
    assert output.filters[0].property_key == "name"
    assert output.filters[0].property_value == "宫保鸡丁"


def test_dynamic_execution_treats_legacy_validation_errors_as_warnings_after_success():
    source = Path("multisource_data_agent/application/agents/tool_execution/graph_tool.py").read_text(encoding="utf-8")

    assert '"validation_warnings": validation_warnings' in source
    assert 'status = "success" if records else "empty" if cyphers else "error"' in source
    assert '"errors": []' in source


def test_cypher_tools_validator_uses_canonical_normalizing_model():
    import importlib.util

    module_path = Path(
        "multisource_data_agent/application/agents/kg_sub_graph/agentic_rag_agents/components/cypher_tools/utils.py"
    )
    source = module_path.read_text(encoding="utf-8")
    assert "class ValidateCypherOutput" not in source
    assert "components.text2cypher.validation.models import" in source

    models_path = Path(
        "multisource_data_agent/application/agents/kg_sub_graph/agentic_rag_agents/components/text2cypher/validation/models.py"
    )
    spec = importlib.util.spec_from_file_location("graph_validation_models_canonical", models_path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    output = module.ValidateCypherOutput.model_validate({
        "errors": "None",
        "filters": '[{"node_label":"Dish","property_key":"name","property_value":"宫保鸡丁"}]',
    })
    assert output.errors is None
    assert output.filters and output.filters[0].property_value == "宫保鸡丁"
