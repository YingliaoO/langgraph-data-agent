"""Regression checks must distinguish policy refusals, failures and n/a facts."""

import argparse
import json
from pathlib import Path

import pytest

from eval.evaluators.metrics import evaluate_case, summarize_results
from eval.run_eval import load_cases, thresholds_passed


def response(status="success", category=None, tools=None, evidence=None):
    tool_results = tools if tools is not None else [{
        "tool_name": "sql_agent", "status": status, "error_category": category,
    }]
    return {
        "message": "共有 5 位客户。", "route": "data-query",
        "execution": {
            "contract_version": "1", "status": status, "tool_results": tool_results,
            "evidence": evidence or [], "citation_ids": [], "fallback_reason": "none",
        },
    }


def case(**kwargs):
    return {"id": "test", "expected_route": "data-query", "expected_tools": ["sql_agent"],
            "expected_execution_status": "success", **kwargs}


def test_expected_policy_rejection_does_not_block_gate_or_inflate_tool_success():
    result = evaluate_case(case(expected_execution_status="error"), response("error", "policy_rejection"), 10, http_status=200)
    assert result["diagnostics"]["failure_reasons"] == []
    assert result["diagnostics"]["execution_outcome"] == "expected_policy_rejection"
    assert result["metrics"]["tool_execution_success"] is False
    assert result["metrics"]["execution_status_matched"] is True


@pytest.mark.parametrize("category", ["timeout", "connection", None])
def test_timeout_cannot_impersonate_expected_policy_error(category):
    result = evaluate_case(case(expected_execution_status="error"), response("error", category), 100000, http_status=200)
    assert result["metrics"]["execution_status_matched"] is True  # old score preserved
    assert result["diagnostics"]["execution_outcome"] == "unexpected_error"
    assert "expected_error_unverified" in result["diagnostics"]["failure_reasons"]


def test_explicit_nonpolicy_error_category_is_supported():
    result = evaluate_case(case(expected_execution_status="error", expected_error_categories=["timeout"]), response("error", "timeout"), 100000, http_status=200)
    assert result["diagnostics"]["execution_outcome"] == "expected_error"
    assert result["diagnostics"]["failure_reasons"] == []


def test_unexpected_timeout_and_partial_result_are_failures():
    for status in ["error", "partial"]:
        result = evaluate_case(case(), response(status, "timeout"), 100000, http_status=200)
        assert {"unexpected_execution_error", "execution_status_mismatch"} <= set(result["diagnostics"]["failure_reasons"])


def test_correct_empty_is_not_error_but_unexpected_fallback_blocks_gate():
    actual = response("empty")
    actual["execution"]["fallback_reason"] = "router_timeout"
    result = evaluate_case(case(expected_execution_status="empty", expected_evidence_available=False), actual, 61000, http_status=200)
    assert result["diagnostics"]["failure_reasons"] == ["unexpected_fallback"]
    assert result["diagnostics"]["warnings"] == ["fallback:router_timeout"]
    assert result["metrics"]["tool_execution_success"] is True


def test_extra_tool_blocks_exact_selection_without_changing_legacy_subset_metric():
    result = evaluate_case(case(), response(tools=[{"tool_name": name, "status": "success"} for name in ["sql_agent", "graph_tool"]]), 10, http_status=200)
    assert result["metrics"]["tool_selection_accuracy"] is True
    assert "tool_selection_mismatch" in result["diagnostics"]["failure_reasons"]


def test_general_control_cannot_silently_use_a_data_tool():
    result = evaluate_case(case(expected_tools=[]), response(), 10, http_status=200)
    assert "unexpected_tool_use" in result["diagnostics"]["failure_reasons"]


def test_http_error_is_not_labeled_transport_failure_in_diagnostics():
    result = evaluate_case(case(), None, 10, error="HTTP 503", http_status=503)
    assert "http_error" in result["diagnostics"]["failure_reasons"]
    assert "transport_error" not in result["diagnostics"]["failure_reasons"]
    transport = evaluate_case(case(), None, 10, error="TimeoutError")
    assert "transport_error" in transport["diagnostics"]["failure_reasons"]


def test_literal_warning_and_structured_assertion_failure_are_separate():
    assertion = {"id": "customer_count", "kind": "evidence_scalar", "source": "mysql", "equals": 5}
    result = evaluate_case(case(expected_key_facts=["five"], fact_assertions=[assertion]), response(evidence=[{"source": "mysql", "content": {"count": 15}}]), 10, http_status=200)
    assert "fact_assertion_mismatch" in result["diagnostics"]["failure_reasons"]
    assert "literal_fact_coverage_mismatch" in result["diagnostics"]["warnings"]


def test_summary_preserves_all_failures_and_expected_policy_refusals():
    results = [
        evaluate_case(case(), response(), 10, http_status=200),
        evaluate_case(case(), response("error", "timeout"), 100000, http_status=200),
        evaluate_case(case(expected_execution_status="error"), response("error", "policy_rejection"), 10, http_status=200),
    ]
    summary = summarize_results(results)
    assert summary["cases_total"] == 3
    assert summary["regression_failure_cases"] == 1
    assert summary["execution_outcome_counts"] == {"success": 1, "unexpected_error": 1, "expected_policy_rejection": 1}
    assert summary["avg_total_latency_ms"] == pytest.approx(100020 / 3)


@pytest.mark.parametrize("count,unchanged,passed", [(0, True, True), (1, True, False), (0, False, False), (None, True, False)])
def test_opt_in_gate_fails_closed(count, unchanged, passed):
    args = argparse.Namespace(fail_on_regression=True, min_route_accuracy=None, max_p95_latency_ms=None)
    assert thresholds_passed({"regression_failure_cases": count, "snapshot_unchanged": unchanged}, args) is passed


def test_old_threshold_callers_remain_compatible():
    args = argparse.Namespace(min_route_accuracy=None, max_p95_latency_ms=None)
    assert thresholds_passed({}, args)


def test_duplicate_or_unknown_selected_case_fails_before_http(tmp_path):
    dataset = tmp_path / "cases.jsonl"
    dataset.write_text(json.dumps({"id": "one", "query": "q"}) + "\n", encoding="utf-8")
    with pytest.raises(ValueError, match="Unknown or disabled"):
        load_cases(dataset, ["typo"])
    dataset.write_text(dataset.read_text(encoding="utf-8") * 2, encoding="utf-8")
    with pytest.raises(ValueError, match="unique"):
        load_cases(dataset, [])


def test_error_expectations_are_validated_before_http(tmp_path):
    dataset = tmp_path / "invalid.jsonl"
    dataset.write_text(json.dumps({"id": "one", "query": "q", "expected_error_categories": ["timeout"]}), encoding="utf-8")
    with pytest.raises(ValueError, match="expected_execution_status=error"):
        load_cases(dataset, [])


def test_runner_preserves_progress_and_uses_independent_run_ids(tmp_path, monkeypatch):
    from eval import run_eval

    dataset = tmp_path / "cases.jsonl"
    dataset.write_text('\n'.join(json.dumps({"id": name, "query": name}) for name in ["one", "two"]), encoding="utf-8")
    output = tmp_path / "results"
    monkeypatch.setattr("sys.argv", ["run_eval", "--dataset", str(dataset), "--output-dir", str(output)])
    monkeypatch.setattr(run_eval, "wait_for_backend_ready", lambda *args: True)
    monkeypatch.setattr(run_eval, "collect_environment_metadata", lambda **kwargs: {"source_snapshot": {"sha256": "fixed"}, "dataset_sha256": run_eval.hashlib.sha256(dataset.read_bytes()).hexdigest()})
    monkeypatch.setattr(run_eval, "archive_source_snapshot", lambda *args: {"path": "mock-source.zip"})
    users = []

    def call(**kwargs):
        users.append(kwargs["user_id"])
        if kwargs["query"] == "two":
            rows = list(output.glob("*/cases.jsonl"))
            assert len(rows) == 1 and len(rows[0].read_text(encoding="utf-8").splitlines()) == 1
            raise KeyboardInterrupt()
        return response(), 10, None, [], 200

    monkeypatch.setattr(run_eval, "call_chat_api_with_retries", call)
    with pytest.raises(KeyboardInterrupt):
        run_eval.main()
    assert list(output.glob("*/environment.json"))
    assert len(users) == 2 and users[0] != users[1]
