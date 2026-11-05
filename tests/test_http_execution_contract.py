"""HTTP response tests for the first-class execution contract."""

import asyncio

from langchain_core.messages import AIMessage

from multisource_data_agent.application.agents.tool_contracts import Evidence, ToolResult
from multisource_data_agent.interfaces.http.v1 import chat as chat_module


def test_http_response_contains_execution_without_full_agent_state(monkeypatch) -> None:
    result = ToolResult(
        tool_name="knowledge_rag",
        tool_kind="rag",
        status="success",
        task="find fact",
        evidence=[Evidence(kind="document", content="fact", source="kb/1")],
    )

    class FakeGraph:
        async def ainvoke(self, input_state, config=None):
            return {
                "messages": [AIMessage(content="answer")],
                "router": {"type": "data-query", "logic": "test"},
                "tasks": [],
                "tool_results": [result],
                "sources": [],
            }

    monkeypatch.setattr(chat_module, "graph", FakeGraph())
    response = asyncio.run(chat_module.process_agent_query("find fact", "execution-http"))

    assert response["execution"]["contract_version"] == "1"
    assert response["execution"]["status"] == "success"
    assert response["execution"]["tool_results"][0]["tool_name"] == "knowledge_rag"
    assert response["execution"]["evidence"][0]["evidence_id"].startswith("ev_")
    assert response["execution"]["citation_ids"] == []
    assert "messages" not in response["execution"]
    assert "agent_state" not in response["metadata"]
    assert "tool_results" not in response["metadata"]

    response_model = chat_module.ChatResponse(
        message=response["message"],
        session_id="execution-http",
        message_id="message-1",
        route=response["route"],
        route_logic=response["route_logic"],
        sources=response["sources"],
        metadata=response["metadata"],
        execution=response["execution"],
    )
    assert response_model.execution.contract_version == "1"
