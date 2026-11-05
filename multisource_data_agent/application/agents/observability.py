"""Lightweight request tracing helpers for agent workflows."""

from __future__ import annotations

import time
import uuid
from contextlib import contextmanager
from contextvars import ContextVar
from functools import wraps
from typing import Any, Dict, List, Optional

from multisource_data_agent.infrastructure.core.logger import get_logger

trace_logger = get_logger(service="agent-trace")

Trace = Dict[str, Any]
TRACE_SCHEMA_VERSION = "b1"
_request_trace: ContextVar[Any] = ContextVar("agent_request_trace", default=None)
_tool_call: ContextVar[str] = ContextVar("agent_tool_call", default="")


@contextmanager
def request_trace_scope(trace: Trace):
    token = _request_trace.set(trace)
    try:
        yield
    finally:
        _request_trace.reset(token)


def current_request_trace() -> Optional[Trace]:
    return _request_trace.get()


def observe_tool_call(function):
    """Correlate concurrent tool events without changing execution or retries."""
    @wraps(function)
    async def wrapped(*args, **kwargs):
        token = _tool_call.set("tool_" + uuid.uuid4().hex)
        try:
            return await function(*args, **kwargs)
        finally:
            _tool_call.reset(token)
    return wrapped
_SENSITIVE_KEY_MARKERS = (
    "api_key",
    "apikey",
    "password",
    "passwd",
    "secret",
    "authorization",
    "access_token",
    "refresh_token",
)


def _redact_text(value: Any) -> str:
    text = str(value)
    lowered = text.lower()
    if any(scheme in lowered for scheme in ("mysql://", "postgres://", "postgresql://", "neo4j://", "bolt://", "redis://")):
        return "<redacted>"
    if "sk-" in lowered:
        return "<redacted>"
    if any(f"{marker}=" in lowered or f"{marker}:" in lowered for marker in _SENSITIVE_KEY_MARKERS):
        return "<redacted>"
    return text


def execution_success(status: str) -> bool:
    """Map a ToolResult status to completed tool execution."""
    return status in {"success", "empty"}


def trace_execution_success(trace: Optional[Trace]) -> Optional[bool]:
    """Return aggregate tool execution status when results have been recorded."""
    if not isinstance(trace, dict):
        return None
    outcomes = [
        event.get("metadata", {}).get("execution_success")
        for event in trace.get("events", [])
        if isinstance(event, dict) and event.get("stage") == "tool_result"
    ]
    outcomes = [value for value in outcomes if isinstance(value, bool)]
    return all(outcomes) if outcomes else None


def create_trace(
    *,
    trace_id: Optional[str] = None,
    session_id: Optional[str] = None,
    user_id: Optional[str] = None,
    route: Optional[str] = None,
) -> Trace:
    """Create the shared trace object passed through one chat request."""
    now = time.time()
    return {
        "schema_version": TRACE_SCHEMA_VERSION,
        "trace_id": trace_id or str(uuid.uuid4()),
        "session_id": session_id,
        "user_id": user_id,
        "route": route,
        "started_at": now,
        "events": [],
        "selected_tools": [],
        "fallback": False,
        "status": "running",
    }


def get_trace(state: Any = None, config: Any = None) -> Optional[Trace]:
    """Best-effort extraction of the shared trace from graph state/config."""
    trace = _trace_from_config(config)
    if trace is not None:
        return trace

    if state is None:
        return None
    if isinstance(state, dict):
        value = state.get("eval_trace")
        return value if isinstance(value, dict) else None
    value = getattr(state, "eval_trace", None)
    return value if isinstance(value, dict) else None


def finish_trace(trace: Optional[Trace], *, route: Optional[str] = None, error: Optional[str] = None) -> None:
    if not trace:
        return
    for call in trace.get("llm_calls", []):
        if call.get("status") == "running":
            call["observed_duration_ms"] = round((time.perf_counter() - call.pop("_started_perf")) * 1000, 2)
            call["status"] = "interrupted"
    if trace.get("llm_calls"):
        trace["llm_usage_complete"] = all(call.get("status") == "completed" and (call.get("usage") or {}).get("total_tokens") is not None for call in trace["llm_calls"])
    trace["finished_at"] = time.time()
    trace["duration_ms"] = round((trace["finished_at"] - trace.get("started_at", trace["finished_at"])) * 1000, 2)
    if route:
        trace["route"] = route
    if error:
        trace["error"] = _shorten(_redact_text(error), 1000)
        trace["status"] = "error"
    else:
        trace["status"] = "completed"


def trace_event(
    trace: Optional[Trace],
    stage: str,
    action: str,
    *,
    success: Optional[bool] = None,
    route: Optional[str] = None,
    next_node: Optional[str] = None,
    tool: Optional[str] = None,
    tools: Optional[List[str]] = None,
    question: Optional[str] = None,
    latency_ms: Optional[float] = None,
    error: Optional[Any] = None,
    error_code: Optional[str] = None,
    attempt: Optional[int] = None,
    fallback: Optional[bool] = None,
    metadata: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Append one structured event and log it with the trace id."""
    event: Dict[str, Any] = {
        "index": 0,
        "ts": time.time(),
        "stage": stage,
        "action": action,
    }
    if stage in {"tool_execution", "graph_strategy"} and _tool_call.get():
        event["call_id"] = _tool_call.get()
    if success is not None:
        event["success"] = success
    if route:
        event["route"] = route
    if next_node:
        event["next_node"] = next_node
    if tool:
        event["tool"] = tool
    if tools is not None:
        event["tools"] = list(tools)
    if question:
        event["question"] = _shorten(question, 500)
    if latency_ms is not None:
        event["latency_ms"] = round(float(latency_ms), 2)
    if error is not None:
        event["error"] = _shorten(_redact_text(error), 1000)
    if error_code:
        event["error_code"] = error_code
    if attempt is not None:
        event["attempt"] = int(attempt)
    if fallback is not None:
        event["fallback"] = fallback
    if metadata:
        event["metadata"] = _json_safe(metadata)

    if trace is not None:
        events = trace.setdefault("events", [])
        event["index"] = len(events) + 1
        events.append(event)
        if route:
            trace["route"] = route
        if fallback:
            trace["fallback"] = True
        _merge_selected_tools(trace, tool=tool, tools=tools)
        trace_id = trace.get("trace_id")
    else:
        trace_id = None

    trace_logger.info(
        "[TRACE] trace_id={} stage={} action={} route={} next_node={} tool={} success={} fallback={}",
        trace_id or "-",
        stage,
        action,
        route or "",
        next_node or "",
        tool or "",
        success if success is not None else "",
        fallback if fallback is not None else "",
    )
    return event


def merge_trace(target: Optional[Trace], source: Optional[Trace]) -> Trace:
    """Merge events from a sub-trace into the request trace."""
    if target is None:
        return source or create_trace()
    if not source or target is source:
        return target

    existing = {
        (
            item.get("stage"),
            item.get("action"),
            item.get("tool"),
            item.get("question"),
            item.get("index"),
        )
        for item in target.get("events", [])
        if isinstance(item, dict)
    }
    for event in source.get("events", []):
        if not isinstance(event, dict):
            continue
        signature = (
            event.get("stage"),
            event.get("action"),
            event.get("tool"),
            event.get("question"),
            event.get("index"),
        )
        if signature in existing:
            continue
        copied = dict(event)
        copied["index"] = len(target.setdefault("events", [])) + 1
        target["events"].append(copied)
        existing.add(signature)

    _merge_selected_tools(target, tools=source.get("selected_tools") or [])
    if source.get("fallback"):
        target["fallback"] = True
    for key in ("token_usage", "token_cost"):
        if source.get(key) is not None:
            target[key] = source[key]
    return target


def attach_trace_to_message(message: Any, trace: Optional[Trace]) -> Any:
    if trace is None:
        return message
    additional_kwargs = getattr(message, "additional_kwargs", None)
    if additional_kwargs is None:
        return message
    additional_kwargs["eval_trace"] = trace
    return message


def elapsed_ms(started_at: float) -> float:
    return (time.perf_counter() - started_at) * 1000


def _trace_from_config(config: Any) -> Optional[Trace]:
    if not config:
        return None
    if isinstance(config, dict):
        configurable = config.get("configurable", {})
        if isinstance(configurable, dict) and isinstance(configurable.get("eval_trace"), dict):
            return configurable["eval_trace"]
        return None
    configurable = getattr(config, "configurable", None)
    if isinstance(configurable, dict) and isinstance(configurable.get("eval_trace"), dict):
        return configurable["eval_trace"]
    getter = getattr(config, "get", None)
    if callable(getter):
        try:
            configurable = getter("configurable", {})
            if isinstance(configurable, dict) and isinstance(configurable.get("eval_trace"), dict):
                return configurable["eval_trace"]
        except Exception:
            return None
    return None


def _merge_selected_tools(trace: Trace, *, tool: Optional[str] = None, tools: Optional[List[str]] = None) -> None:
    selected = trace.setdefault("selected_tools", [])
    candidates: List[str] = []
    if tool:
        candidates.append(tool)
    if tools:
        candidates.extend(str(item) for item in tools if item)
    for candidate in candidates:
        if candidate not in selected:
            selected.append(candidate)


def _json_safe(value: Any) -> Any:
    if isinstance(value, str):
        return _redact_text(value)
    if value is None or isinstance(value, (int, float, bool)):
        return value
    if isinstance(value, dict):
        safe = {}
        for key, item in value.items():
            key_text = str(key)
            lowered = key_text.lower()
            if any(marker in lowered for marker in _SENSITIVE_KEY_MARKERS):
                safe[key_text] = "<redacted>"
            else:
                safe[key_text] = _json_safe(item)
        return safe
    if isinstance(value, list):
        return [_json_safe(item) for item in value]
    if isinstance(value, tuple):
        return [_json_safe(item) for item in value]
    return _shorten(str(value), 1000)


def _shorten(value: str, limit: int) -> str:
    if len(value) <= limit:
        return value
    return value[: limit - 3] + "..."
