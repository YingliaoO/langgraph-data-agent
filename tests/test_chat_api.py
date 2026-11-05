"""Mocked current-path HTTP contract tests for the Enterprise Retail agent."""

import httpx
import pytest

from multisource_data_agent.application.agents.execution_contract import build_execution_dto
from multisource_data_agent.application.agents.tool_contracts import Evidence, Statement, ToolResult
from multisource_data_agent.interfaces.http.v1 import chat as chat_module
from multisource_data_agent.main import application


def _tool_result(kind: str, status: str = "success") -> ToolResult:
    names = {"sql": "sql_agent", "graph": "graph_tool", "rag": "knowledge_rag"}
    statement = None
    evidence = []
    data = []
    if kind == "sql":
        statement = Statement(language="sql", text="SELECT COUNT(*) AS total FROM retail_orders")
        evidence = [Evidence(kind="sql_row", content={"total": 12}, source="mysql", rank=1)]
        data = [{"total": 12}]
    elif kind == "graph":
        statement = Statement(language="cypher", text="MATCH (p:RetailProduct)-[:SUPPLIED_BY]->(s:RetailSupplier) RETURN s.name")
        evidence = [Evidence(kind="graph_record", content={"supplier": "NorthStar Electronics"}, source="neo4j", rank=1)]
        data = [{"supplier": "NorthStar Electronics"}]
    elif kind == "rag":
        evidence = [Evidence(kind="document", content="Electronics returns are allowed within 7 calendar days.", source="pgvector", source_id="ENT-REFUND-001", rank=1)]
    return ToolResult(
        tool_name=names[kind],
        tool_kind=kind,
        status=status,
        task="enterprise contract test",
        statement=statement,
        data=data,
        evidence=evidence if status != "error" else [],
    )


@pytest.fixture
def mocked_chat(monkeypatch):
    application.dependency_overrides[chat_module.get_db] = lambda: object()
    monkeypatch.setattr(chat_module, "get_or_create_session", lambda _db, _sid, _uid: "contract-session")

    async def save_message(*_args, **_kwargs):
        return "contract-message"

    monkeypatch.setattr(chat_module, "save_message", save_message)
    yield
    application.dependency_overrides.pop(chat_module.get_db, None)


async def _post_contract(monkeypatch, result: ToolResult, route: str = "data-query"):
    execution = build_execution_dto(tool_results=[result], route=route).model_dump(mode="json")

    async def fake_process(*_args, **_kwargs):
        return {
            "message": "contract answer",
            "route": route,
            "route_logic": "mocked",
            "sources": [],
            "metadata": {"eval_trace": {"trace_id": "contract-trace", "events": []}},
            "execution": execution,
        }

    monkeypatch.setattr(chat_module, "process_agent_query", fake_process)
    transport = httpx.ASGITransport(app=application)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        return await client.post("/api/v1/chat/", json={"message": "enterprise test"})


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("kind", "expected_tool", "statement_language"),
    [("sql", "sql_agent", "sql"), ("graph", "graph_tool", "cypher"), ("rag", "knowledge_rag", None)],
)
async def test_enterprise_http_execution_contract(mocked_chat, monkeypatch, kind, expected_tool, statement_language):
    response = await _post_contract(monkeypatch, _tool_result(kind))
    assert response.status_code == 200
    body = response.json()
    assert body["route"] == "data-query"
    assert body["execution"]["contract_version"] == "1"
    assert body["execution"]["status"] == "success"
    assert body["execution"]["tool_results"][0]["tool_name"] == expected_tool
    assert body["execution"]["evidence"]
    if statement_language:
        assert body["execution"]["tool_results"][0]["statement"]["language"] == statement_language
    if kind == "rag":
        assert body["execution"]["evidence"][0]["source"] == "pgvector"


@pytest.mark.asyncio
async def test_enterprise_http_policy_rejection_is_error_without_evidence(mocked_chat, monkeypatch):
    response = await _post_contract(monkeypatch, _tool_result("sql", status="error"))
    body = response.json()
    assert response.status_code == 200
    assert body["execution"]["status"] == "error"
    assert body["execution"]["tool_results"][0]["tool_name"] == "sql_agent"
    assert body["execution"]["evidence"] == []
