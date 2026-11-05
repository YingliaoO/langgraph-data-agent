"""Phase 2 Step 2 tests for generic multi-source planning metadata."""

import asyncio
from pathlib import Path

from langchain_core.messages import AIMessage
from langchain_core.runnables import RunnableLambda

from multisource_data_agent.application.agents.kg_sub_graph.agentic_rag_agents.components.planner import (
    PlannerOutput,
    PlannerTask,
    create_planner_node,
    normalize_planner_tasks,
)
from multisource_data_agent.application.agents.kg_sub_graph.agentic_rag_agents.components.tool_selection.node import (
    create_tool_selection_node,
)
from multisource_data_agent.application.agents.kg_sub_graph.agentic_rag_agents.workflows.multi_agent.edges import (
    map_reduce_planner_to_tool_selection,
)
from multisource_data_agent.application.agents.tool_contracts import Evidence, ToolResult
from multisource_data_agent.application.agents.tool_execution.interface import (
    create_default_tool_executor,
    create_unified_tool_node,
)


def run(coro):
    return asyncio.run(coro)


class FakePlannerLLM:
    def __init__(self, output=None, error=None):
        self.output = output
        self.error = error
        self.invoke_count = 0

    def with_structured_output(self, _schema):
        async def respond(_prompt):
            self.invoke_count += 1
            if self.error:
                raise self.error
            return self.output

        return RunnableLambda(respond)


class FakeToolCallingLLM:
    def __init__(self, tool_name="graph_tool"):
        self.tool_name = tool_name
        self.invoke_count = 0

    def bind_tools(self, tools):
        def respond(_prompt):
            self.invoke_count += 1
            return AIMessage(
                content="",
                tool_calls=[
                    {
                        "name": self.tool_name,
                        "args": {},
                        "id": "call-generic-planner",
                        "type": "tool_call",
                    }
                ],
            )

        return RunnableLambda(respond)


class FakeDataTool:
    def __init__(self, name, kind):
        self.tool_name = name
        self.tool_kind = kind

    async def execute(self, task, context=None, *, trace=None):
        return ToolResult(
            tool_name=self.tool_name,
            tool_kind=self.tool_kind,
            status="success",
            task=task,
            data=[{"value": 1}],
            evidence=[Evidence(kind="sql_row", content={"value": 1})],
        )


def test_planner_schema_exposes_only_canonical_tool_intent():
    schema = PlannerTask.model_json_schema()
    enum = schema["properties"]["tool_hint"]["enum"]

    assert enum == ["auto", "sql_agent", "knowledge_rag", "graph_tool"]
    assert "text2sql_query" not in str(schema)
    assert "customer_tools" not in str(schema)


def test_route_hint_only_fills_auto_tool_hint():
    tasks = normalize_planner_tasks(
        "分析问题",
        "text2sql-query",
        [
            PlannerTask(task_id="task_1", question="统计订单", tool_hint="auto"),
            PlannerTask(task_id="task_2", question="查询客户关系", tool_hint="graph_tool"),
        ],
    )

    assert tasks[0].tool_hint == "sql_agent"
    assert tasks[1].tool_hint == "graph_tool"


def test_dependencies_are_normalized_to_earlier_tasks_only():
    tasks = normalize_planner_tasks(
        "综合分析",
        "graphrag-query",
        [
            PlannerTask(task_id="first", question="第一项"),
            PlannerTask(
                task_id="second",
                question="第二项",
                depends_on=["first", "missing", "second", "first"],
            ),
            PlannerTask(task_id="third", question="第三项", depends_on=["second"]),
        ],
    )

    assert tasks[0].depends_on == []
    assert tasks[1].depends_on == ["first"]
    assert tasks[2].depends_on == ["second"]


def test_duplicate_or_missing_task_ids_are_stabilized():
    tasks = normalize_planner_tasks(
        "综合分析",
        None,
        [
            PlannerTask(task_id="task_1", question="A"),
            PlannerTask(task_id="task_1", question="B", depends_on=["task_1"]),
            PlannerTask(question="C"),
        ],
    )

    assert [task.task_id for task in tasks] == ["task_1", "task_2", "task_3"]
    assert tasks[1].depends_on == ["task_1"]


def test_planner_is_bounded_and_falls_back_to_single_task_on_empty_output():
    many = [PlannerTask(question=f"q{i}") for i in range(20)]
    tasks = normalize_planner_tasks("父问题", None, many)
    fallback = normalize_planner_tasks("父问题", "kb-query", [])

    assert len(tasks) == 6
    assert len(fallback) == 1
    assert fallback[0].question == "父问题"
    assert fallback[0].parent_task == "父问题"
    assert fallback[0].tool_hint == "knowledge_rag"


def test_planner_runtime_failure_degrades_to_safe_single_task():
    llm = FakePlannerLLM(error=RuntimeError("planner unavailable"))
    node = create_planner_node(llm=llm)
    trace = {"trace_id": "planner-fallback", "events": []}

    output = run(
        node(
            {
                "question": "统计订单总额",
                "route_type": "text2sql-query",
                "eval_trace": trace,
            }
        )
    )

    assert llm.invoke_count == 1
    assert len(output["tasks"]) == 1
    assert output["tasks"][0].tool_hint == "sql_agent"
    assert output["tasks"][0].question == "统计订单总额"
    planner_done = [e for e in trace["events"] if e.get("stage") == "planner" and e.get("action") == "done"][-1]
    assert planner_done["fallback"] is True


def test_map_edge_preserves_planner_task_lineage():
    tasks = normalize_planner_tasks(
        "组合问题",
        "graphrag-query",
        [
            PlannerTask(task_id="task_1", question="查历史", tool_hint="knowledge_rag"),
            PlannerTask(
                task_id="task_2",
                question="查关系",
                tool_hint="graph_tool",
                depends_on=["task_1"],
            ),
        ],
    )

    sends = map_reduce_planner_to_tool_selection(
        {
            "question": "组合问题",
            "route_type": "graphrag-query",
            "tasks": tasks,
            "eval_trace": {},
        }
    )

    planner_task = sends[1].arg["context"]["planner_task"]
    assert planner_task == {
        "task_id": "task_2",
        "tool_hint": "graph_tool",
        "depends_on": ["task_1"],
        "parent_task": "组合问题",
    }


def test_planner_tool_hint_overrides_legacy_route_hint_without_llm_call():
    llm = FakeToolCallingLLM("sql_agent")
    node = create_tool_selection_node(llm=llm)

    command = run(
        node(
            {
                "question": "查询客户关系",
                "context": {
                    "route_type": "text2sql-query",
                    "planner_task": {
                        "task_id": "task_2",
                        "tool_hint": "graph_tool",
                        "depends_on": ["task_1"],
                        "parent_task": "综合分析",
                    },
                },
                "eval_trace": {},
            }
        )
    )

    assert command.goto.node == "graph_tool"
    assert command.goto.arg["planner_context"]["task_id"] == "task_2"
    assert llm.invoke_count == 0


def test_unified_tool_result_preserves_planner_metadata():
    executor = create_default_tool_executor(
        sql_agent=FakeDataTool("sql_agent", "sql"),
        knowledge_rag=FakeDataTool("knowledge_rag", "rag"),
        graph_tool=FakeDataTool("graph_tool", "graph"),
    )
    node = create_unified_tool_node(executor, "sql_agent")
    planner_context = {
        "task_id": "task_1",
        "tool_hint": "sql_agent",
        "depends_on": [],
        "parent_task": "统计并解释",
    }

    output = run(
        node(
            {
                "task": "统计订单",
                "query_parameters": {},
                "planner_context": planner_context,
                "eval_trace": {},
            }
        )
    )

    assert output["tool_results"][0].metadata["planner_task"] == planner_context


def test_planner_prompt_is_generic_data_agent_not_graph_only():
    source = Path(
        "multisource_data_agent/application/agents/kg_sub_graph/prompts/kg_prompts.py"
    ).read_text(encoding="utf-8")
    planner_source = source.split("PLANNER_SYSTEM_PROMPT", 1)[1].split(
        "TEXT2CYPHER_GENERATION_PROMPT", 1
    )[0]

    assert "Data Agent" in planner_source
    assert "sql_agent" in planner_source
    assert "knowledge_rag" in planner_source
    assert "graph_tool" in planner_source
    assert "depends_on" in planner_source
    assert "菜谱知识图谱的任务规划助手" not in planner_source
