from eval.evaluators.metrics import evaluate_case, summarize_results


def _response_with_events(events):
    return {
        "message": "answer",
        "route": "data-query",
        "execution": {
            "contract_version": "1",
            "status": "success",
            "tool_results": [{"tool_name": "sql_agent", "tool_kind": "sql", "status": "success"}],
            "evidence": [],
            "citation_ids": [],
        },
        "metadata": {"eval_trace": {"trace_id": "latency-test", "events": events}},
    }


def test_normal_tool_start_done_reports_completed_and_observed_duration():
    result = evaluate_case(
        {},
        _response_with_events([
            {"stage": "tool_execution", "action": "start", "tool": "sql_agent", "ts": 10.0},
            {"stage": "tool_execution", "action": "done", "tool": "sql_agent", "ts": 10.25},
        ]),
        250.0,
    )

    assert result["metrics"]["latency_ms_tool"] == 250.0
    assert result["metrics"]["observed_duration_ms_tool"] == 250.0


def test_tool_error_reports_observed_duration_without_claiming_completion():
    result = evaluate_case(
        {},
        _response_with_events([
            {"stage": "tool_execution", "action": "start", "tool": "sql_agent", "ts": 20.0},
            {
                "stage": "tool_execution",
                "action": "error",
                "tool": "sql_agent",
                "ts": 20.4,
                "error_code": "tool_execution_exception",
                "latency_ms": 400.0,
            },
        ]),
        400.0,
    )

    assert result["metrics"]["latency_ms_tool"] is None
    assert result["metrics"]["observed_duration_ms_tool"] == 400.0


def test_tool_timeout_is_included_in_observed_average():
    result = evaluate_case(
        {},
        _response_with_events([
            {"stage": "tool_execution", "action": "start", "tool": "sql_agent", "ts": 30.0},
            {
                "stage": "tool_execution",
                "action": "timeout",
                "tool": "sql_agent",
                "ts": 130.0,
                "error_code": "tool_timeout",
                "latency_ms": 100000.0,
            },
        ]),
        100000.0,
    )

    summary = summarize_results([result])
    assert result["metrics"]["latency_ms_tool"] is None
    assert result["metrics"]["observed_duration_ms_tool"] == 100000.0
    assert summary["avg_latency_ms_tool"] == 100000.0
    assert summary["avg_completed_latency_ms_tool"] is None


def test_missing_terminal_event_has_no_fabricated_duration():
    result = evaluate_case(
        {},
        _response_with_events([
            {"stage": "tool_execution", "action": "start", "tool": "sql_agent", "ts": 40.0},
        ]),
        100.0,
    )

    assert result["metrics"]["latency_ms_tool"] is None
    assert result["metrics"]["observed_duration_ms_tool"] is None


def test_public_timeout_timing_is_observed_not_completed():
    response = _response_with_events([
        {"stage": "tool_execution", "action": "start", "tool": "sql_agent", "ts": 10.0},
        {"stage": "tool_execution", "action": "error", "tool": "sql_agent", "ts": 110.0},
    ])
    response["execution"].update({
        "status": "error", "timings": {"tool_execution_ms": 100000.0},
        "tool_results": [{"tool_name": "sql_agent", "status": "error", "error_category": "timeout"}],
    })
    row = evaluate_case({}, response, 100000.0)
    assert row["metrics"]["latency_ms_tool"] is None
    assert row["metrics"]["observed_duration_ms_tool"] == 100000.0


def test_zero_public_timing_is_not_discarded():
    response = _response_with_events([])
    response["execution"]["timings"] = {"tool_execution_ms": 0.0}
    row = evaluate_case({}, response, 1)
    assert row["metrics"]["observed_duration_ms_tool"] == 0.0
    assert row["metrics"]["latency_ms_tool"] == 0.0
