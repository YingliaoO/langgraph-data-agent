"""Declared RAG checks, not an automated semantic-faithfulness judge."""

from __future__ import annotations

from typing import Any

from .fact_assertions import _contains_fact, evaluate_fact_assertions


def validate_rag_case(case: dict[str, Any]) -> None:
    spec = case.get("rag_eval")
    if spec is None:
        return
    if not isinstance(spec, dict) or not isinstance(spec.get("answerable"), bool):
        raise ValueError("rag_eval requires an explicit boolean answerable")
    for field in ("domain", "dataset_id"):
        if not isinstance(spec.get(field), str) or not spec[field].strip():
            raise ValueError(f"rag_eval requires {field}")
    sources = case.get("expected_sources", [])
    if not isinstance(sources, list) or not all(isinstance(s, str) and s for s in sources):
        raise ValueError("expected_sources must contain stable source IDs")
    if len(sources) != len(set(sources)) or bool(sources) != spec["answerable"]:
        raise ValueError("Answerable RAG cases need unique sources; unanswerable cases need none")
    if spec.get("split") not in {"dev", "test"}:
        raise ValueError("rag_eval split must be dev or test")
    if not isinstance(spec.get("http_compatible"), bool):
        raise ValueError("rag_eval must declare whether the product HTTP domain can answer the case")
    if not spec["answerable"]:
        if case.get("expected_execution_status") is not None:
            raise ValueError("Unanswerable is an answer property, not a fixed retrieval execution status")
        phrases = spec.get("abstention_phrases")
        if not isinstance(phrases, list) or not phrases or not all(isinstance(s, str) and s.strip() for s in phrases):
            raise ValueError("Unanswerable RAG cases require explicit abstention_phrases")


def evaluate_rag_quality(case: dict[str, Any], response: dict[str, Any]) -> dict[str, Any]:
    if "rag_eval" not in case:
        return {}
    validate_rag_case(case)
    spec = case["rag_eval"]
    execution = response.get("execution")
    execution = execution if isinstance(execution, dict) else {}
    evidence = execution.get("evidence", [])
    evidence = [e for e in evidence if isinstance(e, dict)] if isinstance(evidence, list) else []
    citations = execution.get("citation_ids", [])
    citations = citations if isinstance(citations, list) else []
    known = {e["evidence_id"] for e in evidence if isinstance(e.get("evidence_id"), str)}
    valid = all(isinstance(c, str) and c in known for c in citations)
    # Missing citations for an answerable query must not pass vacuously.
    completed = execution.get("status") in {"success", "empty", "partial"}
    citation_ok = completed and valid and (bool(citations) or not spec["answerable"])
    cited = [e for e in evidence if e.get("evidence_id") in citations]
    assertions = [a for a in case.get("fact_assertions", []) if a["kind"] == "evidence_fact"]
    checks = evaluate_fact_assertions(assertions, {"execution": {"evidence": cited}})
    answer = response.get("message") or ""
    # This intentionally narrow rule checks explicit abstention language and
    # absence of misleading citations. It does not prove semantic correctness.
    abstention = None
    if not spec["answerable"]:
        abstention = (
            execution.get("status") in {"success", "empty", "partial"}
            and isinstance(answer, str)
            and any(_contains_fact(answer, p) for p in spec["abstention_phrases"])
            and not citations
        )
    expected = set(case.get("expected_sources", []))
    actual = {(e.get("metadata") or {}).get("document_source_id") or e.get("source_id") for e in evidence if e.get("source") == "pgvector"}
    cited_sources = {(e.get("metadata") or {}).get("document_source_id") or e.get("source_id") for e in cited if e.get("source") == "pgvector"}
    return {
        "rag_citation_resolution_ok": citation_ok,
        "rag_cited_fact_support": (
            valid and expected <= cited_sources and all(c["passed"] for c in checks)
            if checks else None
        ),
        "rag_expected_source_recall": len(expected & actual) / len(expected) if expected else None,
        "rag_abstention_ok": abstention,
    }
