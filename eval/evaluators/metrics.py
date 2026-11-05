import math
import re
from typing import Any, Dict, Iterable, List, Optional, Sequence, Set

from multisource_data_agent.application.agents.routing import normalize_route_type
from multisource_data_agent.application.agents.timing import interval_durations, stage_intervals

from .fact_assertions import evaluate_fact_assertions
from .rag_quality import evaluate_rag_quality
from .regression import diagnose_case


FALLBACK_MARKERS = ("超出范围", "拒绝")
TEXT2SQL_TOOLS = {"sql_agent", "text2sql_query"}
CYPHER_TOOLS = {"graph_tool", "cypher_query", "predefined_cypher"}
RAG_TOOLS = {"knowledge_rag", "postgres", "milvus", "customer_tools"}

TOOL_NAME_ALIASES = {
    "text2sql_query": "sql_agent",
    "text2sql": "sql_agent",
    "sql": "sql_agent",
    "postgres": "knowledge_rag",
    "pgvector": "knowledge_rag",
    "milvus": "knowledge_rag",
    "knowledge_retrieval": "knowledge_rag",
    # Historical eval labels only; these are not executable aliases in the
    # Phase 2 runtime registry.
    "customer_tools": "knowledge_rag",
    "microsoft_graphrag_query": "knowledge_rag",
    "cypher_query": "graph_tool",
    "predefined_cypher": "graph_tool",
    "text2cypher": "graph_tool",
}


def evaluate_case(
    case: Dict[str, Any],
    response: Optional[Dict[str, Any]],
    latency_ms: float,
    error: Optional[str] = None,
    latency_attempts: Optional[Sequence[Dict[str, Any]]] = None,
    http_status: Optional[int] = None,
) -> Dict[str, Any]:
    response = response or {}
    answer = _string(response.get("message"))
    expected_route = case.get("expected_route")
    expected_tools = _canonical_tool_names(_strings(case.get("expected_tools")))
    selected_tools = _selected_tools(response)
    executed_tools = _executed_tools(response)
    expected_sources = _strings(case.get("expected_sources"))
    expected_facts = _strings(case.get("expected_key_facts") or case.get("expected_answer_facts"))
    trace = _trace(response)
    events = _events(response)
    fact_coverage = _fact_coverage(answer, expected_facts)
    assertions = evaluate_fact_assertions(case.get("fact_assertions", []), response)
    # This legacy metric checks literal occurrences, not semantic grounding.
    # File/general routes can have expected answer facts without using the
    # canonical SQL/RAG/Graph Evidence contract, so exclude them from this
    # denominator rather than counting the absence of Evidence as a failure.
    canonical_expected_route = (
        normalize_route_type(expected_route, default="") if expected_route else ""
    )
    evidence_fact_support = (
        None
        if canonical_expected_route and canonical_expected_route != "data-query"
        else _expected_fact_supported_by_evidence(response, expected_facts)
    )
    fallback_reason = _fallback_reason(response, error)
    tool_execution_success = _tool_execution_ok(response)
    execution_status = _execution_status(response)
    evidence_available = _evidence_available(response)
    expected_execution_status = case.get("expected_execution_status")
    expected_evidence_available = case.get("expected_evidence_available")

    route_correct = (
        None
        if not expected_route
        else normalize_route_type(response.get("route"), default="") == normalize_route_type(expected_route, default="")
    )
    selection_accuracy = _tool_subset_metric(expected_tools, selected_tools)
    execution_coverage = _tool_subset_metric(expected_tools, executed_tools)
    exact_match = _tool_exact_metric(expected_tools, selected_tools)
    retrieval_recall_at_k = _recall(expected_sources, _source_identifiers(response)) if expected_sources else None
    expected_fallback = case.get("expected_fallback")
    expected_fallback_matched = (
        None if not isinstance(expected_fallback, bool) else (fallback_reason != "none") == expected_fallback
    )

    metrics = {
        "http_success": _http_success(response, error, http_status),
        "agent_completed": _agent_completed(response, error, events),
        "route_correct": route_correct,
        "route_accuracy": route_correct,
        "tool_dispatch_ok": _event_presence(events, "tool_selection", "dispatch"),
        "tool_execution_ok": tool_execution_success,
        "tool_execution_success": tool_execution_success,
        "tool_non_error_completion": tool_execution_success,
        "tool_all_success": _all_tools_success(response),
        "evidence_available": evidence_available,
        "execution_status_matched": (
            None
            if not expected_execution_status
            else execution_status == _string(expected_execution_status)
        ),
        "evidence_available_matched": (
            None
            if not isinstance(expected_evidence_available, bool)
            else evidence_available is expected_evidence_available
        ),
        "partial_success": execution_status == "partial",
        "transport_error": error is not None and http_status is None,
        "http_error": isinstance(http_status, int) and not 200 <= http_status < 300,
        "evidence_source_coverage": (
            _recall(expected_sources, _evidence_source_identifiers(response))
            if expected_sources
            else None
        ),
        "answer_generated": _event_presence(events, "final_answer", "done"),
        "fact_coverage_ok": None if fact_coverage is None else fact_coverage >= 0.5,
        "expected_fact_supported_by_evidence": evidence_fact_support,
        "tool_selection_accuracy": selection_accuracy,
        "tool_execution_coverage": execution_coverage,
        "tool_exact_match": exact_match,
        "tool_accuracy": selection_accuracy,  # legacy compatibility alias
        "retrieval_recall_at_k": retrieval_recall_at_k,
        "fact_coverage": fact_coverage,
        "asserted_fact_coverage": (
            sum(item["passed"] for item in assertions) / len(assertions) if assertions else None
        ),
        "asserted_facts_ok": all(item["passed"] for item in assertions) if assertions else None,
        "answer_faithfulness": fact_coverage,  # legacy alias; Answer Literal Fact Coverage only
        "matched_answer_facts": [fact for fact in expected_facts if _contains(answer, fact)],
        "fallback": fallback_reason != "none",
        "fallback_reason": fallback_reason,
        "expected_fallback_matched": expected_fallback_matched,
        "sql_execution_success": _execution_success(response, TEXT2SQL_TOOLS),
        "cypher_executable": _execution_success(response, CYPHER_TOOLS),
        "rag_execution_success": _execution_success(response, RAG_TOOLS),
        "token_total": _token_total(response),
        "token_cost": _token_cost(response),
        "trace_present": bool(trace.get("trace_id") and events),
        "latency_ms_total": _number_or_none(latency_ms),
        "total_latency": _number_or_none(latency_ms),
        "latency_ms_first_attempt": _first_attempt_latency(latency_attempts),
        "latency_ms_successful_attempt": _successful_attempt_latency(latency_attempts),
        "retry_count": max(0, len(latency_attempts or []) - 1),
        "latency_ms_agent": _stage_latency(events, "agent_graph", "start", "agent_graph", "done"),
        "latency_ms_router": _execution_timing(response, "router_ms")
        or _stage_latency(events, "router", "start", "router", "decision"),
        "latency_ms_planner": _execution_timing(response, "planner_ms"),
        # Keep completed duration separate from the observed duration of an
        # error/timeout. Aggregate reports use the latter to avoid survivor bias.
        "latency_ms_tool": _completed_tool_duration(response, events),
        "observed_duration_ms_tool": (
            _execution_timing(response, "tool_execution_ms")
            if _execution_timing(response, "tool_execution_ms") is not None
            else _tool_observed_latency(events)
        ),
        "observed_work_ms_tool": _execution_timing(response, "tool_execution_sum_ms")
        if _execution_timing(response, "tool_execution_sum_ms") is not None else _tool_observed_work(events),
        "latency_ms_answer": _execution_timing(response, "summarize_ms")
        or _stage_latency(events, "summarize", "start", "final_answer", "done"),
    }
    metrics.update(evaluate_rag_quality(case, response))
    result = {
        "id": case.get("id", case.get("case_id")),
        "case_id": case.get("case_id", case.get("id")),
        "category": case.get("category"),
        "difficulty": case.get("difficulty"),
        "query": case.get("query", case.get("question")),
        "latency_ms": round(latency_ms, 2),
        "error": error,
        "fallback_reason": fallback_reason,
        "expected": {
            "route": expected_route,
            "tools": expected_tools,
            "tools_declared": "expected_tools" in case,
            "sources": expected_sources,
            "answer_facts": expected_facts,
            "fact_assertions": case.get("fact_assertions", []),
            "fallback": expected_fallback,
            "allow_fallback": case.get("allow_fallback", False),
            "sql_success": case.get("expected_sql_success"),
            "cypher_executable": case.get("expected_cypher_executable"),
            "execution_status": expected_execution_status,
            "evidence_available": expected_evidence_available,
            "error_categories": case.get("expected_error_categories"),
        },
        "actual": {
            "http_status": http_status,
            "llm_calls": _llm_observations(trace),
            "route": response.get("route"),
            "canonical_route": normalize_route_type(response.get("route"), default=""),
            "selected_tools": selected_tools,
            "executed_tools": executed_tools,
            "source_ids": sorted(_source_identifiers(response)),
            "evidence_source_ids": sorted(_evidence_source_identifiers(response)),
            "tool_result_count": len(_tool_result_items(response)),
            "tool_statuses": _tool_statuses(response),
            "execution_status": execution_status,
            "evidence_count": _evidence_count(response),
            "citation_count": _citation_count(response),
            "error_categories": _error_categories(response),
            "trace_errors": [
                {key: event[key] for key in ("stage", "action", "tool", "error_code", "latency_ms") if key in event}
                for event in events if event.get("error_code")
            ],
            "statements": _statements(response),
            "answer": answer,
            "fact_assertion_results": assertions,
            "trace_id": trace.get("trace_id"),
            "trace_event_count": len(events),
            "trace_stages": sorted({_string(event.get("stage")) for event in events if event.get("stage")}),
            "answer_preview": answer[:240],
        },
        "metrics": metrics,
    }
    result["diagnostics"] = diagnose_case(result)
    return result


def summarize_results(results: Sequence[Dict[str, Any]]) -> Dict[str, Any]:
    latencies = [float(item["metrics"]["latency_ms_total"]) for item in results if isinstance(item.get("metrics", {}).get("latency_ms_total"), (int, float))]
    return {
        "cases_total": len(results),
        "regression_failure_cases": sum(bool(item.get("diagnostics", {}).get("failure_reasons")) for item in results),
        "failure_reason_counts": _diagnostic_counts(results, "failure_reasons"),
        "review_warning_counts": _diagnostic_counts(results, "warnings"),
        "execution_outcome_counts": _diagnostic_counts(results, "execution_outcome"),
        "cases_with_errors": sum(1 for item in results if item.get("error")),
        "metric_counts": _metric_counts(results),
        "fact_assertion_counts": {
            "total": sum(len(row.get("actual", {}).get("fact_assertion_results", [])) for row in results),
            "passed": sum(check.get("passed") is True for row in results for check in row.get("actual", {}).get("fact_assertion_results", [])),
        },
        "trace_coverage_rate": _mean_metric(results, "trace_present"),
        "http_success_rate": _mean_metric(results, "http_success"),
        "agent_completion_rate": _mean_metric(results, "agent_completed"),
        "route_accuracy": _mean_metric(results, "route_correct"),
        "tool_selection_accuracy": _mean_metric(results, "tool_selection_accuracy"),
        "tool_execution_coverage": _mean_metric(results, "tool_execution_coverage"),
        "tool_exact_match": _mean_metric(results, "tool_exact_match"),
        "tool_dispatch_rate": _mean_metric(results, "tool_dispatch_ok"),
        "tool_execution_success_rate": _mean_metric(results, "tool_execution_ok"),
        "tool_non_error_completion_rate": _mean_metric(results, "tool_non_error_completion"),
        "tool_all_success_rate": _mean_metric(results, "tool_all_success"),
        "execution_status_accuracy": _mean_metric(results, "execution_status_matched"),
        "evidence_available_accuracy": _mean_metric(results, "evidence_available_matched"),
        "expected_fact_supported_by_evidence_rate": _mean_metric(
            results, "expected_fact_supported_by_evidence"
        ),
        "answer_generated_rate": _mean_metric(results, "answer_generated"),
        "evidence_available_rate": _mean_metric(results, "evidence_available"),
        "evidence_source_coverage": _mean_metric(results, "evidence_source_coverage"),
        "retrieval_recall_at_k": _mean_metric(results, "retrieval_recall_at_k"),
        "fact_coverage": _mean_metric(results, "fact_coverage"),
        "asserted_fact_coverage": _mean_metric(results, "asserted_fact_coverage"),
        "asserted_facts_ok_rate": _mean_metric(results, "asserted_facts_ok"),
        "cases_with_fact_assertions": sum(
            item.get("metrics", {}).get("asserted_facts_ok") is not None for item in results
        ),
        "rag_citation_resolution_rate": _mean_metric(results, "rag_citation_resolution_ok"),
        "rag_cited_fact_support_rate": _mean_metric(results, "rag_cited_fact_support"),
        "rag_expected_source_recall": _mean_metric(results, "rag_expected_source_recall"),
        "rag_abstention_accuracy": _mean_metric(results, "rag_abstention_ok"),
        "fact_coverage_ok_rate": _mean_metric(results, "fact_coverage_ok"),
        "fallback_rate": _mean_metric(results, "fallback"),
        "fallback_reason_counts": _reason_counts(results),
        "execution_status_counts": _execution_status_counts(results),
        "tool_status_counts": _tool_status_counts(results),
        "partial_success_rate": _mean_metric(results, "partial_success"),
        "transport_error_rate": _mean_metric(results, "transport_error"),
        "http_error_rate": _mean_metric(results, "http_error"),
        "expected_fallback_accuracy": _mean_metric(results, "expected_fallback_matched"),
        "sql_execution_success_rate": _mean_metric(results, "sql_execution_success"),
        "cypher_executable_rate": _mean_metric(results, "cypher_executable"),
        "rag_execution_success_rate": _mean_metric(results, "rag_execution_success"),
        "avg_latency_ms": _average(latencies),
        "p95_latency_ms": _percentile(latencies, 0.95),
        "avg_total_latency_ms": _average(latencies),
        "p95_total_latency_ms": _percentile(latencies, 0.95),
        "avg_latency_ms_first_attempt": _mean_metric(results, "latency_ms_first_attempt"),
        "avg_latency_ms_successful_attempt": _mean_metric(results, "latency_ms_successful_attempt"),
        "avg_retry_count": _mean_metric(results, "retry_count"),
        "avg_latency_ms_agent": _mean_metric(results, "latency_ms_agent"),
        "avg_latency_ms_router": _mean_metric(results, "latency_ms_router"),
        "avg_latency_ms_planner": _mean_metric(results, "latency_ms_planner"),
        "avg_latency_ms_tool": _mean_metric(results, "observed_duration_ms_tool"),
        "avg_observed_duration_ms_tool": _mean_metric(results, "observed_duration_ms_tool"),
        "avg_completed_latency_ms_tool": _mean_metric(results, "latency_ms_tool"),
        "avg_observed_work_ms_tool": _mean_metric(results, "observed_work_ms_tool"),
        "avg_latency_ms_answer": _mean_metric(results, "latency_ms_answer"),
        "avg_token_total": _mean_metric(results, "token_total"),
        "avg_token_cost": _mean_metric(results, "token_cost"),
        "trace_stage_counts": _stage_counts(results),
    }


def _event_presence(events: Sequence[Dict[str, Any]], stage: str, action: str) -> Optional[bool]:
    return None if not events else any(event.get("stage") == stage and event.get("action") == action for event in events)


def _diagnostic_counts(results: Sequence[Dict[str, Any]], key: str) -> Dict[str, int]:
    counts: Dict[str, int] = {}
    for result in results:
        values = result.get("diagnostics", {}).get(key, [])
        for value in [values] if isinstance(values, str) else values:
            counts[value] = counts.get(value, 0) + 1
    return dict(sorted(counts.items()))


def _metric_counts(results: Sequence[Dict[str, Any]]) -> Dict[str, Any]:
    """Boolean rates are case-level; assertion totals are reported separately."""
    names = (
        "route_correct", "tool_exact_match", "tool_non_error_completion", "tool_all_success",
        "execution_status_matched", "evidence_available_matched",
        "expected_fact_supported_by_evidence", "asserted_facts_ok", "expected_fallback_matched",
    )
    counts = {}
    for name in names:
        values = [row.get("metrics", {}).get(name) for row in results]
        counts[name] = {
            "unit": "case", "applicable": sum(isinstance(value, bool) for value in values),
            "passed": sum(value is True for value in values),
            "failed": sum(value is False for value in values),
            "not_applicable": sum(value is None for value in values),
        }
    return counts


def _http_success(
    response: Dict[str, Any], error: Optional[str], http_status: Optional[int]
) -> Optional[bool]:
    if isinstance(http_status, int):
        return 200 <= http_status < 300
    if error is not None:
        return False
    return True if response else None


def _agent_completed(
    response: Dict[str, Any], error: Optional[str], events: Sequence[Dict[str, Any]]
) -> Optional[bool]:
    if error is not None:
        return False
    completed = _event_presence(events, "agent_graph", "done")
    if completed is not None:
        return completed
    return True if response else None


def _execution_status(response: Dict[str, Any]) -> Optional[str]:
    execution = response.get("execution")
    if isinstance(execution, dict) and execution.get("status"):
        return _string(execution.get("status")).lower()
    statuses = _tool_statuses(response)
    if not statuses:
        return None
    if all(status == "success" for status in statuses):
        return "success"
    if all(status == "empty" for status in statuses):
        return "empty"
    if all(status == "error" for status in statuses):
        return "error"
    return "partial"


def _execution_timing(response: Dict[str, Any], key: str) -> Optional[float]:
    execution = response.get("execution")
    timings = execution.get("timings") if isinstance(execution, dict) else None
    value = timings.get(key) if isinstance(timings, dict) else None
    return float(value) if isinstance(value, (int, float)) else None


def _strict_tool_events(response: Dict[str, Any], tools: Set[str]) -> List[Dict[str, Any]]:
    return [event for event in _events(response) if event.get("stage") == "tool_execution" and event.get("action") == "done" and _string(event.get("tool") or event.get("tool_name")) in tools]


def _execution_success(response: Dict[str, Any], tools: Set[str]) -> Optional[bool]:
    if isinstance(tools, str):
        tools = {
            "sql": TEXT2SQL_TOOLS,
            "cypher": CYPHER_TOOLS,
            "rag": RAG_TOOLS,
        }.get(tools, {tools})
    execution = response.get("execution")
    if isinstance(execution, dict) and isinstance(execution.get("tool_results"), list):
        results = [item for item in execution["tool_results"] if isinstance(item, dict)]
        matching = [item for item in results if _string(item.get("tool_name")) in _canonical_tool_names(tools)]
        return None if not matching else all(item.get("status") in {"success", "empty"} for item in matching)
    events = _strict_tool_events(response, tools)
    return None if not events else all(_event_execution_success(event) is True for event in events)


def _tool_execution_ok(response: Dict[str, Any]) -> Optional[bool]:
    execution = response.get("execution")
    if isinstance(execution, dict) and isinstance(execution.get("tool_results"), list):
        results = [item for item in execution["tool_results"] if isinstance(item, dict)]
        return None if not results else all(item.get("status") in {"success", "empty"} for item in results)
    events = [event for event in _events(response) if event.get("stage") == "tool_execution" and event.get("action") == "done"]
    return None if not events else all(_event_execution_success(event) is True for event in events)


def _all_tools_success(response: Dict[str, Any]) -> Optional[bool]:
    statuses = _tool_statuses(response)
    return all(status == "success" for status in statuses) if statuses else None


def _completed_tool_duration(response: Dict[str, Any], events: Sequence[Dict[str, Any]]) -> Optional[float]:
    completed = _tool_latency(events)
    if completed is not None:
        return completed
    public_completed = _execution_timing(response, "tool_completed_sum_ms")
    if public_completed is not None:
        return public_completed
    # A public timeout duration is observable waiting, not completed execution.
    if _tool_execution_ok(response) is True:
        return _execution_timing(response, "tool_execution_ms")
    return None


def _event_execution_success(event: Dict[str, Any]) -> Optional[bool]:
    metadata = event.get("metadata")
    return metadata.get("execution_success") if isinstance(metadata, dict) and isinstance(metadata.get("execution_success"), bool) else None


def _tool_latency(events: Sequence[Dict[str, Any]]) -> Optional[float]:
    return interval_durations(stage_intervals(list(events), "tool_execution", completed_only=True)).get("sum_ms")


def _tool_observed_latency(events: Sequence[Dict[str, Any]]) -> Optional[float]:
    """Observed wall span, including error/timeout terminals; never fabricate ends."""
    return interval_durations(stage_intervals(list(events), "tool_execution")).get("wall_ms")


def _tool_observed_work(events: Sequence[Dict[str, Any]]) -> Optional[float]:
    return interval_durations(stage_intervals(list(events), "tool_execution")).get("sum_ms")


def _stage_latency(events: Sequence[Dict[str, Any]], start_stage: str, start_action: str, end_stage: str, end_action: str) -> Optional[float]:
    start = next((float(e["ts"]) for e in events if e.get("stage") == start_stage and e.get("action") == start_action and isinstance(e.get("ts"), (int, float))), None)
    if start is None:
        return None
    end = next((float(e["ts"]) for e in events if e.get("stage") == end_stage and e.get("action") == end_action and isinstance(e.get("ts"), (int, float)) and float(e["ts"]) >= start), None)
    return None if end is None else round((end - start) * 1000, 2)


def _tool_result_items(response: Dict[str, Any]) -> List[Dict[str, Any]]:
    execution = response.get("execution")
    if isinstance(execution, dict) and isinstance(execution.get("tool_results"), list):
        return [item for item in execution["tool_results"] if isinstance(item, dict)]
    items: List[Dict[str, Any]] = []
    seen: Set[str] = set()
    for node in _walk_dicts(response):
        candidates = node.get("tool_results")
        if isinstance(candidates, dict):
            candidates = [candidates]
        if isinstance(candidates, list):
            for item in candidates:
                if isinstance(item, dict):
                    _append_unique(items, seen, item)
        if node.get("tool_name") and ("status" in node or "evidence" in node):
            _append_unique(items, seen, node)
    return items


def _append_unique(items: List[Dict[str, Any]], seen: Set[str], item: Dict[str, Any]) -> None:
    marker = repr(sorted((key, repr(value)) for key, value in item.items()))
    if marker not in seen:
        seen.add(marker)
        items.append(item)


def _walk_dicts(value: Any) -> Iterable[Dict[str, Any]]:
    if isinstance(value, dict):
        yield value
        for child in value.values():
            yield from _walk_dicts(child)
    elif isinstance(value, list):
        for child in value:
            yield from _walk_dicts(child)


def _evidence_available(response: Dict[str, Any]) -> Optional[bool]:
    execution = response.get("execution")
    if isinstance(execution, dict) and isinstance(execution.get("evidence"), list):
        return bool(execution["evidence"])
    results = _tool_result_items(response)
    if not results:
        return None
    count = 0
    for result in results:
        evidence = result.get("evidence")
        count += len(evidence) if isinstance(evidence, list) else int(isinstance(evidence, dict))
    return count > 0


def _expected_fact_supported_by_evidence(
    response: Dict[str, Any], expected_facts: Sequence[str]
) -> Optional[bool]:
    """Legacy all-facts substring check over merged Evidence content.

    It does not verify source, same-row associations, citations or entailment.
    Keep historical scores stable; use declared assertions for stricter checks.
    """
    facts = [fact for fact in expected_facts if _string(fact).strip()]
    if not facts:
        return None
    execution = response.get("execution")
    evidence_items: List[Dict[str, Any]] = []
    if isinstance(execution, dict) and isinstance(execution.get("evidence"), list):
        evidence_items = [item for item in execution["evidence"] if isinstance(item, dict)]
    if not evidence_items:
        for result in _tool_result_items(response):
            evidence = result.get("evidence")
            if isinstance(evidence, list):
                evidence_items.extend(item for item in evidence if isinstance(item, dict))
    if not evidence_items:
        return False
    corpus_parts: List[str] = []
    for item in evidence_items:
        content = item.get("content")
        corpus_parts.append(str(content) if isinstance(content, (dict, list, tuple)) else _string(content))
    corpus = " ".join(corpus_parts).lower()
    return all(_string(fact).lower() in corpus for fact in facts)


def _evidence_count(response: Dict[str, Any]) -> int:
    execution = response.get("execution")
    if isinstance(execution, dict) and isinstance(execution.get("evidence"), list):
        return len(execution["evidence"])
    count = 0
    for result in _tool_result_items(response):
        evidence = result.get("evidence")
        count += len(evidence) if isinstance(evidence, list) else 0
    return count


def _citation_count(response: Dict[str, Any]) -> int:
    execution = response.get("execution")
    citations = execution.get("citation_ids") if isinstance(execution, dict) else None
    return len(citations) if isinstance(citations, list) else 0


def _error_categories(response: Dict[str, Any]) -> List[str]:
    execution = response.get("execution")
    results = execution.get("tool_results") if isinstance(execution, dict) else None
    values = []
    for item in results if isinstance(results, list) else []:
        if isinstance(item, dict) and item.get("error_category"):
            values.append(_string(item["error_category"]))
    return sorted(set(values))


def _statements(response: Dict[str, Any]) -> List[Dict[str, str]]:
    execution = response.get("execution")
    results = execution.get("tool_results") if isinstance(execution, dict) else None
    statements: List[Dict[str, str]] = []
    for item in results if isinstance(results, list) else []:
        if not isinstance(item, dict):
            continue
        statement = item.get("statement")
        if isinstance(statement, dict) and statement.get("text"):
            statements.append(
                {
                    "tool_name": _string(item.get("tool_name")),
                    "language": _string(statement.get("language")),
                    "text": _string(statement.get("text")),
                }
            )
    return statements


def _tool_statuses(response: Dict[str, Any]) -> List[str]:
    # The public execution contract is authoritative when present. Trace events
    # mirror the same result at tool_execution/tool_result stages, so mixing both
    # sources double- or triple-counts a single tool invocation in diagnostics.
    execution = response.get("execution")
    if isinstance(execution, dict) and isinstance(execution.get("tool_results"), list):
        return [
            _string(result.get("status")).lower()
            for result in execution["tool_results"]
            if isinstance(result, dict) and result.get("status")
        ]

    statuses = [
        _string(result.get("status")).lower()
        for result in _tool_result_items(response)
        if result.get("status")
    ]
    if statuses:
        return statuses

    # Legacy fallback: prefer one trace stage, not both mirrored stages.
    for stage in ("tool_result", "tool_execution"):
        event_statuses: List[str] = []
        for event in _events(response):
            if event.get("stage") != stage:
                continue
            metadata = event.get("metadata")
            if isinstance(metadata, dict) and metadata.get("status"):
                event_statuses.append(_string(metadata["status"]).lower())
        if event_statuses:
            return event_statuses
    return []


def _fallback_reason(response: Dict[str, Any], error: Optional[str]) -> str:
    """Return only a real alternate-strategy fallback reason.

    Tool errors, empty results, route errors, and transport failures are outcomes,
    not fallbacks.  They are reported separately by execution status and
    transport/error diagnostics.
    """
    execution = response.get("execution")
    if isinstance(execution, dict):
        reason = _string(execution.get("fallback_reason")).strip()
        if reason and reason != "none":
            return reason
    trace = _trace(response)
    reason = _string(trace.get("fallback_reason")).strip()
    if reason and reason != "none":
        return reason
    if trace.get("fallback") is True:
        return "unknown"
    return "none"


def _is_fallback(response: Dict[str, Any], error: Optional[str]) -> bool:
    """Backward-compatible boolean view of the structured fallback reason."""
    return _fallback_reason(response, error) != "none"


def _fact_coverage(answer: str, expected_facts: Sequence[str]) -> Optional[float]:
    if not expected_facts:
        return None
    return sum(1 for fact in expected_facts if _contains(answer, fact)) / len(expected_facts)


def _tool_subset_metric(expected: Sequence[str], actual: Sequence[str]) -> Optional[bool]:
    if not expected:
        return None
    return bool(actual) and set(expected).issubset(set(actual))


def _tool_exact_metric(expected: Sequence[str], actual: Sequence[str]) -> Optional[bool]:
    return None if not expected else set(expected) == set(actual)


def _stage_counts(results: Sequence[Dict[str, Any]]) -> Dict[str, int]:
    counts: Dict[str, int] = {}
    for item in results:
        for stage in item.get("actual", {}).get("trace_stages", []) or []:
            counts[stage] = counts.get(stage, 0) + 1
    return dict(sorted(counts.items()))


def _reason_counts(results: Sequence[Dict[str, Any]]) -> Dict[str, int]:
    counts: Dict[str, int] = {}
    for item in results:
        reason = item.get("fallback_reason") or "unknown"
        counts[reason] = counts.get(reason, 0) + 1
    return dict(sorted(counts.items()))


def _execution_status_counts(results: Sequence[Dict[str, Any]]) -> Dict[str, int]:
    counts: Dict[str, int] = {}
    for item in results:
        status = item.get("actual", {}).get("execution_status") or "unknown"
        counts[status] = counts.get(status, 0) + 1
    return dict(sorted(counts.items()))


def _tool_status_counts(results: Sequence[Dict[str, Any]]) -> Dict[str, int]:
    counts: Dict[str, int] = {}
    for item in results:
        for status in item.get("actual", {}).get("tool_statuses", []) or []:
            counts[status] = counts.get(status, 0) + 1
    return dict(sorted(counts.items()))


def _events(response: Dict[str, Any]) -> List[Dict[str, Any]]:
    raw = _trace(response).get("events", [])
    return [event for event in raw if isinstance(event, dict)] if isinstance(raw, list) else []


def _trace(response: Dict[str, Any]) -> Dict[str, Any]:
    metadata = response.get("metadata")
    trace = metadata.get("eval_trace") if isinstance(metadata, dict) else None
    return trace if isinstance(trace, dict) else {}


def _selected_tools(response: Dict[str, Any]) -> List[str]:
    tools: List[str] = []
    trace = _trace(response)
    for key in ("selected_tools", "tools", "tool_names"):
        tools.extend(_strings(trace.get(key)))
    for event in _events(response):
        if event.get("stage") != "tool_selection":
            continue
        tools.extend(_strings(event.get("tools")))
        for key in ("tool", "tool_name", "selected_tool"):
            if event.get(key):
                tools.append(_string(event[key]))
    if not tools:
        execution = response.get("execution")
        if isinstance(execution, dict):
            tools.extend(
                _string(item.get("tool_name"))
                for item in execution.get("tool_results", [])
                if isinstance(item, dict) and item.get("tool_name")
            )
    return _canonical_tool_names(tool for tool in tools if tool)


def _executed_tools(response: Dict[str, Any]) -> List[str]:
    execution = response.get("execution")
    if isinstance(execution, dict) and isinstance(execution.get("tool_results"), list):
        return _canonical_tool_names(
            _string(item.get("tool_name"))
            for item in execution["tool_results"]
            if isinstance(item, dict) and item.get("tool_name")
        )
    return _canonical_tool_names(
        _string(event.get("tool") or event.get("tool_name"))
        for event in _events(response)
        if event.get("stage") == "tool_execution"
        and event.get("action") == "done"
        and (event.get("tool") or event.get("tool_name"))
    )


def _canonical_tool_names(names: Iterable[str]) -> List[str]:
    canonical = []
    for name in names:
        normalized = _string(name).strip().lower().replace("-", "_")
        if not normalized:
            continue
        canonical.append(TOOL_NAME_ALIASES.get(normalized, normalized))
    return sorted(set(canonical))


def _source_identifiers(response: Dict[str, Any]) -> Set[str]:
    identifiers: Set[str] = set()
    sources = response.get("sources")
    keys = ("id", "source_id", "document_id", "doc_id", "recipe_id", "kb_id", "title", "name", "source")
    for source in sources if isinstance(sources, list) else []:
        if isinstance(source, str):
            identifiers.add(source)
        elif isinstance(source, dict):
            for key in keys:
                if source.get(key) is not None:
                    identifiers.add(_string(source[key]))
            metadata = source.get("metadata")
            if isinstance(metadata, dict):
                for key in keys:
                    if metadata.get(key) is not None:
                        identifiers.add(_string(metadata[key]))
    identifiers.update(_evidence_source_identifiers(response))
    return identifiers


def _evidence_source_identifiers(response: Dict[str, Any]) -> Set[str]:
    """Collect stable source values from canonical ToolResult evidence."""
    identifiers: Set[str] = set()
    execution = response.get("execution")
    if isinstance(execution, dict) and isinstance(execution.get("evidence"), list):
        for item in execution["evidence"]:
            if not isinstance(item, dict):
                continue
            for key in ("source_id", "source"):
                value = item.get(key)
                if value is not None and _string(value).strip():
                    identifiers.add(_string(value))
            metadata = item.get("metadata")
            if isinstance(metadata, dict):
                for key in ("document_source_id", "title", "source_table", "query_name", "strategy"):
                    value = metadata.get(key)
                    if value is not None and _string(value).strip():
                        identifiers.add(_string(value))
        return identifiers
    for result in _tool_result_items(response):
        evidence = result.get("evidence")
        if not isinstance(evidence, list):
            continue
        for item in evidence:
            if not isinstance(item, dict):
                continue
            for key in ("source_id", "source"):
                value = item.get(key)
                if value is not None and _string(value).strip():
                    identifiers.add(_string(value))
            metadata = item.get("metadata")
            if isinstance(metadata, dict):
                for key in ("source_id", "source", "title", "name", "document_id", "source_table"):
                    value = metadata.get(key)
                    if value is not None and _string(value).strip():
                        identifiers.add(_string(value))
    return identifiers


def _first_attempt_latency(attempts: Optional[Sequence[Dict[str, Any]]]) -> Optional[float]:
    if not attempts or not isinstance(attempts[0].get("latency_ms"), (int, float)):
        return None
    return float(attempts[0]["latency_ms"])


def _successful_attempt_latency(attempts: Optional[Sequence[Dict[str, Any]]]) -> Optional[float]:
    for attempt in attempts or []:
        if attempt.get("success") and isinstance(attempt.get("latency_ms"), (int, float)):
            return float(attempt["latency_ms"])
    return None


def _token_total(response: Dict[str, Any]) -> Optional[int]:
    if _trace(response).get("llm_usage_complete") is False:
        return None
    candidates = [_trace(response).get("token_usage"), _trace(response).get("usage"), response.get("usage")]
    metadata = response.get("metadata")
    if isinstance(metadata, dict):
        candidates += [metadata.get("usage"), metadata.get("token_usage")]
    for usage in candidates:
        if isinstance(usage, dict):
            for key in ("total_tokens", "total", "tokens"):
                if isinstance(usage.get(key), int):
                    return usage[key]
    return None


def _llm_observations(trace: Dict[str, Any]) -> List[Dict[str, Any]]:
    keys = {"call_id", "node", "model", "response_model", "status", "duration_ms", "observed_duration_ms", "first_token_ms",
            "sdk_retry_count", "callback_retry_count", "usage", "error_code", "error_chain"}
    calls = []
    for call in trace.get("llm_calls", []):
        if not isinstance(call, dict):
            continue
        result = {key: value for key, value in call.items() if key in keys}
        if "error_chain" in result:
            chain = result["error_chain"]
            result["error_chain"] = [
                {key: value for key, value in item.items()
                 if (key == "class" and isinstance(value, str) and re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]{0,119}", value))
                 or (key in {"errno", "winerror", "verify_code"} and isinstance(value, int) and not isinstance(value, bool))}
                for item in (chain[:5] if isinstance(chain, list) else []) if isinstance(item, dict)
            ]
        calls.append(result)
    return calls


def _token_cost(response: Dict[str, Any]) -> Optional[float]:
    for key in ("token_cost", "cost_usd", "total_cost"):
        value = _trace(response).get(key)
        if isinstance(value, (int, float)):
            return float(value)
    return None


def _recall(expected: Iterable[str], actual: Iterable[str]) -> float:
    expected_set = {_normalize(value) for value in expected if value}
    actual_values = {_normalize(value) for value in actual if value}
    if not expected_set:
        return 0.0
    return len(expected_set & actual_values) / len(expected_set)


def _contains(text: str, needle: str) -> bool:
    return _normalize(needle) in _normalize(text)


def _normalize(value: str) -> str:
    return _string(value).casefold().strip()


def _string(value: Any) -> str:
    return "" if value is None else str(value)


def _strings(value: Any) -> List[str]:
    if value is None:
        return []
    if isinstance(value, (list, tuple, set)):
        return [_string(item) for item in value if item is not None]
    return [_string(value)]


def _number_or_none(value: Any) -> Optional[float]:
    return float(value) if isinstance(value, (int, float)) else None


def _mean_metric(results: Sequence[Dict[str, Any]], key: str) -> Optional[float]:
    values: List[float] = []
    for item in results:
        value = (item.get("metrics") or {}).get(key)
        if isinstance(value, bool):
            values.append(1.0 if value else 0.0)
        elif isinstance(value, (int, float)):
            values.append(float(value))
    return _average(values)


def _average(values: Sequence[float]) -> Optional[float]:
    return None if not values else round(sum(values) / len(values), 4)


def _percentile(values: Sequence[float], percentile: float) -> Optional[float]:
    if not values:
        return None
    ordered = sorted(values)
    index = max(0, min(len(ordered) - 1, math.ceil(percentile * len(ordered)) - 1))
    return round(ordered[index], 2)
