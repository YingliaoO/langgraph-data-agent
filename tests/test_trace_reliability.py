"""B-stage trace schema and reliability boundary tests."""

from multisource_data_agent.application.agents.observability import (
    create_trace,
    finish_trace,
    trace_event,
)
from multisource_data_agent.application.agents.tool_execution.interface import _error_category


def test_error_categories_are_stable() -> None:
    assert _error_category({"error_code": "READ_ONLY_POLICY_VIOLATION"}) == "policy_rejection"
    assert _error_category({"error_type": "TimeoutError", "error": "timed out"}) == "timeout"
    assert _error_category({"error": "connection refused by backend"}) == "service_unavailable"
    assert _error_category({"error": "permission denied for database user"}) == "permission_denied"
    assert _error_category({"error": "unexpected parser failure"}) == "tool_error"


def test_trace_has_schema_and_terminal_status() -> None:
    trace = create_trace(trace_id="trace-reliability")

    assert trace["schema_version"] == "b1"
    assert trace["status"] == "running"

    trace_event(
        trace,
        "tool_execution",
        "error",
        tool="sql_agent",
        success=False,
        error="schema failure",
        error_code="UNKNOWN_COLUMN",
        attempt=1,
    )
    finish_trace(trace, route="data-query", error="tool failed")

    assert trace["status"] == "error"
    assert trace["fallback"] is False
    assert trace["events"][0]["error_code"] == "UNKNOWN_COLUMN"
    assert trace["events"][0]["attempt"] == 1


def test_trace_redacts_sensitive_metadata() -> None:
    trace = create_trace(trace_id="trace-redaction")
    trace_event(
        trace,
        "tool_execution",
        "done",
        success=True,
        metadata={
            "api_key": "secret-value",
            "nested": {"password": "secret-password", "safe": "ok"},
            "dsn": "mysql://user:password@db.example/test",
        },
    )

    metadata = trace["events"][0]["metadata"]
    assert metadata["api_key"] == "<redacted>"
    assert metadata["nested"]["password"] == "<redacted>"
    assert metadata["nested"]["safe"] == "ok"
    assert metadata["dsn"] == "<redacted>"
