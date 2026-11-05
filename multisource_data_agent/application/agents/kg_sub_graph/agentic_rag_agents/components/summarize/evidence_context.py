"""Bounded, tool-agnostic evidence context for the Summarize node."""

from __future__ import annotations

from dataclasses import dataclass, field
from itertools import islice
from math import isfinite
from typing import Any, Sequence


MAX_EVIDENCE = 10
MAX_CONTENT = 1000
MAX_TOOLS = 12
# Keep the synthesis sample bounded while retaining all rows in the normal
# small aggregate result (for example a four-group SQL query).
MAX_SAMPLES_PER_TOOL = 10


@dataclass
class EvidenceContext:
    sql_facts: list[str] = field(default_factory=list)
    graph_facts: list[str] = field(default_factory=list)
    document_facts: list[str] = field(default_factory=list)
    tool_answers: list[dict[str, str]] = field(default_factory=list)
    citation_facts: list[dict[str, Any]] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)

    def summary_sections(self) -> tuple[list[str], list[str], list[str]]:
        """Expose only bounded, ready-to-render text to Summarize."""
        narrative = [item["text"] for item in self.tool_answers if item["kind"] == "rag"]
        if self.document_facts:
            narrative.append("\n".join(self.document_facts))
        metric = [item["text"] for item in self.tool_answers if item["kind"] != "rag"]
        metric.extend(self.sql_facts)
        metric.extend(self.graph_facts)
        return narrative, metric, list(self.metadata.get("errors", []))


def _field(value: Any, name: str, default: Any = None) -> Any:
    return value.get(name, default) if isinstance(value, dict) else getattr(value, name, default)


def _clip(value: Any, limit: int = MAX_CONTENT, depth: int = 0) -> str:
    if isinstance(value, str):
        result = value
    elif isinstance(value, dict):
        if depth >= 2:
            result = "{...}"
        else:
            parts = (
                f"{_clip(key, 60, depth + 1)}: {_clip(item, 160, depth + 1)}"
                for key, item in islice(value.items(), 8)
            )
            result = "{" + ", ".join(parts) + (", ..." if len(value) > 8 else "") + "}"
    elif isinstance(value, (list, tuple)):
        if depth >= 2:
            result = "[...]"
        else:
            parts = (_clip(item, 160, depth + 1) for item in value[:5])
            result = "[" + ", ".join(parts) + (", ..." if len(value) > 5 else "") + "]"
    else:
        result = str(value)
    return result if len(result) <= limit else result[: max(0, limit - 3)] + "..."


def _metadata(raw: Any) -> dict[str, str]:
    if not isinstance(raw, dict):
        return {}
    return {str(key)[:60]: _clip(value, 160) for key, value in islice(raw.items(), 6)}


def _document_scope(raw: Any) -> dict[str, str]:
    if not isinstance(raw, dict):
        return {}
    keys = ("document_id", "document_source_id", "document_version", "effective_from", "effective_to",
            "scope", "section_title", "content_origin", "date_metadata_invalid")
    return {key: _clip(raw[key], 160) for key in keys if raw.get(key) is not None}


def _kind(result: Any, evidence: Sequence[Any]) -> str:
    kind = _field(result, "tool_kind")
    if kind in {"rag", "graph", "sql"}:
        return kind
    language = _field(_field(result, "statement"), "language")
    if language == "sql":
        return "sql"
    if language == "cypher":
        return "graph"
    kinds = {_field(item, "kind") for item in evidence[:MAX_EVIDENCE]}
    if "document" in kinds:
        return "rag"
    if "sql_row" in kinds:
        return "sql"
    if "graph_record" in kinds:
        return "graph"
    return "other"


def _score(value: Any) -> float | None:
    if isinstance(value, (int, float)) and not isinstance(value, bool) and isfinite(value):
        return float(value)
    return None


def build_evidence_context(tool_results: Sequence[Any]) -> EvidenceContext:
    """Build a small context without serializing full ToolResult data or statements."""
    context = EvidenceContext(
        metadata={"tools": [], "evidence": [], "errors": [], "evidence_count": 0, "truncated": False}
    )
    if len(tool_results) > MAX_TOOLS:
        context.metadata["truncated"] = True

    for result in tool_results[:MAX_TOOLS]:
        tool_name = _clip(_field(result, "tool_name") or "tool", 80)
        task = _clip(_field(result, "task") or tool_name, 120)
        status = _field(result, "status") or "empty"
        raw_evidence = _field(result, "evidence") or []
        if not isinstance(raw_evidence, (list, tuple)):
            raw_evidence = []
        kind = _kind(result, raw_evidence)
        data = _field(result, "data")
        row_count = len(data) if isinstance(data, list) else len(raw_evidence)
        statement_language = _field(_field(result, "statement"), "language")
        context.metadata["tools"].append({
            "tool_name": tool_name,
            "task": task,
            "status": _clip(status, 20),
            "kind": kind,
            "row_count": row_count if kind in {"sql", "graph"} else None,
            "statement_language": statement_language if statement_language in {"sql", "cypher"} else None,
        })
        if status == "error":
            # Neither the model prompt nor deterministic answer fallback may echo driver exceptions.
            # Detailed failures remain in server-side diagnostics; status/codes remain in execution.
            context.metadata["errors"].append(f"{task}: Tool execution failed; this task produced no evidence.")
            continue

        answer = _field(result, "answer")
        if isinstance(answer, str) and answer.strip():
            context.tool_answers.append({
                "kind": kind,
                "text": f"{task}: {_clip(answer.strip(), 700)}" if kind != "rag" else _clip(answer.strip(), 700),
            })

        if status == "empty":
            continue

        samples: list[str] = []
        for item in raw_evidence[:MAX_SAMPLES_PER_TOOL]:
            if context.metadata["evidence_count"] >= MAX_EVIDENCE:
                context.metadata["truncated"] = True
                break
            evidence_kind = _field(item, "kind")
            if evidence_kind not in {"sql_row", "graph_record", "document"}:
                continue
            source = _field(item, "source")
            source_id = _field(item, "source_id")
            rank = _field(item, "rank")
            raw_meta = _field(item, "metadata") or {}
            if evidence_kind == "document":
                source = source or (raw_meta.get("source") if isinstance(raw_meta, dict) else None)
                source = source or source_id
                if not source:
                    continue
            raw_content = _field(item, "content")
            if raw_content is None or raw_content == "":
                continue
            content = _clip(raw_content, MAX_CONTENT)
            if isinstance(raw_content, str) and len(raw_content) > MAX_CONTENT:
                context.metadata["truncated"] = True
            score = _score(_field(item, "score"))
            retrieval_score = _score(_field(item, "retrieval_score"))
            rerank_score = _score(_field(item, "rerank_score"))
            evidence_id = _field(item, "evidence_id")
            context.metadata["evidence"].append({
                "evidence_id": _clip(evidence_id, 120) if evidence_id else None,
                "kind": evidence_kind,
                "source": _clip(source, 200) if source else None,
                "source_id": _clip(source_id, 200) if source_id else None,
                "rank": rank if isinstance(rank, int) else None,
                "score": score,
                "retrieval_score": retrieval_score,
                "rerank_score": rerank_score,
                "citation": _clip(_field(item, "citation"), 240) if _field(item, "citation") else None,
                "metadata": _metadata(raw_meta),
            })
            context.metadata["evidence_count"] += 1
            if evidence_id:
                context.citation_facts.append({
                    "evidence_id": _clip(evidence_id, 120),
                    "kind": evidence_kind,
                    "source": _clip(source, 200) if source else None,
                    "source_id": _clip(source_id, 200) if source_id else None,
                    "content": content,
                    **({"document": _document_scope(raw_meta)} if evidence_kind == "document" else {}),
                })
            if evidence_kind == "document":
                score_text = f" (score={score:.3f})" if score is not None else ""
                context.document_facts.append(f"{_clip(source, 200)}{score_text}: {content}")
            elif (kind == "sql" and evidence_kind == "sql_row") or (
                kind == "graph" and evidence_kind == "graph_record"
            ):
                samples.append(content)

        if len(raw_evidence) > MAX_SAMPLES_PER_TOOL:
            context.metadata["truncated"] = True

        if kind in {"sql", "graph"}:
            if not samples and isinstance(data, list):
                samples = [_clip(row, MAX_CONTENT) for row in data[:MAX_SAMPLES_PER_TOOL]]
            if isinstance(data, list) and len(data) > MAX_SAMPLES_PER_TOOL:
                context.metadata["truncated"] = True
            if samples or row_count:
                count_label = "row_count" if kind == "sql" else "record_count"
                fact = f"{task}: {count_label}={row_count}"
                if samples:
                    fact += "\n" + "\n".join(
                        f"{index}. {sample}" for index, sample in enumerate(samples, 1)
                    )
                (context.sql_facts if kind == "sql" else context.graph_facts).append(fact)

    return context
