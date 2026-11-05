"""Retrieval-only mode of the existing Eval runner (never full Agent accuracy)."""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import statistics
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

from .rag_quality import validate_rag_case
from kb_ingest.kb_service.services.documents import prepare_document_records


def retrieval_metrics(expected: list[str], ranked: list[str], k: int) -> dict[str, float | None]:
    # Retrieval slots are physical chunks. Repeated chunks from one document
    # cannot shift a later relevant document upward or inflate its reciprocal rank.
    ranked = ranked[:k]
    relevant = set(expected)
    if not relevant:
        return {"recall_at_k": None, "mrr_at_k": None, "precision_at_k": None}
    hits = relevant & set(ranked)
    first = next((index for index, source in enumerate(ranked, 1) if source in relevant), None)
    return {
        "recall_at_k": len(hits) / len(relevant),
        "mrr_at_k": 1 / first if first else 0.0,
        # Missing results still occupy the fixed retrieval budget denominator.
        "precision_at_k": len(hits) / k,
    }


def _search(base_url: str, payload: dict[str, Any], timeout: float) -> dict[str, Any]:
    request = urllib.request.Request(
        base_url.rstrip("/") + "/api/v1/knowledge/search",
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json; charset=utf-8"}, method="POST",
    )
    started = time.perf_counter()
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            body = json.loads(response.read().decode("utf-8"))
        if not isinstance(body, dict) or not isinstance(body.get("results"), list):
            raise ValueError("Malformed retrieval response")
        docs = body["results"]
        if any(not isinstance(doc, dict) or not isinstance(doc.get("source_id"), str) for doc in docs):
            raise ValueError("Retrieval results must contain stable source IDs")
        diagnostics = body.get("retrieval")
        if not isinstance(diagnostics, dict) or not isinstance(diagnostics.get("candidate_source_ids"), list):
            raise ValueError("Retrieval diagnostics missing: update kb_ingest before running the comparison")
        return {
            "http_success": True,
            "source_ids": [doc["source_id"] for doc in docs],
            "document_source_ids": [(doc.get("metadata") or {}).get("document_source_id") or doc["source_id"] for doc in docs],
            "scored_documents": [{
                "source_id": (doc.get("metadata") or {}).get("document_source_id") or doc["source_id"],
                "similarity": doc.get("similarity"), "rerank_score": doc.get("rerank_score"),
            } for doc in docs],
            "content_hashes": {
                doc["source_id"]: hashlib.sha256(str(doc.get("content") or "").encode("utf-8")).hexdigest()
                for doc in docs
            },
            "candidate_source_ids": diagnostics["candidate_source_ids"],
            "candidate_document_ids": diagnostics.get("candidate_document_ids", diagnostics["candidate_source_ids"]),
            "lexical_applied": diagnostics.get("lexical_applied") is True,
            "rerank_applied": diagnostics.get("rerank_applied") is True,
            "timings": {key: diagnostics.get(key) for key in ("embedding_ms", "vector_ms", "lexical_ms", "rerank_ms", "total_ms")},
            "observed_http_ms": round((time.perf_counter() - started) * 1000, 2),
            "error": None,
        }
    except Exception as exc:
        # Never serialize upstream error bodies / connection credentials.
        return {"http_success": False, "source_ids": [], "candidate_source_ids": [],
                "rerank_applied": False, "timings": {},
                "observed_http_ms": round((time.perf_counter() - started) * 1000, 2),
                "error": f"{type(exc).__name__}" + (f" HTTP {exc.code}" if isinstance(exc, urllib.error.HTTPError) else "")}


def summarize_comparison(results: list[dict[str, Any]]) -> dict[str, Any]:
    paired = [r for r in results if r["comparable"]]
    summary: dict[str, Any] = {"cases_total": len(results), "paired_comparable_cases": len(paired)}
    for variant in (results[0]["variants"] if results else ("dense", "rerank")):
        all_runs = [r["variants"][variant] for r in results]
        scored = [r["variants"][variant] for r in paired]
        summary[variant] = {
            "http_failures": sum(not r["http_success"] for r in all_runs),
            "rerank_applied_cases": sum(r["rerank_applied"] for r in all_runs),
            "rerank_fallback_cases": sum(
                r["http_success"] and bool(r["candidate_source_ids"]) and not r["rerank_applied"]
                for r in all_runs
            ) if variant != "dense" else 0,
            "lexical_hit_cases": sum(r.get("lexical_applied", False) for r in all_runs),
            "quality_denominator": sum(r["metrics"]["recall_at_k"] is not None for r in scored),
            "avg_observed_http_ms": statistics.mean(r["observed_http_ms"] for r in all_runs) if all_runs else None,
        }
        for metric in ("recall_at_k", "mrr_at_k", "precision_at_k", "candidate_recall"):
            values = [r["metrics"][metric] for r in scored if r["metrics"][metric] is not None]
            summary[variant][metric] = statistics.mean(values) if values else None
        for stage in ("embedding_ms", "vector_ms", "lexical_ms", "rerank_ms"):
            durations = [r["timings"][stage] for r in all_runs if isinstance(r["timings"].get(stage), (int, float))]
            summary[variant]["avg_" + stage] = statistics.mean(durations) if durations else None
    summary["unanswerable_cases"] = sum(not r["expected_sources"] for r in results)
    return summary


def run_retrieval_comparison(args: Any, cases: list[dict[str, Any]], dataset_path: Path) -> int:
    from eval.run_eval import collect_environment_metadata, write_json, write_jsonl

    if not 1 <= args.top_k <= args.candidate_k <= 100:
        raise ValueError("Require 1 <= top_k <= candidate_k <= 100")
    if args.retry_count:
        raise ValueError("Retrieval ablation runs once per variant; use --retry-count 0")
    for case in cases:
        validate_rag_case(case)
        if "rag_eval" not in case:
            raise ValueError("Retrieval comparison requires rag_eval corpus identity")
    environment = collect_environment_metadata(
        dataset_path=dataset_path, base_url=args.retrieval_base_url, timeout=args.timeout,
        retry_count=0, case_count=len(cases), model_label=args.model_label, provider_label=args.provider_label,
    )
    corpus = getattr(args, "corpus", None) or Path(__file__).resolve().parents[1] / "fixtures" / "rag_documents_v1.jsonl"
    corpus_docs = [json.loads(line) for line in corpus.read_text(encoding="utf-8").splitlines() if line.strip()]
    identity = cases[0]["rag_eval"]
    if any(case["rag_eval"][key] != identity[key] for case in cases for key in ("domain", "dataset_id")):
        raise ValueError("A paired retrieval run must use one fixed corpus identity")
    records = [record for doc in corpus_docs for record in prepare_document_records(
        dict(source_id=doc["source_id"], original_data=doc, domain=doc["domain"], dataset_id=doc["dataset_id"]))]
    expected_ids = {record["source_id"] for record in records}
    expected_hashes = {
        record["source_id"]: hashlib.sha256(record["source_content"].encode("utf-8")).hexdigest()
        for record in records
    }
    if any(doc["domain"] != identity["domain"] or doc["dataset_id"] != identity["dataset_id"] for doc in corpus_docs):
        raise ValueError("Case corpus identity differs from the fixed fixture")
    probe = _search(args.retrieval_base_url, {
        "query": "Evaluation corpus integrity check", "top_k": 100, "candidate_k": 100,
        "rerank": False, "threshold": None,
        "domain": identity["domain"], "dataset_id": identity["dataset_id"],
    }, args.timeout)
    if (not probe["http_success"] or set(probe["source_ids"]) != expected_ids
            or probe.get("content_hashes") != expected_hashes):
        raise ValueError("Evaluation corpus is unavailable/incomplete; seed the isolated fixture before scoring")
    environment.update({"mode": "retrieval_only", "candidate_k": args.candidate_k, "top_k": args.top_k,
                        "corpus_sha256": hashlib.sha256(corpus.read_bytes()).hexdigest(),
                        "corpus_source_ids_verified": sorted(expected_ids)})
    run_dir = Path(args.output_dir) / dt.datetime.now().strftime("%Y%m%d_%H%M%S")
    run_dir.mkdir(parents=True, exist_ok=True)
    write_json(run_dir / "environment.json", environment)
    results = []
    for index, case in enumerate(cases, 1):
        spec = case["rag_eval"]
        row = {"id": case["id"], "split": spec["split"], "expected_sources": case.get("expected_sources", []), "variants": {}}
        # Alternate order to reduce consistent warm-cache / provider-order bias.
        variants = ["dense", "rerank"] + (["hybrid"] if getattr(args, "compare_hybrid", False) else [])
        rotation = (index - 1) % len(variants)
        for variant in variants[rotation:] + variants[:rotation]:
            print(f"[{index}/{len(cases)}] {case['id']} / {variant}", flush=True)
            payload = {"query": case["query"], "top_k": args.candidate_k if variant == "dense" else args.top_k,
                       "candidate_k": args.candidate_k, "rerank": variant != "dense",
                       "retrieval_mode": "hybrid" if variant == "hybrid" else "dense",
                       "domain": spec["domain"], "dataset_id": spec["dataset_id"], "threshold": None}
            if spec.get("as_of"):
                payload["as_of"] = spec["as_of"]
            outcome = _search(args.retrieval_base_url, payload, args.timeout)
            outcome["metrics"] = retrieval_metrics(row["expected_sources"], outcome.get("document_source_ids", outcome["source_ids"]), args.top_k)
            outcome["metrics"]["candidate_recall"] = retrieval_metrics(
                row["expected_sources"], outcome.get("candidate_document_ids", outcome["candidate_source_ids"]), args.candidate_k
            )["recall_at_k"]
            row["variants"][variant] = outcome
        dense, rerank = row["variants"]["dense"], row["variants"]["rerank"]
        row["comparable"] = (
            dense["http_success"] and rerank["http_success"]
            and set(dense["candidate_source_ids"]) == set(rerank["candidate_source_ids"])
            and (rerank["rerank_applied"] or not rerank["candidate_source_ids"])
        )
        if "hybrid" in row["variants"]:
            hybrid = row["variants"]["hybrid"]
            # Hybrid intentionally has a different candidate set. Equality to
            # dense would hide its recall benefit; corpus/filter/budget are fixed.
            row["comparable"] = row["comparable"] and hybrid["http_success"] and (
                hybrid["rerank_applied"] or not hybrid["candidate_source_ids"])
        results.append(row)
        write_jsonl(run_dir / "cases.jsonl", results)
    summary = summarize_comparison(results)
    write_json(run_dir / "summary.json", summary)
    lines = ["# RAG retrieval-only comparison", "", "This is not a full Agent HTTP evaluation.", "",
             f"Candidate budget: {args.candidate_k}; final top_k: {args.top_k}.", "",
             "Dense/rerank share candidates; hybrid uses the same corpus/filter/budget, with independent lexical recall.",
             "HTTP failures and rerank fallbacks remain counted separately; observed latency includes failures.",
             "Unanswerable questions have null recall/MRR; returned documents do not prove an answer exists.", "",
             "```json", json.dumps(summary, ensure_ascii=False, indent=2), "```", "",
             "| Case | Comparable | Dense recall | Rerank recall | Dense MRR | Rerank MRR |", "|---|---|---:|---:|---:|---:|"]
    for row in results:
        d, r = (row["variants"][v]["metrics"] for v in ("dense", "rerank"))
        lines.append(f"| {row['id']} | {row['comparable']} | {d['recall_at_k']} | {r['recall_at_k']} | {d['mrr_at_k']} | {r['mrr_at_k']} |")
    if getattr(args, "compare_hybrid", False):
        lines.extend(["", "## Hybrid case metrics", "", "| Case | Recall | MRR | Candidate recall |", "|---|---:|---:|---:|"])
        for row in results:
            h = row["variants"]["hybrid"]["metrics"]
            lines.append(f"| {row['id']} | {h['recall_at_k']} | {h['mrr_at_k']} | {h['candidate_recall']} |")
    (run_dir / "report.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    print(f"Report: {run_dir / 'report.md'}")
    return 0 if len(results) == summary["paired_comparable_cases"] else 1
