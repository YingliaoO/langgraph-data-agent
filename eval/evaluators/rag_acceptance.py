"""Offline acceptance calibration from one frozen retrieval run per query.

Uses the existing Eval schema/runner and never changes runtime configuration.
Threshold selection uses dev only; test is a release check, not tuning input.
"""

import datetime as dt
import hashlib
import json
from math import isfinite
from pathlib import Path

from .rag_quality import validate_rag_case
from .rag_retrieval import _search

BASELINE_THRESHOLD = 0.8
THRESHOLDS = (0.6, 0.65, 0.7, 0.75, 0.8, 0.85, 0.9)
COSINE_THRESHOLD = 0.5


def _finite(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and isfinite(value)


def score_acceptance(rows, threshold):
    tp = fp = fn = unanswered_admitted = answerable_complete = 0
    for row in rows:
        expected = set(row["expected_sources"])
        accepted = {d["source_id"] for d in row["scored_documents"]
                    if d["similarity"] >= COSINE_THRESHOLD and d["rerank_score"] >= threshold}
        tp += len(expected & accepted)
        fp += len(accepted - expected)
        fn += len(expected - accepted)
        unanswered_admitted += int(not expected and bool(accepted))
        answerable_complete += int(bool(expected) and expected <= accepted)
    precision = tp / (tp + fp) if tp + fp else 0.0
    recall = tp / (tp + fn) if tp + fn else 0.0
    return {"threshold": threshold, "document_precision": precision, "document_recall": recall,
            "document_f1": 2 * precision * recall / (precision + recall) if precision + recall else 0.0,
            "tp": tp, "fp": fp, "fn": fn, "answerable_complete": answerable_complete,
            "answerable_count": sum(bool(r["expected_sources"]) for r in rows),
            "unanswerable_admitted": unanswered_admitted,
            "unanswerable_count": sum(not r["expected_sources"] for r in rows)}


def select_threshold(dev_rows):
    if not dev_rows or not any(r["expected_sources"] for r in dev_rows) or not any(not r["expected_sources"] for r in dev_rows):
        raise ValueError("Calibration needs both answerable and unanswerable dev cases")
    baseline = score_acceptance(dev_rows, BASELINE_THRESHOLD)
    curve = [score_acceptance(dev_rows, threshold) for threshold in THRESHOLDS]
    eligible = [p for p in curve if p["unanswerable_admitted"] <= baseline["unanswerable_admitted"]]
    # Maximum document F1; ties prefer the stricter threshold. No test input.
    return max(eligible, key=lambda p: (p["document_f1"], p["threshold"])), curve


def run_acceptance_calibration(args, cases, dataset_path):
    from eval.run_eval import collect_environment_metadata, write_json, write_jsonl

    if args.retry_count or args.top_k != 5 or args.candidate_k != 20 or args.split:
        raise ValueError("Fixed calibration requires top_k=5, candidate_k=20, retry-count=0 and both splits")
    for case in cases:
        validate_rag_case(case)
    if {c["rag_eval"]["split"] for c in cases} != {"dev", "test"}:
        raise ValueError("Both frozen dev/test splits are required")
    identity = cases[0]["rag_eval"]
    if any((c["rag_eval"]["domain"], c["rag_eval"]["dataset_id"]) != (identity["domain"], identity["dataset_id"]) for c in cases):
        raise ValueError("Calibration must use one fixed corpus")
    corpus = args.corpus or Path(__file__).resolve().parents[1] / "fixtures/rag_documents_v1.jsonl"
    docs = [json.loads(line) for line in corpus.read_text(encoding="utf-8").splitlines() if line.strip()]
    expected_hashes = {d["source_id"]: hashlib.sha256(d["content"].encode()).hexdigest() for d in docs}
    if any((d["domain"], d["dataset_id"]) != (identity["domain"], identity["dataset_id"]) for d in docs):
        raise ValueError("Corpus namespace differs from cases")
    if any(not set(c["expected_sources"]) <= set(expected_hashes) for c in cases):
        raise ValueError("Expected source not present in corpus")
    probe = _search(args.retrieval_base_url, dict(query="Corpus integrity check", top_k=100, candidate_k=100,
                    rerank=False, threshold=None, domain=identity["domain"], dataset_id=identity["dataset_id"]), args.timeout)
    if not probe["http_success"] or probe.get("content_hashes") != expected_hashes:
        raise ValueError("Fixed corpus unavailable/incomplete")
    environment = collect_environment_metadata(dataset_path=dataset_path, base_url=args.retrieval_base_url,
        timeout=args.timeout, retry_count=0, case_count=len(cases), model_label=args.model_label, provider_label=args.provider_label)
    environment.update(mode="acceptance_calibration", corpus_sha256=hashlib.sha256(corpus.read_bytes()).hexdigest(),
                       thresholds=list(THRESHOLDS), baseline_threshold=BASELINE_THRESHOLD,
                       cosine_threshold=COSINE_THRESHOLD, candidate_k=20, top_k=5)
    previous = {}
    resume = getattr(args, "acceptance_resume", None)
    if resume:
        parent = json.loads((resume / "environment.json").read_text(encoding="utf-8"))
        for key in ("dataset_sha256", "corpus_sha256", "thresholds", "baseline_threshold", "cosine_threshold", "candidate_k", "top_k", "model_label", "provider_label"):
            if parent.get(key) != environment.get(key):
                raise ValueError(f"Resume input/config mismatch: {key}")
        for key, digest in parent.get("source_snapshot", {}).get("files", {}).items():
            if key.startswith(("multisource_data_agent/", "kb_ingest/kb_service/")) and environment["source_snapshot"]["files"].get(key) != digest:
                raise ValueError("Runtime source changed since the calibration run")
        parent_bytes = (resume / "cases.jsonl").read_bytes()
        previous = {r["id"]: r for r in map(json.loads, parent_bytes.decode("utf-8").splitlines())}
        if set(previous) != {c["id"] for c in cases}:
            raise ValueError("Resume requires the same complete case list")
        environment["resumed_from"] = {"run": resume.name, "cases_sha256": hashlib.sha256(parent_bytes).hexdigest(),
                                       "rule": "Reuse valid rows unchanged; measure only missing rerank scores once"}
    run_dir = Path(args.output_dir) / dt.datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    run_dir.mkdir(parents=True)
    write_json(run_dir / "environment.json", environment)
    rows = []
    for index, case in enumerate(cases, 1):
        prior = previous.get(case["id"])
        if prior and prior["valid"]:
            rows.append(prior)
            write_jsonl(run_dir / "cases.jsonl", rows)
            continue
        print(f"[{index}/{len(cases)}] {case['id']} / {case['rag_eval']['split']}", flush=True)
        result = _search(args.retrieval_base_url, dict(query=case["query"], top_k=5, candidate_k=20,
            rerank=True, threshold=COSINE_THRESHOLD, domain=identity["domain"], dataset_id=identity["dataset_id"]), args.timeout)
        scored = result.get("scored_documents", [])
        valid = result["http_success"] and (result["rerank_applied"] or not result["source_ids"])
        valid = valid and all(_finite(d.get("similarity")) and _finite(d.get("rerank_score")) for d in scored)
        rows.append(dict(id=case["id"], split=case["rag_eval"]["split"], expected_sources=case["expected_sources"],
                         valid=valid, **result))
        if prior:
            rows[-1]["previous_measurement"] = prior
        write_jsonl(run_dir / "cases.jsonl", rows)
    invalid = [r["id"] for r in rows if not r["valid"]]
    if invalid:
        write_json(run_dir / "summary.json", {"invalid_cases": invalid, "selected_threshold": None})
        print("Calibration incomplete; no threshold selected:", invalid)
        return 1
    dev = [r for r in rows if r["split"] == "dev"]
    test = [r for r in rows if r["split"] == "test"]
    selected, curve = select_threshold(dev)
    test_baseline = score_acceptance(test, BASELINE_THRESHOLD)
    test_selected = score_acceptance(test, selected["threshold"])
    summary = dict(cases_total=len(rows), dev_curve=curve, dev_selected=selected,
                   test_baseline=test_baseline, test_selected=test_selected,
                   initial_rerank_missing=sum("previous_measurement" in r for r in rows),
                   source_runs=([resume.name] if resume else []) + [run_dir.name],
                   test_admission_guard_passed=test_selected["unanswerable_admitted"] <= test_baseline["unanswerable_admitted"])
    write_json(run_dir / "summary.json", summary)
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    print("Report:", run_dir / "summary.json")
    return 0 if summary["test_admission_guard_passed"] else 1
