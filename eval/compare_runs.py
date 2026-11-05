"""Compare saved chat Runner outputs; no model calls or runtime reconfiguration."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from eval.evaluators.metrics import summarize_results


def compare_runs(a_runs: list[Path], b_runs: list[Path]) -> dict:
    if len(a_runs) < 2 or len(a_runs) != len(b_runs):
        raise ValueError("Use the same number of complete runs per model, at least two")
    paths = [p.resolve() for p in a_runs + b_runs]
    if len(paths) != len(set(paths)):
        raise ValueError("A run cannot be reused to inflate the number of observations")
    signature = None
    models = []
    result = {"scope": "Small paired comparison, not a production latency/SLA or generalization claim.", "variants": {}}
    grouped = []
    for label, runs in (("a", a_runs), ("b", b_runs)):
        rows, observed_models, unknown = [], set(), []
        for path in runs:
            environment = json.loads((path / "environment.json").read_text(encoding="utf-8"))
            current = [json.loads(line) for line in (path / "cases.jsonl").read_text(encoding="utf-8").splitlines() if line.strip()]
            ids = [r["id"] for r in current]
            if len(ids) != environment.get("case_count") or len(ids) != len(set(ids)) or not ids:
                raise ValueError("Incomplete or duplicate case list")
            if environment.get("snapshot_unchanged") is not True:
                raise ValueError("Each run must have a verified unchanged source/input snapshot")
            identity = (environment.get("dataset_sha256"), environment.get("source_snapshot", {}).get("sha256"), tuple(sorted(ids)))
            if not all(identity[:2]) or (signature is not None and identity != signature):
                raise ValueError("Compare the same dataset, selected cases and source snapshot")
            signature = identity
            for row in current:
                calls = row.get("actual", {}).get("llm_calls", [])
                known = {c["model"] for c in calls if c.get("model") not in {None, "unknown"}}
                observed_models.update(known)
                if not known:
                    unknown.append({"run": path.name, "id": row["id"]})
            rows.extend(current)
        if len(observed_models) != 1:
            raise ValueError("Each side requires one actually observed model; labels alone are not verification")
        model = next(iter(observed_models))
        models.append(model)
        result["variants"][label] = {
            "model": model, "runs": len(runs), "observations": len(rows),
            "unattributed_cases": unknown, "summary": summarize_results(rows),
        }
        grouped.append(rows)
    if models[0] == models[1]:
        raise ValueError("A/B must use different observed models")
    result["paired_cases"] = []
    for case_id in signature[2]:
        sides = [[r for r in rows if r["id"] == case_id] for rows in grouped]
        result["paired_cases"].append({"id": case_id, **{
            label: {"total_latency_ms": [r["metrics"]["latency_ms_total"] for r in rows],
                    "execution_status": [r["actual"].get("execution_status") for r in rows],
                    "failure_reasons": [r.get("diagnostics", {}).get("failure_reasons", []) for r in rows]}
            for label, rows in zip(("a", "b"), sides)}})
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--a-run", type=Path, action="append", required=True)
    parser.add_argument("--b-run", type=Path, action="append", required=True)
    args = parser.parse_args()
    print(json.dumps(compare_runs(args.a_run, args.b_run), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
