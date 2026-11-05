"""Versioned, bounded HTTP view of canonical tool execution.

The tool and graph states retain complete results for answer synthesis.  This
module constructs a separate public view with an explicit field allowlist.
"""

from __future__ import annotations

import hashlib
import re
from collections import defaultdict
from math import isfinite
from typing import Any, Literal, Sequence

from pydantic import BaseModel, Field

from multisource_data_agent.application.agents.tool_contracts import Evidence, ToolResult
from multisource_data_agent.application.agents.timing import interval_durations, stage_intervals


CONTRACT_VERSION = "1"
MAX_PREVIEW_ITEMS = 10
MAX_PUBLIC_EVIDENCE = 30
MAX_DOCUMENT_CHARS = 500
MAX_CELL_CHARS = 240
MAX_STATEMENT_CHARS = 2000
_SAFE_METADATA_KEYS = frozenset({
    "backend", "title", "source_table", "row_index", "strategy", "query_name",
    "domain", "dataset_id", "document_id", "document_source_id", "document_version",
    "effective_from", "effective_to", "scope", "section_title", "content_origin",
    "chunk_index", "chunk_count", "start_char", "end_char", "source_content_hash",
    "date_metadata_invalid",
})
_SENSITIVE_KEY = re.compile(r"api.?key|password|passwd|secret|token|authorization|connection|credential", re.I)
_SENSITIVE_VALUE = re.compile(
    r"(?i)(?:mysql|postgres(?:ql)?|neo4j|bolt|redis)://\S+|"
    r"\bsk-[A-Za-z0-9_-]{12,}\b|"
    r"(?:api[_-]?key|password|passwd|secret|token|authorization)\s*[:=]\s*[^\s,;]+"
)


class ExecutionStatement(BaseModel):
    language: Literal["sql", "cypher"]
    text: str


class ExecutionTask(BaseModel):
    task_id: str
    question: str
    tool_hint: str = "auto"
    depends_on: list[str] = Field(default_factory=list)


class ExecutionEvidence(BaseModel):
    evidence_id: str
    kind: str
    source: str | None = None
    source_id: str | None = None
    content: Any
    score: float | None = None
    rank: int | None = None
    task_id: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class ExecutionToolResult(BaseModel):
    tool_name: str
    tool_kind: str | None = None
    status: Literal["success", "empty", "error"]
    task_id: str | None = None
    task: str | None = None
    statement: ExecutionStatement | None = None
    result_count: int = 0
    rows_preview: list[dict[str, Any]] = Field(default_factory=list)
    records_preview: list[dict[str, Any]] = Field(default_factory=list)
    documents_preview: list[dict[str, Any]] = Field(default_factory=list)
    evidence_ids: list[str] = Field(default_factory=list)
    error_code: str | None = None
    error_category: str | None = None
    repair_count: int = 0


class ExecutionDTO(BaseModel):
    contract_version: Literal["1"] = CONTRACT_VERSION
    status: Literal["success", "empty", "partial", "error"] = "empty"
    tasks: list[ExecutionTask] = Field(default_factory=list)
    tool_results: list[ExecutionToolResult] = Field(default_factory=list)
    evidence: list[ExecutionEvidence] = Field(default_factory=list)
    citation_ids: list[str] = Field(default_factory=list)
    timings: dict[str, float] = Field(default_factory=dict)
    fallback_reason: str = "none"


def _field(value: Any, key: str, default: Any = None) -> Any:
    return value.get(key, default) if isinstance(value, dict) else getattr(value, key, default)


def _short(value: Any, limit: int) -> str:
    text = _SENSITIVE_VALUE.sub("<redacted>", str(value if value is not None else ""))
    return text[:limit]


def _safe_value(value: Any, *, limit: int = MAX_CELL_CHARS, depth: int = 0) -> Any:
    if depth >= 3:
        return "<truncated>"
    if isinstance(value, dict):
        return {
            _short(key, 80): _safe_value(item, limit=limit, depth=depth + 1)
            for key, item in list(value.items())[:12]
            if not _SENSITIVE_KEY.search(str(key))
        }
    if isinstance(value, (list, tuple)):
        return [_safe_value(item, limit=limit, depth=depth + 1) for item in value[:10]]
    if isinstance(value, str):
        return _short(value, limit)
    if value is None or isinstance(value, (bool, int)):
        return value
    if isinstance(value, float) and isfinite(value):
        return value
    return _short(value, limit)


def _normalize_task_id(value: Any, *, default: str = "task") -> str:
    normalized = re.sub(r"[^A-Za-z0-9_]+", "_", str(value or ""))[:48]
    return normalized or default


def _task_id(result: Any) -> str:
    metadata = _field(result, "metadata") or {}
    planner = metadata.get("planner_task") if isinstance(metadata, dict) else None
    declared = _field(planner, "task_id")
    if declared:
        return _normalize_task_id(declared)
    stable = f"{_field(result, 'tool_name', '')}:{_field(result, 'task', '')}"
    return "task_" + hashlib.sha256(stable.encode("utf-8")).hexdigest()[:10]


def normalize_tool_results(results: Sequence[Any]) -> list[ToolResult]:
    """Give every evidence item a reproducible task-scoped ID."""
    normalized: list[ToolResult] = []
    counters: dict[str, int] = defaultdict(int)
    for raw in results or []:
        result = raw if isinstance(raw, ToolResult) else ToolResult.model_validate(raw)
        task_id = _task_id(result)
        evidence: list[Evidence] = []
        for raw_item in result.evidence:
            item = raw_item if isinstance(raw_item, Evidence) else Evidence.model_validate(raw_item)
            counters[task_id] += 1
            evidence_index = counters[task_id]
            evidence_id = f"ev_{task_id}_{evidence_index}"
            source_id = item.source_id
            if item.kind == "sql_row":
                source_id = f"mysql:{task_id}:row:{item.rank or evidence_index}"
            elif item.kind == "graph_record":
                source_id = f"neo4j:{task_id}:record:{item.rank or evidence_index}"
            evidence.append(item.model_copy(update={
                "evidence_id": evidence_id,
                "task_id": task_id,
                "source_id": source_id,
            }))
        normalized.append(result.model_copy(update={
            "evidence": evidence,
            "evidence_ids": [item.evidence_id for item in evidence],
        }))
    return normalized


def execution_summary(results: Sequence[Any], *, route: str | None = None) -> dict[str, Any]:
    statuses = [_field(result, "status") for result in results]
    if not statuses:
        status = "empty" if route == "data-query" else "error" if route == "error" else "success"
    else:
        has_success = "success" in statuses
        has_error = "error" in statuses
        if has_success and has_error:
            # Reserve partial for genuine partial success: at least one task
            # succeeded while at least one task failed. Empty is a valid
            # completed outcome and does not by itself make a request partial.
            status = "partial"
        elif has_error:
            status = "error"
        elif has_success:
            status = "success"
        else:
            status = "empty"
    # Failure/empty outcomes are represented by status and per-tool error fields.
    # fallback_reason is reserved for a real alternate-strategy fallback.
    return {"status": status, "fallback_reason": "none", "tool_result_count": len(statuses)}


def _trace_fallback_reason(trace: Any) -> str:
    if not isinstance(trace, dict):
        return "none"
    explicit = trace.get("fallback_reason")
    if explicit:
        return _short(explicit, 80)
    for event in reversed(trace.get("events", [])):
        if not isinstance(event, dict) or event.get("fallback") is not True:
            continue
        metadata = event.get("metadata")
        if isinstance(metadata, dict):
            for key in ("fallback_reason", "reason"):
                if metadata.get(key):
                    return _short(metadata[key], 80)
        if event.get("error_code"):
            return _short(event["error_code"], 80)
        return _short(f"{event.get('stage') or 'execution'}_fallback", 80)
    return "fallback" if trace.get("fallback") else "none"


def _result_count(result: Any) -> int:
    metadata = _field(result, "metadata") or {}
    for key in ("result_count", "row_count"):
        value = metadata.get(key) if isinstance(metadata, dict) else None
        if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
            return value
    data = _field(result, "data")
    return len(data) if isinstance(data, list) else len(_field(result, "evidence") or [])


def _timings(trace: Any) -> dict[str, float]:
    if not isinstance(trace, dict):
        return {}
    timings: dict[str, float] = {}
    duration = trace.get("duration_ms")
    if isinstance(duration, (int, float)) and isfinite(duration):
        timings["total_ms"] = round(float(duration), 2)
    for stage in ("router", "planner", "tool_execution", "summarize", "data_query"):
        durations = interval_durations(stage_intervals(trace.get("events"), stage))
        if durations:
            timings[f"{stage}_ms"] = durations["wall_ms"]
            if stage == "tool_execution":
                timings["tool_execution_sum_ms"] = durations["sum_ms"]
                completed = interval_durations(stage_intervals(trace.get("events"), stage, completed_only=True))
                if completed:
                    timings["tool_completed_sum_ms"] = completed["sum_ms"]
    return timings


def build_execution_dto(
    *, tasks: Sequence[Any] = (), tool_results: Sequence[Any] = (), evidence: Sequence[Any] = (),
    citation_ids: Sequence[str] = (), execution_summary_value: dict[str, Any] | None = None,
    trace: Any = None, route: str | None = None,
) -> ExecutionDTO:
    """Build the public contract only from selected, bounded fields."""
    results = list(tool_results or [])
    summary = execution_summary_value or execution_summary(results, route=route)
    task_views = [ExecutionTask(
        task_id=_normalize_task_id(_field(task, "task_id") or f"task_{index}"),
        question=_short(_field(task, "question"), 300),
        tool_hint=_short(_field(task, "tool_hint", "auto"), 40),
        depends_on=[_normalize_task_id(value) for value in (_field(task, "depends_on") or [])[:6]],
    ) for index, task in enumerate(tasks or [], 1)][:6]
    all_evidence = list(evidence or [item for result in results for item in (_field(result, "evidence") or [])])
    evidence_views: list[ExecutionEvidence] = []
    for item in all_evidence[:MAX_PUBLIC_EVIDENCE]:
        evidence_id = _field(item, "evidence_id")
        if not evidence_id:
            continue
        evidence_kind = _field(item, "kind")
        public_source = {
            "sql_row": "mysql",
            "document": "pgvector",
            "graph_record": "neo4j",
        }.get(evidence_kind, _field(item, "source"))
        raw_metadata = _field(item, "metadata") or {}
        safe_metadata = {
            key: _safe_value(raw_metadata[key])
            for key in _SAFE_METADATA_KEYS
            if isinstance(raw_metadata, dict) and key in raw_metadata
        }
        evidence_views.append(ExecutionEvidence(
            evidence_id=_short(evidence_id, 100),
            kind=_short(evidence_kind, 40),
            source=_short(public_source, 100) or None,
            source_id=_short(_field(item, "source_id"), 160) or None,
            content=_safe_value(_field(item, "content"), limit=MAX_DOCUMENT_CHARS),
            score=_field(item, "score"),
            rank=_field(item, "rank"),
            task_id=_short(_field(item, "task_id"), 48) or None,
            metadata=safe_metadata,
        ))
    exposed_ids = {item.evidence_id for item in evidence_views}
    result_views: list[ExecutionToolResult] = []
    for result in results[:12]:
        kind = _field(result, "tool_kind")
        data = _field(result, "data") or []
        preview = [_safe_value(row) for row in data[:MAX_PREVIEW_ITEMS]] if isinstance(data, list) else []
        if kind == "rag":
            document_preview = []
            for item in (_field(result, "evidence") or [])[:MAX_PREVIEW_ITEMS]:
                document_preview.append(
                    {
                        "evidence_id": _field(item, "evidence_id"),
                        "source_id": _field(item, "source_id"),
                        "source": "pgvector",
                        "content": _safe_value(_field(item, "content"), limit=MAX_DOCUMENT_CHARS),
                        "score": _field(item, "score"),
                        "rank": _field(item, "rank"),
                    }
                )
            if document_preview:
                preview = document_preview
        statement = _field(result, "statement")
        language = _field(statement, "language")
        safe_statement = ExecutionStatement(language=language, text=_short(_field(statement, "text"), MAX_STATEMENT_CHARS)) if language in {"sql", "cypher"} else None
        metadata = _field(result, "metadata") or {}
        result_views.append(ExecutionToolResult(
            tool_name=_short(_field(result, "tool_name"), 80),
            tool_kind=kind,
            status=_field(result, "status"),
            task_id=_task_id(result),
            task=_short(_field(result, "task"), 300) or None,
            statement=safe_statement,
            result_count=_result_count(result),
            rows_preview=preview if kind == "sql" else [],
            records_preview=preview if kind == "graph" else [],
            documents_preview=preview if kind == "rag" else [],
            evidence_ids=[value for value in (_field(result, "evidence_ids") or []) if value in exposed_ids],
            error_code=_short(metadata.get("error_code"), 80) or None if isinstance(metadata, dict) else None,
            error_category=_short(metadata.get("error_category"), 80) or None if isinstance(metadata, dict) else None,
            repair_count=max(0, int(metadata.get("repair_count") or 0)) if isinstance(metadata, dict) else 0,
        ))
    public_citation_ids = [
        _short(value, 100) for value in citation_ids
        if value in exposed_ids
    ]
    return ExecutionDTO(
        status=summary.get("status", "empty"),
        tasks=task_views,
        tool_results=result_views,
        evidence=evidence_views,
        citation_ids=public_citation_ids,
        timings=_timings(trace),
        fallback_reason=_trace_fallback_reason(trace),
    )
