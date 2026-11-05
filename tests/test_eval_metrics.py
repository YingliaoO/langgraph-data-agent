"""B-stage evaluator tests for canonical results and evidence."""

from eval.evaluators.metrics import evaluate_case


def _response_with_result(*, status="success", execution_success=True):
    return {
        "message": "宫保鸡丁的历史典故来自知识库。",
        "route": "data-query",
        "sources": [],
        "metadata": {
            "eval_trace": {
                "trace_id": "trace-eval-contract",
                "selected_tools": ["knowledge_rag"],
                "events": [
                    {
                        "stage": "tool_selection",
                        "action": "dispatch",
                        "success": True,
                        "tool": "knowledge_rag",
                    },
                    {
                        "stage": "tool_execution",
                        "action": "done",
                        "tool": "knowledge_rag",
                        "metadata": {
                            "status": status,
                            "execution_success": execution_success,
                        },
                    },
                ],
            },
            "tool_results": [
                {
                    "tool_name": "knowledge_rag",
                    "tool_kind": "rag",
                    "status": status,
                    "evidence": [
                        {
                            "kind": "document",
                            "content": "宫保鸡丁的历史典故来自宫廷传说。",
                            "source": "kb/table/1",
                            "source_id": "doc-1",
                            "rank": 1,
                            "metadata": {"title": "宫保鸡丁"},
                        }
                    ],
                }
            ],
        },
    }


def test_eval_uses_canonical_tool_result_evidence_for_recall() -> None:
    result = evaluate_case(
        {
            "id": "rag-evidence",
            "expected_route": "kb-query",
            "expected_tools": ["postgres"],
            "expected_sources": ["宫保鸡丁"],
            "expected_key_facts": ["宫保鸡丁"],
        },
        _response_with_result(),
        100.0,
    )

    assert result["actual"]["canonical_route"] == "data-query"
    assert result["metrics"]["tool_selection_accuracy"] is True
    assert result["metrics"]["tool_execution_coverage"] is True
    assert result["metrics"]["retrieval_recall_at_k"] == 1.0
    assert result["metrics"]["evidence_source_coverage"] == 1.0
    assert result["metrics"]["evidence_available"] is True


def test_eval_does_not_call_dispatch_success_execution_success() -> None:
    response = _response_with_result(status="error", execution_success=False)
    response["metadata"]["eval_trace"]["events"][0]["success"] = True

    result = evaluate_case(
        {"id": "tool-error", "expected_tools": ["knowledge_rag"]},
        response,
        100.0,
    )

    assert result["metrics"]["tool_selection_accuracy"] is True
    assert result["metrics"]["tool_execution_coverage"] is True
    assert result["metrics"]["rag_execution_success"] is False
    assert result["actual"]["execution_status"] == "error"
    assert result["fallback_reason"] == "none"


def test_eval_keeps_retry_latency_breakdown_separate() -> None:
    result = evaluate_case(
        {"id": "retry-latency"},
        {},
        350.0,
        latency_attempts=[
            {"attempt": 1, "latency_ms": 150.0, "success": False, "error": "timeout"},
            {"attempt": 2, "latency_ms": 200.0, "success": True, "error": None},
        ],
    )

    assert result["metrics"]["latency_ms_total"] == 350.0
    assert result["metrics"]["latency_ms_first_attempt"] == 150.0
    assert result["metrics"]["latency_ms_successful_attempt"] == 200.0
    assert result["metrics"]["retry_count"] == 1


def test_eval_prefers_execution_contract_over_debug_state() -> None:
    response = {
        "message": "grounded answer",
        "route": "data-query",
        "metadata": {"eval_trace": {}, "agent_state": {"tool_results": []}},
        "execution": {
            "contract_version": "1",
            "status": "success",
            "tool_results": [
                {"tool_name": "sql_agent", "tool_kind": "sql", "status": "success"}
            ],
            "evidence": [
                {"evidence_id": "ev_task_1_1", "kind": "sql_row", "source": "mysql", "source_id": "sql_row:1"}
            ],
            "citation_ids": ["ev_task_1_1"],
        },
    }

    result = evaluate_case(
        {"expected_tools": ["sql_agent"], "expected_sources": ["mysql"]},
        response,
        10.0,
    )

    assert result["metrics"]["tool_selection_accuracy"] is True
    assert result["metrics"]["tool_execution_ok"] is True
    assert result["metrics"]["evidence_available"] is True
    assert result["metrics"]["evidence_source_coverage"] == 1.0


def test_evidence_source_coverage_does_not_treat_content_or_evidence_id_as_a_source() -> None:
    response = {
        "message": "answer",
        "route": "data-query",
        "execution": {
            "contract_version": "1",
            "status": "success",
            "tool_results": [{"tool_name": "knowledge_rag", "tool_kind": "rag", "status": "success"}],
            "evidence": [
                {
                    "evidence_id": "expected-source",
                    "kind": "document",
                    "source": "pgvector",
                    "source_id": "doc-1",
                    "content": "expected-source appears only in document content",
                    "metadata": {"title": "real-title"},
                }
            ],
            "citation_ids": [],
        },
    }

    result = evaluate_case(
        {"expected_sources": ["expected-source"], "expected_key_facts": ["expected-source"]},
        response,
        10.0,
    )

    assert result["metrics"]["evidence_source_coverage"] == 0.0
    assert result["metrics"]["expected_fact_supported_by_evidence"] is True


def test_eval_uses_execution_status_without_turning_error_into_fallback() -> None:
    response = {
        "message": "SQL write rejected.",
        "route": "data-query",
        "execution": {
            "contract_version": "1",
            "status": "error",
            "tool_results": [
                {
                    "tool_name": "sql_agent",
                    "tool_kind": "sql",
                    "status": "error",
                    "error_category": "policy_rejection",
                }
            ],
            "evidence": [],
            "citation_ids": [],
            "fallback_reason": "none",
        },
    }

    result = evaluate_case(
        {
            "id": "sql-policy",
            "expected_route": "data-query",
            "expected_tools": ["sql_agent"],
            "expected_execution_status": "error",
            "expected_evidence_available": False,
        },
        response,
        25.0,
        http_status=200,
    )

    assert result["metrics"]["http_success"] is True
    assert result["actual"]["execution_status"] == "error"
    assert result["metrics"]["execution_status_matched"] is True
    assert result["metrics"]["evidence_available_matched"] is True
    assert result["fallback_reason"] == "none"
    assert result["metrics"]["fallback"] is False
    assert result["actual"]["error_categories"] == ["policy_rejection"]


def test_eval_reports_real_execution_fallback_reason() -> None:
    response = {
        "message": "answer",
        "route": "data-query",
        "execution": {
            "contract_version": "1",
            "status": "success",
            "tool_results": [
                {"tool_name": "graph_tool", "tool_kind": "graph", "status": "success"}
            ],
            "evidence": [],
            "citation_ids": [],
            "fallback_reason": "tool_execution_fallback",
        },
    }

    result = evaluate_case({}, response, 10.0, http_status=200)

    assert result["fallback_reason"] == "tool_execution_fallback"
    assert result["metrics"]["fallback"] is True


def test_eval_http_status_and_transport_error_are_separate() -> None:
    result = evaluate_case(
        {"id": "transport"},
        None,
        1000.0,
        error="TimeoutError()",
        http_status=None,
    )

    assert result["metrics"]["http_success"] is False
    assert result["metrics"]["transport_error"] is True
    assert result["fallback_reason"] == "none"


def test_summary_reports_execution_status_and_partial_success() -> None:
    from eval.evaluators.metrics import summarize_results

    success = evaluate_case(
        {},
        {
            "message": "ok",
            "route": "data-query",
            "execution": {
                "contract_version": "1",
                "status": "success",
                "tool_results": [{"tool_name": "sql_agent", "status": "success"}],
                "evidence": [],
                "citation_ids": [],
                "fallback_reason": "none",
            },
        },
        10.0,
        http_status=200,
    )
    partial = evaluate_case(
        {},
        {
            "message": "partial",
            "route": "data-query",
            "execution": {
                "contract_version": "1",
                "status": "partial",
                "tool_results": [
                    {"tool_name": "sql_agent", "status": "success"},
                    {"tool_name": "knowledge_rag", "status": "error"},
                ],
                "evidence": [],
                "citation_ids": [],
                "fallback_reason": "none",
            },
        },
        20.0,
        http_status=200,
    )

    summary = summarize_results([success, partial])

    assert summary["execution_status_counts"] == {"partial": 1, "success": 1}
    assert summary["tool_status_counts"] == {"error": 1, "success": 2}
    assert summary["partial_success_rate"] == 0.5
    assert summary["transport_error_rate"] == 0.0


def test_eval_does_not_duplicate_tool_status_from_trace_when_execution_contract_exists() -> None:
    from eval.evaluators.metrics import summarize_results

    response = {
        "message": "ok",
        "route": "data-query",
        "metadata": {
            "eval_trace": {
                "trace_id": "trace-tool-status",
                "events": [
                    {
                        "stage": "tool_execution",
                        "action": "done",
                        "tool": "sql_agent",
                        "metadata": {"status": "success", "execution_success": True},
                    },
                    {
                        "stage": "tool_result",
                        "action": "done",
                        "tool": "sql_agent",
                        "metadata": {"status": "success"},
                    },
                ],
            }
        },
        "execution": {
            "contract_version": "1",
            "status": "success",
            "tool_results": [{"tool_name": "sql_agent", "status": "success"}],
            "evidence": [],
            "citation_ids": [],
            "fallback_reason": "none",
        },
    }

    result = evaluate_case({}, response, 10.0, http_status=200)
    summary = summarize_results([result])

    assert result["actual"]["tool_statuses"] == ["success"]
    assert summary["tool_status_counts"] == {"success": 1}


def test_file_route_expected_fact_is_not_scored_as_canonical_evidence_support() -> None:
    response = {
        "message": "这个 CSV 一共有 3 条数据记录。",
        "route": "file-query",
        "metadata": {"eval_trace": {"trace_id": "trace-file", "events": []}},
        "execution": {
            "contract_version": "1",
            "status": "success",
            "tool_results": [],
            "evidence": [],
            "citation_ids": [],
            "fallback_reason": "none",
        },
    }

    result = evaluate_case(
        {
            "expected_route": "file-query",
            "expected_key_facts": ["3"],
        },
        response,
        10.0,
        http_status=200,
    )

    assert result["metrics"]["fact_coverage"] == 1.0
    assert result["metrics"]["expected_fact_supported_by_evidence"] is None
