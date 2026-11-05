"""Phase 2 Step 1 tests for canonical three-tool selection and dispatch."""

import asyncio
from typing import Any, Dict, Optional

from langchain_core.messages import AIMessage
from langchain_core.runnables import RunnableLambda
from pydantic import BaseModel

from eval.evaluators.metrics import evaluate_case
from multisource_data_agent.application.agents.kg_sub_graph.agentic_rag_agents.components.guardrails.node import (
    create_guardrails_node,
)
from multisource_data_agent.application.agents.kg_sub_graph.agentic_rag_agents.components.tool_selection.models import (
    get_core_tool_schemas,
)
from multisource_data_agent.application.agents.kg_sub_graph.agentic_rag_agents.components.tool_selection.node import (
    create_tool_selection_node,
)
from multisource_data_agent.application.agents.observability import trace_event
from multisource_data_agent.application.agents.tool_contracts import Evidence, ToolResult
from multisource_data_agent.application.agents.tool_execution.interface import (
    CORE_TOOL_NAMES,
    create_default_tool_executor,
    create_unified_tool_node,
)


# Lightweight historical schemas exercise the compatibility boundary without
# distributing the retired Recipe-specific tool catalogue.
class cypher_query(BaseModel):
    task: str


class predefined_cypher(BaseModel):
    query: str


class microsoft_graphrag_query(BaseModel):
    task: str


class text2sql_query(BaseModel):
    task: str


def run(coro):
    return asyncio.run(coro)


class FakeToolCallingLLM:
    def __init__(self, tool_name: Optional[str], args: Optional[Dict[str, Any]] = None) -> None:
        self.tool_name = tool_name
        self.args = dict(args or {})
        self.invoke_count = 0
        self.bound_names = []

    def bind_tools(self, tools):
        self.bound_names = [schema.model_json_schema().get("title") for schema in tools]

        def respond(_prompt):
            self.invoke_count += 1
            if not self.tool_name:
                return AIMessage(content="")
            return AIMessage(
                content="",
                tool_calls=[
                    {
                        "name": self.tool_name,
                        "args": self.args,
                        "id": "call-test",
                        "type": "tool_call",
                    }
                ],
            )

        return RunnableLambda(respond)


class FakeGuardrailsLLM:
    def __init__(self) -> None:
        self.invoke_count = 0

    def with_structured_output(self, _schema):
        async def unexpected_call(_payload):
            self.invoke_count += 1
            raise AssertionError("explicit SQL source intent should bypass guardrails LLM")

        return RunnableLambda(unexpected_call)


class FakeTool:
    def __init__(self, name: str, kind: str) -> None:
        self.tool_name = name
        self.tool_kind = kind
        self.calls = []

    async def execute(self, task, context=None, *, trace=None):
        self.calls.append((task, dict(context or {}), trace))
        return ToolResult(
            tool_name=self.tool_name,
            tool_kind=self.tool_kind,
            status="success",
            task=task,
            data=[{"value": self.tool_kind}],
            evidence=[Evidence(kind=f"{self.tool_kind}_evidence", content=task)],
        )




class LegacyTraceSQLTool(FakeTool):
    async def execute(self, task, context=None, *, trace=None):
        self.calls.append((task, dict(context or {}), trace))
        trace_event(
            trace,
            "tool_execution",
            "done",
            success=True,
            tool="text2sql_query",
            question=task,
            metadata={"execution_success": True, "status": "success"},
        )
        return ToolResult(
            tool_name="text2sql_query",
            tool_kind="sql",
            status="success",
            task=task,
            data=[{"count": 70}],
            evidence=[Evidence(kind="sql_row", content={"count": 70})],
        )


def _send(command):
    goto = command.goto
    return goto.node, goto.arg


def test_selection_schema_surface_is_exactly_three_canonical_tools():
    schemas = get_core_tool_schemas()
    names = tuple(schema.model_json_schema().get("title") for schema in schemas)

    assert names == CORE_TOOL_NAMES
    assert "customer_tools" not in names
    assert "cypher_query" not in names
    assert "predefined_cypher" not in names
    assert "text2sql_query" not in names


def test_legacy_schema_input_is_replaced_before_binding_to_llm():
    llm = FakeToolCallingLLM("graph_tool")
    node = create_tool_selection_node(
        llm=llm,
        tool_schemas=[
            cypher_query,
            predefined_cypher,
            microsoft_graphrag_query,
            text2sql_query,
        ],
    )

    command = run(
        node(
            {
                "question": "宫保鸡丁和鱼香肉丝有哪些共同食材？",
                "context": {"route_type": "graphrag-query"},
                "eval_trace": {},
            }
        )
    )

    target, payload = _send(command)
    assert llm.bound_names == list(CORE_TOOL_NAMES)
    assert target == "graph_tool"
    assert payload["query_name"] == "graph_tool"


def test_text2sql_route_hint_dispatches_sql_agent_without_keyword_hack_or_llm_call():
    llm = FakeToolCallingLLM("graph_tool")
    node = create_tool_selection_node(llm=llm)

    command = run(
        node(
            {
                "question": "按城市汇总订单金额",
                "context": {"route_type": "text2sql-query"},
                "eval_trace": {},
            }
        )
    )

    target, payload = _send(command)
    assert target == "sql_agent"
    assert payload["query_name"] == "sql_agent"
    assert llm.invoke_count == 0


def test_explicit_mysql_source_constraint_overrides_planner_graph_hint():
    llm = FakeToolCallingLLM("graph_tool")
    node = create_tool_selection_node(llm=llm)

    command = run(
        node(
            {
                "question": "按菜系统计每个菜系有多少道菜，并按数量从高到低排序。",
                "context": {
                    "route_type": "data-query",
                    "planner_task": {
                        "tool_hint": "graph_tool",
                        "parent_task": "请使用关系数据库（MySQL）按菜系统计每个菜系有多少道菜。",
                    },
                },
                "eval_trace": {},
            }
        )
    )

    target, payload = _send(command)
    assert target == "sql_agent"
    assert payload["query_name"] == "sql_agent"
    assert llm.invoke_count == 0


def test_sql_selection_paths_do_not_inject_an_implicit_max_rows():
    planner_llm = FakeToolCallingLLM("graph_tool")
    planner_node = create_tool_selection_node(llm=planner_llm)
    planner_command = run(
        planner_node(
            {
                "question": "统计各地区订单",
                "context": {
                    "route_type": "data-query",
                    "planner_task": {"tool_hint": "sql_agent"},
                },
                "eval_trace": {},
            }
        )
    )

    explicit_llm = FakeToolCallingLLM("graph_tool")
    explicit_node = create_tool_selection_node(llm=explicit_llm)
    explicit_command = run(
        explicit_node(
            {
                "question": "请使用 MySQL 查询订单总数",
                "context": {"route_type": "data-query"},
                "eval_trace": {},
            }
        )
    )

    semantic_llm = FakeToolCallingLLM("sql_agent")
    semantic_node = create_tool_selection_node(llm=semantic_llm)
    semantic_command = run(
        semantic_node(
            {
                "question": "统计各地区的业务记录",
                "context": {"route_type": "data-query"},
                "eval_trace": {},
            }
        )
    )

    for command in (planner_command, explicit_command, semantic_command):
        target, payload = _send(command)
        assert target == "sql_agent"
        assert "max_rows" not in payload["query_parameters"]

    assert planner_llm.invoke_count == 0
    assert explicit_llm.invoke_count == 0
    assert semantic_llm.invoke_count == 1


def test_explicit_sql_policy_request_bypasses_scope_guardrails():
    llm = FakeGuardrailsLLM()
    node = create_guardrails_node(llm=llm, graph=None)

    output = run(
        node(
            {
                "question": "请通过 SQL 删除 recipes 表中的所有数据。",
                "route_type": "data-query",
                "eval_trace": {},
            }
        )
    )

    assert output["next_action"] == "planner"
    assert output["summary"] is None
    assert llm.invoke_count == 0


def test_descriptive_question_is_semantically_selected_as_knowledge_rag_not_lightrag():
    llm = FakeToolCallingLLM("knowledge_rag", {"top_k": 4})
    node = create_tool_selection_node(llm=llm)

    command = run(
        node(
            {
                "question": "介绍一下宫保鸡丁的历史典故和风味特色",
                "context": {"route_type": "graphrag-query"},
                "eval_trace": {},
            }
        )
    )

    target, payload = _send(command)
    assert llm.invoke_count == 1
    assert target == "knowledge_rag"
    assert payload["query_parameters"] == {"top_k": 4}


def test_graph_selection_does_not_expose_predefined_or_text2cypher_strategy():
    llm = FakeToolCallingLLM("graph_tool")
    node = create_tool_selection_node(llm=llm)

    command = run(
        node(
            {
                "question": "查询宫保鸡丁和花生之间的关系",
                "context": {"route_type": "graphrag-query"},
                "eval_trace": {},
            }
        )
    )

    target, payload = _send(command)
    assert target == "graph_tool"
    assert payload["query_parameters"] == {}
    assert "strategy" not in payload["query_parameters"]


def test_empty_llm_selection_falls_back_to_canonical_graph_tool():
    llm = FakeToolCallingLLM(None)
    node = create_tool_selection_node(llm=llm, default_to_text2cypher=True)

    command = run(
        node(
            {
                "question": "查询未知关系",
                "context": {"route_type": "graphrag-query"},
                "eval_trace": {},
            }
        )
    )

    target, payload = _send(command)
    assert target == "graph_tool"
    assert payload["query_name"] == "graph_tool"


def test_unified_langgraph_node_executes_canonical_tool_and_keeps_legacy_history_shape():
    sql = FakeTool("sql_agent", "sql")
    rag = FakeTool("knowledge_rag", "rag")
    graph = FakeTool("graph_tool", "graph")
    executor = create_default_tool_executor(
        sql_agent=sql,
        knowledge_rag=rag,
        graph_tool=graph,
    )
    node = create_unified_tool_node(executor, "knowledge_rag")
    trace = {"trace_id": "phase2-step1"}

    output = run(
        node(
            {
                "task": "宫保鸡丁的历史",
                "query_parameters": {"top_k": 3},
                "eval_trace": trace,
            }
        )
    )

    assert output["tool_results"][0].tool_name == "knowledge_rag"
    assert output["tool_results"][0].tool_kind == "rag"
    assert output["steps"] == ["execute_knowledge_rag"]
    assert output["cyphers"][0]["task"] == "宫保鸡丁的历史"
    assert rag.calls == [("宫保鸡丁的历史", {"top_k": 3}, trace)]


def test_eval_normalizes_legacy_expected_tool_to_canonical_selection():
    response = {
        "route": "text2sql-query",
        "message": "共有 70 条记录",
        "metadata": {
            "eval_trace": {
                "trace_id": "t1",
                "events": [
                    {
                        "stage": "tool_selection",
                        "action": "dispatch",
                        "tool": "sql_agent",
                    },
                    {
                        "stage": "tool_execution",
                        "action": "done",
                        "tool": "sql_agent",
                        "metadata": {"execution_success": True},
                    },
                ],
            }
        },
    }
    result = evaluate_case(
        {
            "id": "legacy-golden",
            "expected_route": "text2sql-query",
            "expected_tools": ["text2sql_query"],
        },
        response,
        10.0,
    )

    assert result["expected"]["tools"] == ["sql_agent"]
    assert result["actual"]["selected_tools"] == ["sql_agent"]
    assert result["actual"]["executed_tools"] == ["sql_agent"]
    assert result["metrics"]["tool_selection_accuracy"] is True
    assert result["metrics"]["tool_exact_match"] is True
    assert result["metrics"]["sql_execution_success"] is True


def test_kb_route_hint_dispatches_knowledge_rag_without_llm_call():
    llm = FakeToolCallingLLM("graph_tool")
    node = create_tool_selection_node(llm=llm)

    command = run(
        node(
            {
                "question": "宫保鸡丁的历史典故",
                "context": {"route_type": "kb-query"},
                "eval_trace": {},
            }
        )
    )

    target, payload = _send(command)
    assert target == "knowledge_rag"
    assert payload["query_name"] == "knowledge_rag"
    assert llm.invoke_count == 0


def test_no_selection_with_fallback_disabled_routes_to_registered_error_node():
    llm = FakeToolCallingLLM(None)
    node = create_tool_selection_node(llm=llm, default_to_text2cypher=False)

    command = run(
        node(
            {
                "question": "无法判定的数据问题",
                "context": {"route_type": "graphrag-query"},
                "eval_trace": {},
            }
        )
    )

    target, payload = _send(command)
    assert target == "error_tool_selection"
    assert payload["errors"]


def test_unified_executor_canonicalizes_legacy_sql_trace_identity():
    sql = LegacyTraceSQLTool("sql_agent", "sql")
    executor = create_default_tool_executor(
        sql_agent=sql,
        knowledge_rag=FakeTool("knowledge_rag", "rag"),
        graph_tool=FakeTool("graph_tool", "graph"),
    )
    trace = {"trace_id": "canonical-trace", "events": [], "selected_tools": ["sql_agent"]}

    result = run(executor.execute("sql_agent", "统计 recipes 数量", trace=trace))

    assert result.tool_name == "sql_agent"
    assert trace["selected_tools"] == ["sql_agent"]
    event = trace["events"][0]
    assert event["tool"] == "sql_agent"
    assert event["metadata"]["native_tool_name"] == "text2sql_query"
