"""Unified execution interface for the three core Data Agent tools.

Phase 1 converges SQL, knowledge retrieval, and graph execution behind one
small application-level contract without changing the current Router/Planner
or legacy LangGraph node names.  Phase 2 can depend on this module directly
and see exactly three first-class tools:

- ``sql_agent``
- ``knowledge_rag``
- ``graph_tool``

Legacy names are aliases only; they never create additional first-class tools.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Dict, Iterable, Literal, Mapping, Optional, Protocol, Tuple, runtime_checkable

from multisource_data_agent.application.agents.tool_contracts import ToolResult
from multisource_data_agent.application.agents.observability import observe_tool_call, trace_event
from multisource_data_agent.infrastructure.core.logger import get_logger

logger = get_logger(service="core-tool-executor")


CoreToolName = Literal["sql_agent", "knowledge_rag", "graph_tool"]
CoreToolKind = Literal["sql", "rag", "graph"]

CORE_TOOL_NAMES: Tuple[CoreToolName, ...] = (
    "sql_agent",
    "knowledge_rag",
    "graph_tool",
)


@runtime_checkable
class DataTool(Protocol):
    """Structural contract implemented by every first-class Data Agent tool."""

    tool_name: str
    tool_kind: str

    async def execute(
        self,
        task: str,
        context: Optional[Dict[str, Any]] = None,
        *,
        trace: Any = None,
    ) -> ToolResult:
        """Execute one task and return the shared ``ToolResult`` contract."""
        ...


@dataclass(frozen=True)
class ToolDescriptor:
    """Stable metadata exposed to future orchestration layers."""

    name: CoreToolName
    kind: CoreToolKind
    description: str
    aliases: Tuple[str, ...] = ()

    def as_dict(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "kind": self.kind,
            "description": self.description,
            "aliases": list(self.aliases),
        }


@dataclass(frozen=True)
class ToolBinding:
    """One descriptor bound to one executable tool instance.

    ``alias_context_defaults`` preserves meaningful legacy alias intent without
    creating extra first-class tools.  For example, ``predefined_cypher`` maps
    to ``graph_tool`` with ``strategy=predefined``.
    """

    descriptor: ToolDescriptor
    tool: DataTool
    alias_context_defaults: Mapping[str, Mapping[str, Any]] = field(default_factory=dict)


class UnknownToolError(LookupError):
    """Raised when a requested tool/alias is not registered."""


class DuplicateToolAliasError(ValueError):
    """Raised when two tool bindings claim the same normalized name."""


def _error_category(metadata: Mapping[str, Any]) -> str:
    """Map heterogeneous backend errors to a small reliability taxonomy."""
    code = str(metadata.get("error_code") or metadata.get("execution_error_code") or "").lower()
    text = f"{metadata.get('error') or ''} {metadata.get('error_type') or ''}".lower()
    combined = f"{code} {text}"
    if "read_only" in combined or "policy" in combined:
        return "policy_rejection"
    if any(marker in combined for marker in ("permission", "access denied", "unauthorized", "authentication")):
        return "permission_denied"
    if "timeout" in combined:
        return "timeout"
    if any(marker in combined for marker in ("connection", "unavailable", "transport", "network")):
        return "service_unavailable"
    return "tool_error"


_TRANSIENT_HTTP_STATUSES = {429, 502, 503}
_TERMINAL_RETRY_MARKERS = (
    "read_only",
    "policy",
    "validation",
    "invalid_argument",
    "invalid request",
    "bad request",
    "unauthorized",
    "authentication",
    "forbidden",
    "permission",
    "insufficient_quota",
    "insufficient quota",
    "quota exhausted",
    "billing",
    "balance",
    "credit",
    "payment required",
)
_CONNECTION_RETRY_MARKERS = (
    "connection error",
    "connection_error",
    "connectionerror",
    "connecterror",
    "connection refused",
    "connection reset",
    "connection aborted",
    "network error",
    "network_error",
    "network unreachable",
    "transport error",
    "transport_error",
    "temporary failure",
    "temporarily unavailable",
    "service unavailable",
    "service_unavailable",
)


def _coerce_http_status(metadata: Mapping[str, Any]) -> Optional[int]:
    for key in ("http_status", "http_status_code", "status_code", "response_status"):
        raw = metadata.get(key)
        if raw is None:
            continue
        try:
            return int(raw)
        except (TypeError, ValueError):
            continue
    return None


def _transient_retry_reason(metadata: Mapping[str, Any]) -> Optional[str]:
    """Return a retry reason only for bounded transient transport failures.

    Policy/validation/auth/billing failures are terminal even when a provider
    happens to surface them with HTTP 429. Timeouts belong to the separate
    timeout-budget phase and are intentionally not retried here.
    """

    parts = [
        metadata.get("error_code"),
        metadata.get("execution_error_code"),
        metadata.get("error_category"),
        metadata.get("error_type"),
        metadata.get("error"),
        metadata.get("message"),
    ]
    combined = " ".join(str(part) for part in parts if part is not None).lower()

    if any(marker in combined for marker in _TERMINAL_RETRY_MARKERS):
        return None
    if "timeout" in combined or "timed out" in combined:
        return None

    status = _coerce_http_status(metadata)
    if status is not None:
        if status in _TRANSIENT_HTTP_STATUSES:
            return f"http_{status}"
        if 400 <= status < 500:
            return None

    if any(marker in combined for marker in _CONNECTION_RETRY_MARKERS):
        return "connection"
    return None


def normalize_tool_name(name: str) -> str:
    """Normalize orchestration-facing tool names without changing semantics."""

    return str(name or "").strip().lower().replace("-", "_")


class ToolRegistry:
    """Registry exposing exactly the canonical Data Agent tool surface.

    The registry is intentionally small.  It does not perform routing or tool
    selection; it only resolves canonical names and legacy aliases to an
    executable boundary.  That keeps Phase 1 separate from the Phase 2
    Router/Planner redesign.
    """

    def __init__(self, bindings: Iterable[ToolBinding] = ()) -> None:
        self._bindings: Dict[str, ToolBinding] = {}
        self._aliases: Dict[str, str] = {}
        for binding in bindings:
            self.register(binding)

    def register(self, binding: ToolBinding) -> None:
        canonical = normalize_tool_name(binding.descriptor.name)
        if canonical not in CORE_TOOL_NAMES:
            raise ValueError(f"unsupported canonical tool: {binding.descriptor.name}")
        if canonical in self._bindings:
            raise DuplicateToolAliasError(f"duplicate canonical tool: {canonical}")
        if not callable(getattr(binding.tool, "execute", None)):
            raise TypeError(f"tool {canonical} does not implement execute()")

        declared_name = normalize_tool_name(getattr(binding.tool, "tool_name", ""))
        declared_kind = str(getattr(binding.tool, "tool_kind", "") or "").strip().lower()
        if declared_name and declared_name != canonical:
            raise ValueError(
                f"tool metadata name {declared_name!r} does not match {canonical!r}"
            )
        if declared_kind and declared_kind != binding.descriptor.kind:
            raise ValueError(
                f"tool metadata kind {declared_kind!r} does not match "
                f"{binding.descriptor.kind!r}"
            )

        claimed_names = (canonical, *binding.descriptor.aliases)
        normalized_claims = [normalize_tool_name(item) for item in claimed_names]
        for alias in normalized_claims:
            if not alias:
                raise ValueError(f"empty alias registered for tool: {canonical}")
            owner = self._aliases.get(alias)
            if owner is not None and owner != canonical:
                raise DuplicateToolAliasError(
                    f"tool alias {alias!r} is already owned by {owner!r}"
                )

        declared_aliases = set(normalized_claims)
        for alias in binding.alias_context_defaults:
            normalized_alias = normalize_tool_name(alias)
            if normalized_alias not in declared_aliases:
                raise ValueError(
                    f"context defaults reference undeclared alias {alias!r} "
                    f"for tool {canonical!r}"
                )

        self._bindings[canonical] = binding
        for alias in normalized_claims:
            self._aliases[alias] = canonical

    def resolve(self, name: str) -> ToolBinding:
        normalized = normalize_tool_name(name)
        canonical = self._aliases.get(normalized)
        if canonical is None:
            raise UnknownToolError(normalized or "<empty>")
        return self._bindings[canonical]

    def canonical_name(self, name: str) -> CoreToolName:
        return self.resolve(name).descriptor.name

    def descriptors(self) -> Tuple[ToolDescriptor, ...]:
        return tuple(
            self._bindings[name].descriptor
            for name in CORE_TOOL_NAMES
            if name in self._bindings
        )

    def names(self) -> Tuple[CoreToolName, ...]:
        return tuple(descriptor.name for descriptor in self.descriptors())

    def aliases(self) -> Mapping[str, str]:
        return dict(self._aliases)


def _trace_event_count(trace: Any) -> int:
    if not isinstance(trace, dict):
        return 0
    events = trace.get("events")
    return len(events) if isinstance(events, list) else 0


def _canonicalize_trace_segment(
    trace: Any,
    registry: ToolRegistry,
    canonical_name: CoreToolName,
    start_index: int,
) -> None:
    """Normalize backend/legacy tool aliases emitted inside one core execution.

    Some Phase-1 compatibility adapters still emit their historical native name
    (notably ``text2sql_query``). Once execution enters the unified boundary,
    raw observability should expose the canonical tool identity while retaining
    the native name in event metadata for debugging.
    """

    if not isinstance(trace, dict):
        return
    events = trace.get("events")
    if isinstance(events, list):
        for event in events[start_index:]:
            if not isinstance(event, dict):
                continue
            raw_name = event.get("tool") or event.get("tool_name")
            if not raw_name:
                continue
            try:
                resolved = registry.canonical_name(str(raw_name))
            except UnknownToolError:
                continue
            if resolved != canonical_name:
                continue
            normalized_raw = normalize_tool_name(str(raw_name))
            if normalized_raw != canonical_name:
                metadata = event.get("metadata")
                metadata = dict(metadata) if isinstance(metadata, dict) else {}
                metadata.setdefault("native_tool_name", normalized_raw)
                event["metadata"] = metadata
            if "tool" in event:
                event["tool"] = canonical_name
            if "tool_name" in event:
                event["tool_name"] = canonical_name

    selected = trace.get("selected_tools")
    if isinstance(selected, list):
        normalized_selected = []
        for raw_name in selected:
            name = str(raw_name)
            try:
                name = registry.canonical_name(name)
            except UnknownToolError:
                name = normalize_tool_name(name) or name
            if name not in normalized_selected:
                normalized_selected.append(name)
        trace["selected_tools"] = normalized_selected


class UnifiedToolExecutor:
    """Single execution entry point for SQL, RAG, and graph tools.

    Individual tools keep their current internal implementation and legacy
    adapters.  This executor adds only cross-tool concerns:

    - canonical tool identity;
    - shared error containment;
    - one ``ToolResult`` output contract;
    - alias compatibility for legacy orchestration names.
    """

    def __init__(
        self,
        registry: ToolRegistry,
        *,
        max_transient_retries: int = 1,
        retry_backoff_seconds: float = 0.25,
        timeout_seconds: float = 100.0,
    ) -> None:
        self.registry = registry
        self.max_transient_retries = max(0, int(max_transient_retries))
        self.retry_backoff_seconds = max(0.0, float(retry_backoff_seconds))
        self.timeout_seconds = min(max(float(timeout_seconds), 0.001), 300.0)

    @observe_tool_call
    async def execute(
        self,
        tool_name: str,
        task: str,
        context: Optional[Dict[str, Any]] = None,
        *,
        trace: Any = None,
    ) -> ToolResult:
        requested = normalize_tool_name(tool_name)
        try:
            binding = self.registry.resolve(requested)
        except UnknownToolError:
            trace_event(
                trace,
                "tool_execution",
                "error",
                tool=requested or "unknown_tool",
                success=False,
                error_code="unknown_tool",
                error=f"unknown core tool: {tool_name}",
                metadata={"available_tools": list(self.registry.names())},
            )
            return ToolResult(
                tool_name=requested or "unknown_tool",
                status="error",
                task=task,
                metadata={
                    "error": f"unknown core tool: {tool_name}",
                    "error_code": "unknown_tool",
                    "error_category": "tool_error",
                    "requested_tool_name": requested,
                    "available_tools": list(self.registry.names()),
                },
            )

        descriptor = binding.descriptor
        effective_context = dict(context or {})
        alias_defaults = binding.alias_context_defaults.get(requested)
        if alias_defaults is None:
            for alias, defaults in binding.alias_context_defaults.items():
                if normalize_tool_name(alias) == requested:
                    alias_defaults = defaults
                    break
        for key, value in dict(alias_defaults or {}).items():
            effective_context.setdefault(key, value)

        trace_start = _trace_event_count(trace)
        retry_count = 0
        attempt = 1
        while True:
            attempt_started_at = time.perf_counter()
            try:
                result = await asyncio.wait_for(
                    binding.tool.execute(task, effective_context, trace=trace),
                    timeout=self.timeout_seconds,
                )
            except asyncio.TimeoutError as exc:
                _canonicalize_trace_segment(
                    trace, self.registry, descriptor.name, trace_start
                )
                elapsed = (time.perf_counter() - attempt_started_at) * 1000
                trace_event(
                    trace,
                    "tool_execution",
                    "error",
                    tool=descriptor.name,
                    success=False,
                    error="tool timeout budget exceeded",
                    error_code="tool_timeout",
                    attempt=attempt,
                    fallback=False,
                    latency_ms=elapsed,
                    metadata={
                        "error_category": "timeout",
                        "timeout_budget_ms": round(self.timeout_seconds * 1000, 2),
                        "retry_count": retry_count,
                    },
                )
                return ToolResult(
                    tool_name=descriptor.name,
                    tool_kind=descriptor.kind,
                    status="error",
                    task=task,
                    metadata={
                        "error": f"tool execution exceeded {self.timeout_seconds:g}s timeout budget",
                        "error_type": "TimeoutError",
                        "error_code": "tool_timeout",
                        "error_category": "timeout",
                        "timeout_budget_ms": round(self.timeout_seconds * 1000, 2),
                        "elapsed_ms": round(elapsed, 2),
                        "requested_tool_name": requested,
                        "canonical_tool_name": descriptor.name,
                        "retry_count": retry_count,
                        "attempts": attempt,
                    },
                )
            except Exception as exc:  # pragma: no cover - defensive boundary
                error_text = str(exc).strip() or repr(exc)
                retry_reason = _transient_retry_reason(
                    {
                        "error": error_text,
                        "error_type": type(exc).__name__,
                    }
                )
                if retry_reason and retry_count < self.max_transient_retries:
                    retry_count += 1
                    attempt += 1
                    trace_event(
                        trace,
                        "tool_execution",
                        "retry",
                        tool=descriptor.name,
                        success=False,
                        error_code="transient_retry",
                        error=error_text,
                        attempt=attempt,
                        fallback=False,
                        metadata={
                            "retry_reason": retry_reason,
                            "retry_count": retry_count,
                            "backoff_seconds": self.retry_backoff_seconds,
                            "error_type": type(exc).__name__,
                        },
                    )
                    if self.retry_backoff_seconds:
                        await asyncio.sleep(self.retry_backoff_seconds)
                    continue

                _canonicalize_trace_segment(
                    trace, self.registry, descriptor.name, trace_start
                )
                logger.exception(
                    "Core tool {} execution failed: {}",
                    descriptor.name,
                    exc,
                )
                trace_event(
                    trace,
                    "tool_execution",
                    "error",
                    tool=descriptor.name,
                    success=False,
                    error_code="tool_execution_exception",
                    error=error_text,
                    attempt=attempt,
                    metadata={
                        "error_type": type(exc).__name__,
                        "retry_count": retry_count,
                    },
                )
                return ToolResult(
                    tool_name=descriptor.name,
                    tool_kind=descriptor.kind,
                    status="error",
                    task=task,
                    metadata={
                        "error": error_text,
                        "error_type": type(exc).__name__,
                        "error_code": "tool_execution_exception",
                        "error_category": _error_category(
                            {
                                "error": error_text,
                                "error_type": type(exc).__name__,
                                "error_code": "tool_execution_exception",
                            }
                        ),
                        "requested_tool_name": requested,
                        "canonical_tool_name": descriptor.name,
                        "retry_count": retry_count,
                        "attempts": attempt,
                    },
                )

            if not isinstance(result, ToolResult):
                break

            retry_reason = (
                _transient_retry_reason(dict(result.metadata or {}))
                if result.status == "error"
                else None
            )
            if retry_reason and retry_count < self.max_transient_retries:
                retry_count += 1
                attempt += 1
                trace_event(
                    trace,
                    "tool_execution",
                    "retry",
                    tool=descriptor.name,
                    success=False,
                    error_code="transient_retry",
                    error=(result.metadata or {}).get("error"),
                    attempt=attempt,
                    fallback=False,
                    metadata={
                        "retry_reason": retry_reason,
                        "retry_count": retry_count,
                        "backoff_seconds": self.retry_backoff_seconds,
                        "previous_error_code": (result.metadata or {}).get("error_code")
                        or (result.metadata or {}).get("execution_error_code"),
                    },
                )
                if self.retry_backoff_seconds:
                    await asyncio.sleep(self.retry_backoff_seconds)
                continue
            break

        _canonicalize_trace_segment(
            trace, self.registry, descriptor.name, trace_start
        )

        if not isinstance(result, ToolResult):
            trace_event(
                trace,
                "tool_execution",
                "error",
                tool=descriptor.name,
                success=False,
                error_code="invalid_tool_result",
                error="tool returned a non-ToolResult payload",
                metadata={"error_type": type(result).__name__},
            )
            return ToolResult(
                tool_name=descriptor.name,
                tool_kind=descriptor.kind,
                status="error",
                task=task,
                metadata={
                    "error": "tool returned a non-ToolResult payload",
                    "error_type": type(result).__name__,
                    "error_code": "invalid_tool_result",
                    "error_category": "tool_error",
                    "requested_tool_name": requested,
                    "canonical_tool_name": descriptor.name,
                },
            )

        metadata = dict(result.metadata or {})
        if result.status == "error":
            metadata.setdefault("error_category", _error_category(metadata))
        metadata.update(
            {
                "requested_tool_name": requested,
                "canonical_tool_name": descriptor.name,
                "native_tool_name": result.tool_name,
                "retry_count": retry_count,
                "attempts": attempt,
            }
        )
        canonical_result = result.model_copy(
            update={
                "tool_name": descriptor.name,
                "tool_kind": descriptor.kind,
                "task": result.task or task,
                "metadata": metadata,
            }
        )
        # Assign deterministic evidence IDs before the result enters LangGraph
        # state. The local import keeps the lightweight registry import free
        # of any workflow initialization.
        from multisource_data_agent.application.agents.execution_contract import normalize_tool_results

        return normalize_tool_results([canonical_result])[0]



def create_unified_tool_node(
    executor: UnifiedToolExecutor,
    tool_name: CoreToolName,
) -> Callable[[Dict[str, Any]], Awaitable[Dict[str, Any]]]:
    """Adapt the unified executor to the current LangGraph state contract.

    Phase 2 orchestration dispatches only canonical tool names.  The adapter
    keeps legacy ``cyphers`` history populated for the existing summarizer/API
    while the semantic result lives in ``tool_results``.
    """

    canonical = executor.registry.canonical_name(tool_name)

    async def unified_tool_node(state: Dict[str, Any]) -> Dict[str, Any]:
        task = state.get("task") or state.get("question") or ""
        trace = state.get("eval_trace")
        raw_context = state.get("query_parameters")
        context = dict(raw_context) if isinstance(raw_context, dict) else {}
        planner_context = state.get("planner_context")
        planner_context = (
            dict(planner_context) if isinstance(planner_context, dict) else {}
        )

        result = await executor.execute(canonical, task, context, trace=trace)
        if planner_context:
            metadata = dict(result.metadata or {})
            metadata["planner_task"] = planner_context
            result = result.model_copy(update={"metadata": metadata})
            from multisource_data_agent.application.agents.execution_contract import normalize_tool_results

            result = normalize_tool_results([result])[0]
        step = f"execute_{canonical}"
        statement = result.statement.text if result.statement else ""
        parameters = result.statement.parameters if result.statement else {}

        raw_errors = result.metadata.get("errors") if isinstance(result.metadata, dict) else None
        if isinstance(raw_errors, str):
            errors = [raw_errors]
        elif isinstance(raw_errors, (list, tuple)):
            errors = [str(item) for item in raw_errors if item]
        else:
            error = result.metadata.get("error") if isinstance(result.metadata, dict) else None
            errors = [str(error)] if error and result.status == "error" else []

        records: Any = result.data or []
        if result.tool_kind == "sql":
            records = {
                "answer": result.answer or "",
                "rows": result.data or [],
                "sql": statement,
            }
        elif result.tool_kind == "rag":
            records = {
                "answer": result.answer or "",
                "documents": result.data or [],
            }

        return {
            "cyphers": [
                {
                    "task": task,
                    "query": statement,
                    "statement": statement,
                    "parameters": parameters,
                    "errors": errors,
                    "records": records,
                    "steps": [step],
                }
            ],
            "tool_results": [result],
            "evidence": list(result.evidence),
            "steps": [step],
        }

    unified_tool_node.__name__ = canonical
    return unified_tool_node

def create_default_tool_registry(
    *,
    sql_agent: Optional[DataTool] = None,
    knowledge_rag: Optional[DataTool] = None,
    graph_tool: Optional[DataTool] = None,
) -> ToolRegistry:
    """Build the canonical three-tool registry.

    Dependencies may be injected for tests or specialized runtime setup.  The
    imports are local so importing the interface itself does not initialize any
    database/LLM client.
    """

    if sql_agent is None:
        from .sql_agent import SQLAgent

        sql_agent = SQLAgent()
    if knowledge_rag is None:
        from .knowledge_rag import KnowledgeRAGTool

        knowledge_rag = KnowledgeRAGTool()
    if graph_tool is None:
        from .graph_tool import GraphTool

        graph_tool = GraphTool()

    return ToolRegistry(
        (
            ToolBinding(
                descriptor=ToolDescriptor(
                    name="sql_agent",
                    kind="sql",
                    description=(
                        "Query structured relational data through the canonical "
                        "Text2SQL workflow and return SQL statement, rows, and evidence."
                    ),
                    aliases=("text2sql_query", "text2sql", "sql"),
                ),
                tool=sql_agent,
            ),
            ToolBinding(
                descriptor=ToolDescriptor(
                    name="knowledge_rag",
                    kind="rag",
                    description=(
                        "Retrieve grounded knowledge documents through the canonical "
                        "PGVector + reranker path and return citation-ready evidence."
                    ),
                    aliases=(
                        "knowledge_retrieval",
                        "postgres",
                        "pgvector",
                        "rag",
                    ),
                ),
                tool=knowledge_rag,
            ),
            ToolBinding(
                descriptor=ToolDescriptor(
                    name="graph_tool",
                    kind="graph",
                    description=(
                        "Query Neo4j relationships using predefined Cypher or dynamic "
                        "Text2Cypher behind one graph execution boundary."
                    ),
                    aliases=(
                        "cypher_query",
                        "predefined_cypher",
                        "text2cypher",
                        "graph",
                    ),
                ),
                tool=graph_tool,
                alias_context_defaults={
                    "predefined_cypher": {"strategy": "predefined"},
                    "cypher_query": {"strategy": "text2cypher"},
                    "text2cypher": {"strategy": "text2cypher"},
                },
            ),
        )
    )


def create_default_tool_executor(
    *,
    sql_agent: Optional[DataTool] = None,
    knowledge_rag: Optional[DataTool] = None,
    graph_tool: Optional[DataTool] = None,
    timeout_seconds: Optional[float] = None,
) -> UnifiedToolExecutor:
    """Return the Phase 1 unified executor over the three core tools."""

    from multisource_data_agent.config import settings

    return UnifiedToolExecutor(
        create_default_tool_registry(
            sql_agent=sql_agent,
            knowledge_rag=knowledge_rag,
            graph_tool=graph_tool,
        ),
        timeout_seconds=(
            settings.CORE_TOOL_TIMEOUT_SECONDS
            if timeout_seconds is None
            else timeout_seconds
        ),
    )


__all__ = [
    "CORE_TOOL_NAMES",
    "CoreToolKind",
    "CoreToolName",
    "DataTool",
    "DuplicateToolAliasError",
    "ToolBinding",
    "ToolDescriptor",
    "ToolRegistry",
    "UnifiedToolExecutor",
    "UnknownToolError",
    "create_default_tool_executor",
    "create_unified_tool_node",
    "create_default_tool_registry",
    "normalize_tool_name",
]
