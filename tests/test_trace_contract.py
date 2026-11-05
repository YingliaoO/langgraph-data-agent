"""Trace contract compatibility checks."""

from multisource_data_agent.application.agents.observability import create_trace, finish_trace, trace_event


def test_trace_contract_has_version_status_attempt_and_redaction() -> None:
    trace = create_trace(trace_id="trace-contract")
    trace_event(
        trace,
        "tool_execution",
        "error",
        error="timeout",
        error_code="SQL_TIMEOUT",
        attempt=1,
        metadata={"nested": {"password": "hidden", "safe": "value"}},
    )
    finish_trace(trace, route="data-query", error="timeout")

    assert trace["schema_version"] == "b1"
    assert trace["status"] == "error"
    assert trace["fallback"] is False
    assert trace["events"][0]["error_code"] == "SQL_TIMEOUT"
    assert trace["events"][0]["attempt"] == 1
    assert trace["events"][0]["metadata"]["nested"]["password"] == "<redacted>"
