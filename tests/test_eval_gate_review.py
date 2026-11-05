"""Gate review and recoverable snapshots need no LLM, database or network."""

import argparse
import hashlib
import json
import zipfile

import pytest

from eval.evaluators.fact_assertions import evaluate_fact_assertions
from eval.evaluators.metrics import evaluate_case, summarize_results
from eval.evaluators.regression import regression_gate, validate_error_expectations
from eval.run_eval import archive_source_snapshot, collect_source_snapshot, review_saved_run


def result(*, status="success", fallback=False, expected_fallback=None, allow=False, tools=True):
    case = {"id": "sample", "expected_tools": ["sql_agent"] if tools else [],
            "expected_execution_status": status, "allow_fallback": allow}
    if expected_fallback is not None:
        case["expected_fallback"] = expected_fallback
    response = {"message": "answer", "route": "data-query", "execution": {
        "status": status, "tool_results": [{"tool_name": "sql_agent", "status": status}] if tools else [],
        "evidence": [], "fallback_reason": "router_timeout" if fallback else "none",
    }}
    return evaluate_case(case, response, 100, http_status=200)


def test_strict_success_and_nonerror_completion_have_explicit_case_denominators():
    summary = summarize_results([result(), result(status="empty"), result(status="error"), result(tools=False)])
    assert summary["tool_execution_success_rate"] == summary["tool_non_error_completion_rate"] == 0.6667
    assert summary["tool_all_success_rate"] == 0.3333
    assert summary["metric_counts"]["tool_all_success"] == {
        "unit": "case", "applicable": 3, "passed": 1, "failed": 2, "not_applicable": 1,
    }


def test_evidence_support_case_count_and_assertion_count_are_different_units():
    case = {"expected_key_facts": ["7", "days"], "fact_assertions": [
        {"id": "one", "kind": "answer_fact", "any_of": ["7"]},
        {"id": "two", "kind": "evidence_fact", "source": "pgvector", "any_of": ["days"]},
    ]}
    row = evaluate_case(case, {"message": "7 days", "execution": {
        "evidence": [{"source": "pgvector", "content": "7 days"}],
    }}, 1)
    summary = summarize_results([row, evaluate_case({}, {}, 1)])
    assert summary["metric_counts"]["expected_fact_supported_by_evidence"]["applicable"] == 1
    assert summary["fact_assertion_counts"] == {"total": 2, "passed": 2}


@pytest.mark.parametrize("expected,allowed,actual,status", [
    (False, False, True, "FAIL"),
    (True, False, False, "FAIL"),
    (True, False, True, "WARN"),
    (None, False, True, "FAIL"),
    (None, True, True, "WARN"),
    (None, True, False, "PASS"),
    (False, False, False, "PASS"),
])
def test_fallback_expectation_and_permission_are_not_the_same(expected, allowed, actual, status):
    row = result(fallback=actual, expected_fallback=expected, allow=allowed)
    gate = regression_gate([row], True)
    assert row["diagnostics"]["check_status"] == status
    assert gate["status"] == status and gate["passed"] is (status != "FAIL")


def test_ambiguous_fallback_declarations_fail_fast():
    with pytest.raises(ValueError, match="contradicts"):
        validate_error_expectations({"allow_fallback": True, "expected_fallback": False})
    with pytest.raises(ValueError, match="boolean"):
        validate_error_expectations({"allow_fallback": "yes"})


def test_undeclared_tool_expectation_does_not_invent_a_failure():
    row = evaluate_case({}, {"message": "answer", "execution": {
        "status": "success", "tool_results": [{"tool_name": "sql_agent", "status": "success"}],
    }}, 1, http_status=200)
    assert not row["diagnostics"]["failure_reasons"]


def test_empty_run_cannot_pass_gate():
    assert regression_gate([], True)["status"] == "FAIL"


def test_http_server_error_is_not_transport_error():
    row = evaluate_case({}, None, 100, error="HTTP 503", http_status=503)
    assert row["metrics"]["http_error"] is True
    assert row["metrics"]["transport_error"] is False
    transport = evaluate_case({}, None, 100, error="TimeoutError", http_status=None)
    assert transport["metrics"]["transport_error"] is True
    assert transport["metrics"]["http_error"] is False


def source_fixture(tmp_path):
    root = tmp_path / "source"
    for name in ["multisource_data_agent/main.py", "tests/test_sample.py", "eval/datasets/sample.jsonl"]:
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('"reviewed"\n', encoding="utf-8")
    (root / ".env").write_text("private-placeholder", encoding="utf-8")
    (root / "eval/results").mkdir()
    (root / "eval/results/debug.py").write_text("unreviewed", encoding="utf-8")
    return root


def test_archive_restores_exact_source_test_and_dataset_bytes_without_secrets(tmp_path):
    root = source_fixture(tmp_path)
    snapshot = collect_source_snapshot(root)
    assert set(snapshot["files"]) == {
        "multisource_data_agent/main.py", "tests/test_sample.py", "eval/datasets/sample.jsonl",
    }
    run = tmp_path / "run"
    run.mkdir()
    record = archive_source_snapshot(run, snapshot, root)
    archive = run / record["path"]
    assert hashlib.sha256(archive.read_bytes()).hexdigest() == record["sha256"]
    with zipfile.ZipFile(archive) as saved:
        assert ".env" not in saved.namelist() and "eval/results/debug.py" not in saved.namelist()
        for name, digest in snapshot["files"].items():
            assert saved.read(name) == (root / name).read_bytes()
            assert hashlib.sha256(saved.read(name)).hexdigest() == digest
        assert json.loads(saved.read("source_manifest.json")) == snapshot


def test_test_changes_affect_snapshot_but_environment_changes_do_not(tmp_path):
    root = source_fixture(tmp_path)
    first = collect_source_snapshot(root)
    (root / ".env").write_text("changed-private-placeholder", encoding="utf-8")
    assert collect_source_snapshot(root) == first
    (root / "tests/test_sample.py").write_text('"changed"', encoding="utf-8")
    assert collect_source_snapshot(root)["sha256"] != first["sha256"]


def test_archive_rejects_credentials_and_source_drift(tmp_path):
    root = source_fixture(tmp_path)
    run = tmp_path / "run"
    run.mkdir()
    with pytest.raises(ValueError, match="approved"):
        archive_source_snapshot(run, {"files": {".env": "fake"}}, root)
    snapshot = collect_source_snapshot(root)
    (root / "multisource_data_agent/main.py").write_text("changed", encoding="utf-8")
    with pytest.raises(ValueError, match="Source changed"):
        archive_source_snapshot(run, snapshot, root)


def saved_run(tmp_path):
    source = tmp_path / "original"
    source.mkdir()
    row = result(fallback=True, expected_fallback=False)
    # Simulate the old permissive gate, not a new HTTP measurement.
    row["diagnostics"] = {"failure_reasons": [], "warnings": ["fallback:router_timeout"]}
    (source / "cases.jsonl").write_text(json.dumps(row) + "\n", encoding="utf-8")
    (source / "summary.json").write_text(json.dumps({"regression_gate": {"passed": True}}), encoding="utf-8")
    (source / "environment.json").write_text(json.dumps({
        "case_count": 1, "selected_case_ids": ["sample"], "snapshot_unchanged": True,
        "source_snapshot": {"sha256": "original-source"},
    }), encoding="utf-8")
    return source


def test_offline_review_preserves_originals_and_exposes_unexpected_fallback(tmp_path, monkeypatch):
    from eval import run_eval

    source = saved_run(tmp_path)
    before = {p.name: p.read_bytes() for p in source.iterdir()}
    monkeypatch.setattr(run_eval, "collect_source_snapshot", lambda: {"sha256": "new-review-source"})
    monkeypatch.setattr(run_eval, "archive_source_snapshot", lambda *args: {"path": "mock-source.zip"})

    def forbidden(*args, **kwargs):
        raise AssertionError("Offline review must not contact the backend")

    monkeypatch.setattr(run_eval, "call_chat_api", forbidden)
    monkeypatch.setattr(run_eval, "wait_for_backend_ready", forbidden)
    args = argparse.Namespace(fail_on_regression=True, min_route_accuracy=None, max_p95_latency_ms=None)
    assert review_saved_run(source, tmp_path / "reviews", args) == 1
    assert before == {p.name: p.read_bytes() for p in source.iterdir()}
    review = json.loads(next((tmp_path / "reviews").glob("*/review.json")).read_text(encoding="utf-8"))
    assert review["review_kind"] == "gate_only_no_http_requests"
    assert review["summary"]["regression_gate"]["status"] == "FAIL"
    assert "unexpected_fallback" in review["cases"][0]["failure_reasons"]
    assert review["original_source_sha256"] != review["review_source_snapshot"]["sha256"]


def test_incomplete_saved_run_and_output_inside_original_are_rejected(tmp_path):
    source = saved_run(tmp_path)
    args = argparse.Namespace(fail_on_regression=True, min_route_accuracy=None, max_p95_latency_ms=None)
    with pytest.raises(ValueError, match="outside"):
        review_saved_run(source, source / "review", args)
    (source / "cases.jsonl").write_text("", encoding="utf-8")
    with pytest.raises(ValueError, match="incomplete"):
        review_saved_run(source, tmp_path / "review", args)


def test_gate_flag_is_not_silently_ignored_in_retrieval_mode(monkeypatch):
    from eval import run_eval

    monkeypatch.setattr("sys.argv", ["run_eval", "--mode", "retrieval", "--fail-on-regression"])
    with pytest.raises(SystemExit) as exc:
        run_eval.main()
    assert exc.value.code == 2


def test_condition_assertion_detects_missing_scope_without_a_new_judge():
    checks = [
        {"id": "sla", "kind": "answer_fact", "any_of": ["48小时", "48 hours"]},
        {"id": "scope", "kind": "answer_fact", "any_of": ["现货采购订单", "in-stock purchase orders"]},
    ]
    missing = evaluate_fact_assertions(checks, {"message": "NorthStar的SLA是48小时。"})
    assert [row["passed"] for row in missing] == [True, False]
    complete = evaluate_fact_assertions(checks, {"message": "NorthStar现货采购订单的SLA是48小时。"})
    assert all(row["passed"] for row in complete)


@pytest.mark.parametrize("answer", ["并非7天内可以退货。", "期限是7天，也可以无条件延长到60天。"])
def test_literal_assertion_documents_negation_and_extra_claim_blind_spots(answer):
    # Matcher-scope test, NOT a judgment that either answer is correct.
    check = {"id": "window", "kind": "answer_fact", "any_of": ["7天"]}
    assert evaluate_fact_assertions([check], {"message": answer})[0]["passed"] is True
