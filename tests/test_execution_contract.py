"""Tests for the bounded versioned execution DTO."""

from multisource_data_agent.application.agents.execution_contract import (
    build_execution_dto,
    normalize_tool_results,
)
from multisource_data_agent.application.agents.tool_contracts import Evidence, Statement, ToolResult


def test_execution_contract_assigns_stable_citations_and_bounded_preview() -> None:
    result = ToolResult(
        tool_name="sql_agent",
        tool_kind="sql",
        status="success",
        task="group recipes",
        statement=Statement(language="sql", text="SELECT * FROM recipes"),
        data=[{"group": index} for index in range(12)],
        evidence=[
            Evidence(kind="sql_row", content={"group": index}, source="mysql")
            for index in range(12)
        ],
        metadata={"row_count": 12},
    )

    normalized = normalize_tool_results([result])
    dto = build_execution_dto(
        tool_results=normalized,
        citation_ids=[normalized[0].evidence_ids[0]],
        route="data-query",
    )

    assert dto.contract_version == "1"
    assert dto.status == "success"
    assert dto.tool_results[0].result_count == 12
    assert len(dto.tool_results[0].rows_preview) == 10
    assert len(dto.evidence) == 12
    assert dto.evidence[0].evidence_id.startswith("ev_task_")
    assert dto.citation_ids == [dto.evidence[0].evidence_id]
    assert dto.tool_results[0].evidence_ids == [item.evidence_id for item in dto.evidence]
    assert dto.evidence[0].source == "mysql"
    assert dto.tool_results[0].statement.language == "sql"


def test_execution_contract_maps_rag_source_and_hides_sensitive_metadata() -> None:
    result = ToolResult(
        tool_name="knowledge_rag",
        tool_kind="rag",
        status="success",
        task="find fact",
        evidence=[
            Evidence(
                kind="document",
                content="a" * 2000,
                source="private/table/1",
                source_id="doc-1",
                metadata={"source_table": "private/table/1", "api_key": "secret"},
            )
        ],
    )

    dto = build_execution_dto(tool_results=normalize_tool_results([result]), route="data-query")

    assert dto.evidence[0].source == "pgvector"
    assert dto.evidence[0].source_id == "doc-1"
    assert len(dto.evidence[0].content) <= 500
    assert "api_key" not in dto.evidence[0].metadata
    assert dto.tool_results[0].documents_preview[0]["source"] == "pgvector"
    assert len(dto.tool_results[0].documents_preview[0]["content"]) <= 500


def test_execution_summary_distinguishes_partial_and_empty() -> None:
    empty = build_execution_dto(
        tool_results=[ToolResult(tool_name="knowledge_rag", tool_kind="rag", status="empty")],
        route="data-query",
    )
    partial = build_execution_dto(
        tool_results=[
            ToolResult(tool_name="sql_agent", tool_kind="sql", status="success"),
            ToolResult(tool_name="graph_tool", tool_kind="graph", status="error", metadata={"error_category": "timeout"}),
        ],
        route="data-query",
    )

    assert empty.status == "empty"
    assert empty.fallback_reason == "none"
    assert partial.status == "partial"
    assert partial.fallback_reason == "none"


def test_execution_summary_reserves_partial_for_success_plus_error() -> None:
    success_with_empty = build_execution_dto(
        tool_results=[
            ToolResult(tool_name="sql_agent", tool_kind="sql", status="success"),
            ToolResult(tool_name="knowledge_rag", tool_kind="rag", status="empty"),
        ],
        route="data-query",
    )
    error_with_empty = build_execution_dto(
        tool_results=[
            ToolResult(
                tool_name="graph_tool",
                tool_kind="graph",
                status="error",
                metadata={"error_category": "timeout"},
            ),
            ToolResult(tool_name="knowledge_rag", tool_kind="rag", status="empty"),
        ],
        route="data-query",
    )

    assert success_with_empty.status == "success"
    assert error_with_empty.status == "error"


def test_execution_contract_preserves_empty_and_policy_error_states() -> None:
    empty = build_execution_dto(
        tool_results=[
            ToolResult(tool_name="knowledge_rag", tool_kind="rag", status="empty"),
            ToolResult(tool_name="graph_tool", tool_kind="graph", status="empty"),
        ],
        route="data-query",
    )
    rejected = build_execution_dto(
        tool_results=[
            ToolResult(
                tool_name="sql_agent",
                tool_kind="sql",
                status="error",
                metadata={"error_code": "READ_ONLY_POLICY_VIOLATION", "error_category": "policy_rejection"},
            )
        ],
        route="data-query",
    )

    assert empty.status == "empty"
    assert empty.tool_results[0].result_count == 0
    assert rejected.status == "error"
    assert rejected.fallback_reason == "none"
    assert rejected.tool_results[0].error_code == "READ_ONLY_POLICY_VIOLATION"
    assert rejected.tool_results[0].error_category == "policy_rejection"


def test_execution_contract_preserves_planner_lineage_and_scopes_synthetic_sources() -> None:
    result = ToolResult(
        tool_name="sql_agent",
        tool_kind="sql",
        status="success",
        task="group recipes",
        evidence=[Evidence(kind="sql_row", content={"count": 1}, source="mysql", rank=1)],
        metadata={"planner_task": {"task_id": "task-1"}},
    )

    normalized = normalize_tool_results([result])
    dto = build_execution_dto(
        tasks=[{"task_id": "task-1", "question": "group recipes"}],
        tool_results=normalized,
        route="data-query",
    )

    assert dto.tasks[0].task_id == "task_1"
    assert dto.tool_results[0].task_id == "task_1"
    assert dto.evidence[0].task_id == "task_1"
    assert dto.evidence[0].source_id == "mysql:task_1:row:1"
    assert dto.citation_ids == []


def test_execution_contract_reports_only_real_fallbacks() -> None:
    trace = {
        "fallback": False,
        "events": [
            {
                "stage": "knowledge_rag",
                "action": "fallback",
                "fallback": True,
                "metadata": {"fallback_reason": "primary_backend_unavailable"},
            }
        ],
    }

    dto = build_execution_dto(
        tool_results=[ToolResult(tool_name="knowledge_rag", tool_kind="rag", status="success")],
        trace=trace,
        route="data-query",
    )

    assert dto.status == "success"
    assert dto.fallback_reason == "primary_backend_unavailable"
