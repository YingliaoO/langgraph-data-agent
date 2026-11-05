import json

import pytest

from eval.compare_runs import compare_runs
from eval.evaluators.metrics import evaluate_case


def run(tmp_path, name, model, latency=100, error=False):
    path = tmp_path / name
    path.mkdir()
    row = evaluate_case({}, {"message": "ok", "execution": {"status": "error" if error else "success"}}, latency, http_status=200)
    row["id"] = "fixed-question"
    row["actual"]["llm_calls"] = [{"model": model}]
    (path / "cases.jsonl").write_text(json.dumps(row) + "\n", encoding="utf-8")
    (path / "environment.json").write_text(json.dumps({"case_count": 1, "dataset_sha256": "same-input",
        "source_snapshot": {"sha256": "same-code"}, "snapshot_unchanged": True}), encoding="utf-8")
    return path


def test_comparison_keeps_slow_and_failed_observations(tmp_path):
    a = [run(tmp_path, "a1", "model-a"), run(tmp_path, "a2", "model-a", 1000, error=True)]
    b = [run(tmp_path, "b1", "model-b", 50), run(tmp_path, "b2", "model-b", 200)]
    result = compare_runs(a, b)
    assert result["variants"]["a"]["summary"]["avg_total_latency_ms"] == 550
    assert result["variants"]["a"]["summary"]["regression_failure_cases"] == 1
    assert result["paired_cases"][0]["a"]["total_latency_ms"] == [100, 1000]


def test_comparison_rejects_reused_runs_and_different_inputs(tmp_path):
    a = [run(tmp_path, "a1", "model-a"), run(tmp_path, "a2", "model-a")]
    b = [run(tmp_path, "b1", "model-b"), run(tmp_path, "b2", "model-b")]
    with pytest.raises(ValueError, match="reused"):
        compare_runs([a[0], a[0]], b)
    environment = b[1] / "environment.json"
    data = json.loads(environment.read_text())
    data["dataset_sha256"] = "changed"
    environment.write_text(json.dumps(data))
    with pytest.raises(ValueError, match="same dataset"):
        compare_runs(a, b)


def test_manual_model_label_cannot_replace_observed_model(tmp_path):
    a = [run(tmp_path, "a1", "unknown"), run(tmp_path, "a2", "unknown")]
    b = [run(tmp_path, "b1", "model-b"), run(tmp_path, "b2", "model-b")]
    with pytest.raises(ValueError, match="observed model"):
        compare_runs(a, b)
