"""Declared-check diagnostics; never relabel literal matching as faithfulness."""

from typing import Any, Dict, List


def validate_error_expectations(case: Dict[str, Any]) -> None:
    expected_fallback = case.get("expected_fallback")
    if expected_fallback is not None and not isinstance(expected_fallback, bool):
        raise ValueError("expected_fallback must be boolean when declared")
    if not isinstance(case.get("allow_fallback", False), bool):
        raise ValueError("allow_fallback must be boolean")
    if case.get("allow_fallback") is True and expected_fallback is False:
        raise ValueError("allow_fallback=true contradicts expected_fallback=false")
    categories = case.get("expected_error_categories")
    if categories is not None and (
        not isinstance(categories, list)
        or not categories
        or any(not isinstance(value, str) or not value.strip() for value in categories)
        or case.get("expected_execution_status") != "error"
    ):
        raise ValueError("expected_error_categories requires nonempty strings and expected_execution_status=error")


def diagnose_case(result: Dict[str, Any]) -> Dict[str, Any]:
    """Separate incorrect outcomes, expected refusals and scoring limitations.

    A generic expected error does not make a network timeout a correct refusal.
    For non-policy error tests, declare the allowed error categories explicitly.
    Literal mismatches are review warnings, not automatic semantic judgments.
    """
    metrics, actual, expected = result["metrics"], result["actual"], result["expected"]
    failures: List[str] = []
    warnings: List[str] = []
    status = actual.get("execution_status")
    categories = set(actual.get("error_categories", []))
    expected_error = expected.get("execution_status") == "error"
    allowed_errors = set(expected.get("error_categories") or ["policy_rejection"])
    verified_error = expected_error and status == "error" and bool(categories) and categories <= allowed_errors
    http_status = actual.get("http_status")

    if isinstance(http_status, int) and not 200 <= http_status < 300:
        failures.append("http_error")
    elif metrics.get("transport_error") is True:
        failures.append("transport_error")
    elif metrics.get("http_success") is not True:
        failures.append("http_result_missing")
    if metrics.get("agent_completed") is not True:
        failures.append("agent_incomplete")

    for key, reason in (
        ("route_correct", "route_mismatch"),
        ("tool_exact_match", "tool_selection_mismatch"),
        ("tool_execution_coverage", "tool_not_executed"),
        ("execution_status_matched", "execution_status_mismatch"),
        ("evidence_available_matched", "evidence_availability_mismatch"),
        ("asserted_facts_ok", "fact_assertion_mismatch"),
        ("rag_citation_resolution_ok", "citation_resolution_mismatch"),
        ("rag_cited_fact_support", "cited_fact_support_mismatch"),
        ("rag_abstention_ok", "abstention_mismatch"),
    ):
        if metrics.get(key) is False:
            failures.append(reason)
    tools_declared = expected.get("tools_declared", True)
    if tools_declared and expected.get("tools") == [] and (actual.get("selected_tools") or actual.get("executed_tools")):
        failures.append("unexpected_tool_use")
    if tools_declared and set(actual.get("executed_tools", [])) - set(expected.get("tools", [])):
        failures.append("unexpected_tool_execution")
    source_coverage = metrics.get("evidence_source_coverage")
    if isinstance(source_coverage, (int, float)) and source_coverage < 1:
        failures.append("expected_source_missing")

    if status in {"error", "partial"} or "error" in actual.get("tool_statuses", []):
        if verified_error:
            outcome = "expected_policy_rejection" if categories == {"policy_rejection"} else "expected_error"
        else:
            outcome = "unexpected_error"
            failures.append("expected_error_unverified" if expected_error else "unexpected_execution_error")
    elif status == "empty":
        outcome = "empty"
    elif status == "success":
        outcome = "success"
    else:
        outcome = "unobserved"

    for key in ("fact_coverage", "expected_fact_supported_by_evidence"):
        value = metrics.get(key)
        if value is not None and value < 1:
            warnings.append("literal_" + key + "_mismatch")
    if metrics.get("fallback") is True:
        warnings.append("fallback:" + str(result.get("fallback_reason", "unknown")))
        if expected.get("fallback") is False or (
            expected.get("fallback") is not True and expected.get("allow_fallback") is not True
        ):
            failures.append("unexpected_fallback")
    elif expected.get("fallback") is True:
        failures.append("expected_fallback_missing")

    return {
        "check_status": "FAIL" if failures else "WARN" if warnings else "PASS",
        "execution_outcome": outcome,
        "failure_reasons": sorted(set(failures)),
        "warnings": sorted(set(warnings)),
    }


def regression_gate(results: List[Dict[str, Any]], snapshot_unchanged: bool) -> Dict[str, Any]:
    failures = any(row.get("diagnostics", {}).get("failure_reasons") for row in results)
    warnings = any(row.get("diagnostics", {}).get("warnings") for row in results)
    status = "FAIL" if not results or failures or not snapshot_unchanged else "WARN" if warnings else "PASS"
    return {
        "status": status,
        "passed": status != "FAIL",
        "scope": "Declared checks only; WARN requires review and does not prove semantic correctness.",
    }
