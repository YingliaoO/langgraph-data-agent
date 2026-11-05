"""B-stage regression tests for canonical ToolResult propagation."""

from __future__ import annotations

import asyncio

from langchain_core.messages import AIMessage

from multisource_data_agent.application.agents.lg_states import AgentState
from multisource_data_agent.application.agents.tool_contracts import Evidence, ToolResult
from multisource_data_agent.interfaces.http.v1 import chat as chat_module


def test_top_level_agent_state_declares_canonical_tool_results() -> None:
    state = AgentState(messages=[])

    assert state.tool_results == []
    assert "tool_results" in AgentState.__annotations__


def test_http_process_propagates_serialized_tool_results(monkeypatch) -> None:
    result = ToolResult(
        tool_name="knowledge_rag",
        tool_kind="rag",
        status="success",
        task="find a document",
        evidence=[
            Evidence(
                kind="document",
                content="grounded fact",
                source="kb/table/1",
                score=0.91,
            )
        ],
        metadata={"backend": "pgvector"},
    )

    class FakeGraph:
        async def ainvoke(self, input_state, config=None):
            return {
                "messages": [AIMessage(content="grounded answer")],
                "router": {"type": "data-query", "logic": "test"},
                "tool_results": [result],
                "sources": [],
            }

    monkeypatch.setattr(chat_module, "graph", FakeGraph())

    response = asyncio.run(
        chat_module.process_agent_query(
            "find a document",
            "b-propagation-test",
            user_id="test",
            trace_id="trace-propagation-test",
        )
    )

    execution = response["execution"]
    assert execution["tool_results"][0]["tool_name"] == "knowledge_rag"
    assert execution["tool_results"][0]["tool_kind"] == "rag"
    assert execution["tool_results"][0]["status"] == "success"
    assert execution["evidence"][0]["source"] == "pgvector"
    assert response["metadata"].get("agent_state") is None
    assert response["metadata"].get("tool_results") is None
