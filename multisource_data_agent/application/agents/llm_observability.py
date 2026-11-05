"""Metadata-only callbacks: never capture prompts, generated text or credentials.

SDK-internal retries and first-token latency are unknown for non-streaming calls.
Observing a LangChain retry callback does not make hidden SDK retries observable.
"""

from __future__ import annotations

import time
from typing import Any

from langchain_core.callbacks import BaseCallbackHandler

from .observability import current_request_trace, trace_event


def _label(value: Any) -> str:
    value = str(value or "unknown")
    return "unknown" if "://" in value or "sk-" in value.lower() else value[:120]


def _usage(response: Any) -> dict[str, int] | None:
    output = getattr(response, "llm_output", None) or {}
    usage = output.get("token_usage") or output.get("usage") or {}
    if not usage:
        for group in getattr(response, "generations", []):
            for generation in group:
                usage = getattr(getattr(generation, "message", None), "usage_metadata", None) or {}
                if usage:
                    break
            if usage:
                break
    result = {}
    for key, aliases in {
        "input_tokens": ("input_tokens", "prompt_tokens"),
        "output_tokens": ("output_tokens", "completion_tokens"),
        "total_tokens": ("total_tokens",),
    }.items():
        value = next((usage[a] for a in aliases if isinstance(usage.get(a), int) and not isinstance(usage[a], bool) and usage[a] >= 0), None)
        if value is not None:
            result[key] = value
    if "total_tokens" not in result and {"input_tokens", "output_tokens"} <= result.keys():
        result["total_tokens"] = result["input_tokens"] + result["output_tokens"]
    return result or None


def _error_chain(error: BaseException) -> list[dict[str, Any]]:
    """Keep bounded exception types/codes, never messages or request objects."""
    chain, seen = [], set()
    while isinstance(error, BaseException) and id(error) not in seen and len(chain) < 5:
        seen.add(id(error))
        item = {"class": _label(type(error).__name__)}
        for key in ("errno", "winerror", "verify_code"):
            value = getattr(error, key, None)
            if isinstance(value, int) and not isinstance(value, bool):
                item[key] = value
        chain.append(item)
        error = error.__cause__ or error.__context__
    return chain


def _node(metadata: dict, parameters: dict, tags: list) -> str:
    if metadata.get("langgraph_node"):
        return _label(metadata["langgraph_node"])
    # Some nested Runnable chains omit graph metadata (notably on Python 3.10).
    # Identify our structured schemas, never inspect message/prompt contents.
    roles = {"Router": "router", "PlannerOutput": "planner", "AnswerSynthesis": "summarize"}
    for tool in parameters.get("tools") or []:
        if isinstance(tool, dict):
            name = (tool.get("function") or {}).get("name")
            if name in roles:
                return roles[name]
    return next((_label(tag) for tag in tags if tag in {"router", "text2sql", "research_plan", "general_query", "additional_info"}), "unknown")


class LLMObservationCallback(BaseCallbackHandler):
    run_inline = True

    def on_chat_model_start(self, serialized, messages, *, run_id, metadata=None, **kwargs):
        trace = current_request_trace()
        if trace is None:
            return
        parameters = kwargs.get("invocation_params") or {}
        metadata = metadata or {}
        record = {
            "call_id": str(run_id),
            "node": _node(metadata, parameters, kwargs.get("tags") or []),
            "model": _label(parameters.get("model") or parameters.get("model_name") or metadata.get("ls_model_name")),
            "status": "running", "duration_ms": None, "first_token_ms": None,
            "sdk_retry_count": None, "callback_retry_count": 0, "usage": None,
            "_started_perf": time.perf_counter(),
        }
        trace.setdefault("llm_calls", []).append(record)
        trace_event(trace, "llm_call", "start", metadata={k: record[k] for k in ("call_id", "node", "model")})

    def _record(self, run_id):
        trace = current_request_trace()
        if trace is None:
            return None, None
        return trace, next((r for r in trace.get("llm_calls", []) if r["call_id"] == str(run_id)), None)

    def on_retry(self, retry_state, *, run_id, **kwargs):
        _, record = self._record(run_id)
        if record is not None:
            record["callback_retry_count"] += 1

    def on_llm_end(self, response, *, run_id, **kwargs):
        output = getattr(response, "llm_output", None) or {}
        self._finish(run_id, usage=_usage(response), response_model=output.get("model_name"))

    def on_llm_error(self, error, *, run_id, **kwargs):
        self._finish(run_id, error_code=type(error).__name__, error_chain=_error_chain(error))

    def _finish(self, run_id, *, usage=None, error_code=None, error_chain=None, response_model=None):
        trace, record = self._record(run_id)
        if record is None or record["status"] != "running":
            return
        record["duration_ms"] = round((time.perf_counter() - record.pop("_started_perf")) * 1000, 2)
        record.update(status="error" if error_code else "completed", usage=usage)
        if response_model:
            record["response_model"] = _label(response_model)
        if error_code:
            record["error_code"] = error_code
            record["error_chain"] = error_chain or []
        trace_event(trace, "llm_call", "error" if error_code else "done", success=not bool(error_code),
                    latency_ms=record["duration_ms"], error_code=error_code,
                    metadata={k: v for k, v in record.items() if k not in {"status", "duration_ms"}})
        calls = trace["llm_calls"]
        trace["llm_usage_complete"] = all(c["status"] == "completed" and (c.get("usage") or {}).get("total_tokens") is not None for c in calls)
        trace["token_usage"] = {"total_tokens": sum((c.get("usage") or {}).get("total_tokens", 0) for c in calls)}
