"""Exercise real LangGraph fork/join and HTTP serialization without external I/O.

Only model responses, tool execution boundaries, and chat persistence are faked.
The router/planner/selector nodes, both compiled graphs, unified executor,
evidence synthesis, state propagation, and HTTP handler are production code.
"""

import asyncio
import json

import httpx
import pytest
from langchain_core.runnables import RunnableLambda
from langgraph.checkpoint.memory import MemorySaver

from multisource_data_agent.application.agents import lg_builder
from multisource_data_agent.application.agents.kg_sub_graph.agentic_rag_agents.components.guardrails.models import GuardrailsOutput
from multisource_data_agent.application.agents.kg_sub_graph.agentic_rag_agents.components.planner.models import PlannerOutput, PlannerTask
from multisource_data_agent.application.agents.kg_sub_graph.agentic_rag_agents.components.summarize.answer_generator import AnswerSynthesis
from multisource_data_agent.application.agents.lg_states import Router
from multisource_data_agent.application.agents.tool_contracts import Evidence, Statement, ToolResult
from multisource_data_agent.application.agents.tool_execution.graph_tool import GraphTool
from multisource_data_agent.application.agents.tool_execution.knowledge_rag import KnowledgeRAGTool
from multisource_data_agent.application.agents.tool_execution.sql_agent import SQLAgent
from multisource_data_agent.interfaces.http.v1 import chat as chat_module
from multisource_data_agent.main import application


QUESTION = "How many orders are there, and what is the electronics return policy?"
SQL_TASK = "Count the retail orders."
RAG_TASK = "Find the electronics return policy."


class ScriptedModel:
    """Supply model outputs while leaving actual orchestration nodes intact."""

    def __init__(self):
        self.calls = []
        self.synthesis_contexts = []

    def with_structured_output(self, schema):
        def respond(value):
            self.calls.append(schema.__name__)
            if schema is Router:
                return Router(type="data-query", question=QUESTION, logic="mixed-source data")
            if schema is GuardrailsOutput:
                return GuardrailsOutput(decision="planner")
            if schema is PlannerOutput:
                return PlannerOutput(tasks=[
                    PlannerTask(task_id="orders", question=SQL_TASK, tool_hint="sql_agent"),
                    PlannerTask(task_id="policy", question=RAG_TASK, tool_hint="knowledge_rag"),
                ])
            if schema is AnswerSynthesis:
                payload = json.loads(value[-1].content)
                self.synthesis_contexts.append(payload)
                answer = "There are 12 orders."
                if payload["document_facts"]:
                    answer += " Electronics returns are allowed within 7 calendar days."
                if payload["metadata"]["errors"]:
                    answer += " The return policy could not be retrieved."
                return AnswerSynthesis(
                    answer=answer,
                    citation_ids=[item["evidence_id"] for item in payload["citation_evidence"]],
                )
            raise AssertionError(f"Unexpected model schema: {schema}")

        return RunnableLambda(respond)

    def bind_tools(self, *, tools):
        def unexpected_selection(_value):
            raise AssertionError("Explicit planner hints should dispatch without another model call")

        return RunnableLambda(unexpected_selection)


@pytest.mark.asyncio
@pytest.mark.parametrize("rag_fails", [False, True], ids=["two-tools-success", "one-tool-error-partial"])
async def test_real_workflow_merges_parallel_results_into_http_execution(monkeypatch, rag_fails):
    model = ScriptedModel()
    monkeypatch.setattr(lg_builder, "ChatOpenAI", lambda **_kwargs: model)
    monkeypatch.setattr(lg_builder.settings, "LLM_API_KEY", "offline-integration-test")

    # A fresh checkpoint store prevents cross-test conversation state. This is
    # the actual production top-level graph, which compiles the real subgraph.
    graph = lg_builder.builder.compile(checkpointer=MemorySaver())
    monkeypatch.setattr(chat_module, "graph", graph)

    started = []
    both_started = asyncio.Event()

    async def rendezvous(tool_name, task):
        started.append((tool_name, task))
        if len(started) == 2:
            both_started.set()
        # If fork/join becomes sequential, the first tool cannot finish and the
        # contract assertions fail instead of silently testing sequential work.
        await asyncio.wait_for(both_started.wait(), timeout=2)

    async def sql_execute(_self, task, context=None, *, trace=None):
        await rendezvous("sql_agent", task)
        return ToolResult(
            tool_name="sql_agent", tool_kind="sql", status="success", task=task,
            statement=Statement(language="sql", text="SELECT COUNT(*) AS order_count FROM retail_orders"),
            data=[{"order_count": 12}],
            evidence=[Evidence(kind="sql_row", source="mysql", content={"order_count": 12})],
        )

    async def rag_execute(_self, task, context=None, *, trace=None):
        await rendezvous("knowledge_rag", task)
        if rag_fails:
            raise ValueError("fixture retrieval failure")
        content = "Electronics returns are allowed within 7 calendar days."
        return ToolResult(
            tool_name="knowledge_rag", tool_kind="rag", status="success", task=task,
            data=[{"source_id": "ENT-REFUND-001", "content": content}],
            evidence=[Evidence(kind="document", source="pgvector", source_id="ENT-REFUND-001", content=content)],
        )

    async def unexpected_graph(*_args, **_kwargs):
        pytest.fail("The two-task plan must not execute graph_tool")

    monkeypatch.setattr(SQLAgent, "execute", sql_execute)
    monkeypatch.setattr(KnowledgeRAGTool, "execute", rag_execute)
    monkeypatch.setattr(GraphTool, "execute", unexpected_graph)

    # Persistence is not under test, but the HTTP handler and
    # process_agent_query are deliberately not replaced.
    session_id = f"workflow-integration-{rag_fails}"
    monkeypatch.setattr(chat_module, "get_or_create_session", lambda *_args: session_id)

    async def save_message(*_args, **_kwargs):
        return "integration-message"

    monkeypatch.setattr(chat_module, "save_message", save_message)
    previous_overrides = application.dependency_overrides.copy()
    application.dependency_overrides[chat_module.get_db] = lambda: object()
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=application), base_url="http://testserver"
        ) as client:
            response = await client.post("/api/v1/chat/", json={"message": QUESTION})
    finally:
        application.dependency_overrides.clear()
        application.dependency_overrides.update(previous_overrides)

    assert response.status_code == 200
    body = response.json()
    assert body["route"] == "data-query"
    execution = body["execution"]
    assert execution["contract_version"] == "1"
    assert execution["status"] == ("partial" if rag_fails else "success")
    assert sorted(started) == sorted([("sql_agent", SQL_TASK), ("knowledge_rag", RAG_TASK)])

    tasks = {task["task_id"]: task for task in execution["tasks"]}
    assert set(tasks) == {"orders", "policy"}
    assert tasks["orders"]["tool_hint"] == "sql_agent"
    assert tasks["policy"]["tool_hint"] == "knowledge_rag"
    assert all(task["depends_on"] == [] for task in tasks.values())

    results = {item["tool_name"]: item for item in execution["tool_results"]}
    assert len(execution["tool_results"]) == 2
    assert set(results) == {"sql_agent", "knowledge_rag"}
    assert results["sql_agent"]["status"] == "success"
    assert results["sql_agent"]["task_id"] == "orders"
    assert results["sql_agent"]["statement"]["language"] == "sql"
    assert results["sql_agent"]["rows_preview"] == [{"order_count": 12}]
    assert results["sql_agent"]["result_count"] == 1
    assert results["knowledge_rag"]["task_id"] == "policy"

    evidence = {item["evidence_id"]: item for item in execution["evidence"]}
    assert evidence["ev_orders_1"]["content"] == {"order_count": 12}
    assert evidence["ev_orders_1"]["source"] == "mysql"
    if rag_fails:
        assert results["knowledge_rag"]["status"] == "error"
        assert results["knowledge_rag"]["error_code"] == "tool_execution_exception"
        assert results["knowledge_rag"]["evidence_ids"] == []
        assert set(evidence) == {"ev_orders_1"}
        assert "could not be retrieved" in body["message"]
    else:
        assert results["knowledge_rag"]["status"] == "success"
        assert results["knowledge_rag"]["result_count"] == 1
        assert evidence["ev_policy_1"]["source"] == "pgvector"
        assert evidence["ev_policy_1"]["source_id"] == "ENT-REFUND-001"
        assert "7 calendar days" in body["message"]

    assert "12 orders" in body["message"]
    assert set(execution["citation_ids"]) == set(evidence)
    assert {eid for result in results.values() for eid in result["evidence_ids"]} == set(evidence)
    assert len(model.synthesis_contexts) == 1  # reducer joins before synthesis
    assert "12" in " ".join(model.synthesis_contexts[0]["sql_facts"])
    assert len(model.synthesis_contexts[0]["document_facts"]) == (0 if rag_fails else 1)
    assert model.calls.count("Router") == model.calls.count("PlannerOutput") == 1

    # Verify the canonical values also survived the subgraph -> top-level State
    # boundary, rather than being recovered only from debug trace/history.
    snapshot = await graph.aget_state({"configurable": {"thread_id": session_id}})
    assert len(snapshot.values["tasks"]) == 2
    assert len(snapshot.values["tool_results"]) == 2
    assert {item.evidence_id for item in snapshot.values["evidence"]} == set(evidence)
    assert set(snapshot.values["citation_ids"]) == set(evidence)
    assert snapshot.values["execution_summary"]["status"] == execution["status"]
