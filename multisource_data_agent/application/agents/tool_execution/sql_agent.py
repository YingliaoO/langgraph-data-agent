"""Canonical SQL Agent boundary for the Data Agent.

The SQL domain workflow lives under ``multisource_data_agent.application.agents.text2sql``.
This module exposes a stable application-level SQL tool boundary while keeping
a LangGraph-node adapter for the current multi-tool workflow.
"""

from typing import Any, Callable, Coroutine, Dict, List, Optional
import time

from pydantic import BaseModel

from multisource_data_agent.application.agents.tool_contracts import Evidence, Statement, ToolResult
from multisource_data_agent.application.agents.observability import elapsed_ms, execution_success, trace_event
from multisource_data_agent.config import settings
from multisource_data_agent.application.agents.llm_observability import LLMObservationCallback
from multisource_data_agent.infrastructure.core.logger import get_logger

logger = get_logger(service="text2sql-tool")


_TEXT2SQL_LLM: Optional[Any] = None


def _get_text2sql_llm() -> Any:
    """Return the process-local Text2SQL client while preserving its config."""
    global _TEXT2SQL_LLM
    if _TEXT2SQL_LLM is None:
        from langchain_openai import ChatOpenAI

        _TEXT2SQL_LLM = ChatOpenAI(
            callbacks=[LLMObservationCallback()],
            openai_api_key=settings.OPENAI_API_KEY,
            model_name=settings.OPENAI_MODEL,
            openai_api_base=settings.OPENAI_API_BASE,
            temperature=0.0,
            tags=["text2sql"],
        )
        logger.info("Initialized shared Text2SQL LLM client")
    return _TEXT2SQL_LLM


class Text2SQLToolOutputState(BaseModel):
    """Text2SQL result shape consumed by the existing multi-tool workflow."""

    task: str
    query: str
    errors: List[str]
    records: Dict[str, Any]
    steps: List[str]


def create_sql_agent_node(
    _legacy_graph: object | None = None,
) -> Callable[[Dict[str, Any]], Coroutine[Any, Any, Dict[str, Any]]]:
    """
    Create a LangGraph node that executes the Text2SQL workflow.

    Parameters
    ----------
    _legacy_graph : object | None
        Ignored compatibility argument for the existing multi-tool wiring.
        Text2SQL retrieves schema metadata from MySQL INFORMATION_SCHEMA.
    """

    async def text2sql_query(state: Dict[str, Any]) -> Dict[str, Any]:
        question = state.get("task") or state.get("question") or ""
        tool_args: Dict[str, Any] = state.get("query_parameters", {}) or {}
        trace = state.get("eval_trace")
        started_at = time.perf_counter()
        trace_event(trace, "tool_execution", "start", tool="text2sql_query", question=question, metadata={"args": tool_args})
        logger.info(
            "[TOOL_EXEC] start | tool=text2sql_query | task=%s | args=%s",
            question,
            tool_args,
        )

        connection_id = tool_args.get("connection_id")
        db_type = tool_args.get("db_type") or "MySQL"
        max_rows = max(1, int(tool_args.get("max_rows") or settings.SQL_AGENT_MAX_ROWS))
        sql_timeout_ms = max(0, int(tool_args.get("sql_timeout_ms") or settings.SQL_AGENT_TIMEOUT_MS))
        connection_string = tool_args.get("connection_string")
        # Historical max_retries is accepted for compatibility, but Phase 3
        # caps LLM SQL repair to one attempt.
        max_retries = int(tool_args.get("max_retries") or 3)
        requested_max_repairs = tool_args.get("max_repairs")
        max_repairs = min(
            1,
            max(0, int(requested_max_repairs if requested_max_repairs is not None else 1)),
        )

        errors: List[str] = []

        if not settings.OPENAI_API_KEY:
            answer = "抱歉，系统暂时无法调用模型执行数据库查询，请稍后再试。"
            records_payload = {
                "answer": answer,
                "rows": [],
                "sql": "",
            }
            return {
                "cyphers": [
                    Text2SQLToolOutputState(
                        **{
                            "task": question,
                            "query": "",
                            "errors": errors + ["OPENAI_API_KEY 未配置"],
                            "records": records_payload,
                            "steps": ["execute_text2sql_query"],
                        }
                    )
                ],
                "tool_results": [
                    ToolResult(
                        tool_name="text2sql_query",
                        tool_kind="sql",
                        status="error",
                        task=question,
                        answer=answer,
                        metadata={"error": "OPENAI_API_KEY is not configured"},
                    )
                ],
                "steps": ["execute_text2sql_query"],
            }

        from multisource_data_agent.application.agents.text2sql import create_text2sql_workflow

        text2sql_llm = _get_text2sql_llm()

        workflow = create_text2sql_workflow(
            llm=text2sql_llm,
            db_type=db_type,
            connection_string=connection_string,
            max_retries=max_retries,
        )

        input_state = {
            "question": question,
            "connection_id": connection_id,
            "connection_string": connection_string,
            "db_type": db_type,
            "max_retries": max_retries,
            "max_repairs": max_repairs,
            "repair_count": 0,
            "max_rows": max_rows,
            "sql_timeout_ms": sql_timeout_ms,
        }

        try:
            result: Dict[str, Any] = await workflow.ainvoke(input_state)
        except Exception as exc:  # pragma: no cover - defensive logging
            logger.exception("Text2SQL workflow execution failed: %s", exc)
            error_message = f"执行数据库查询失败：{exc}"
            errors.append(error_message)
            answer = "抱歉，执行数据库查询时发生异常，请稍后再试。"
            records_payload = {
                "answer": answer,
                "rows": [],
                "sql": "",
                "error": error_message,
            }
            return {
                "cyphers": [
                    Text2SQLToolOutputState(
                        **{
                            "task": question,
                            "query": "",
                            "errors": errors,
                            "records": records_payload,
                            "steps": ["execute_text2sql_query"],
                        }
                    )
                ],
                "tool_results": [
                    ToolResult(
                        tool_name="text2sql_query",
                        tool_kind="sql",
                        status="error",
                        task=question,
                        answer=answer,
                        metadata={"error": error_message},
                    )
                ],
                "steps": ["execute_text2sql_query"],
            }

        answer = result.get("answer") or ""
        sql_statement = result.get("sql_statement") or ""
        execution_results = result.get("execution_results") or []
        execution_error = result.get("execution_error")
        workflow_steps = result.get("steps") or []
        validation_errors = result.get("validation_errors") or []
        validation_error_codes = result.get("validation_error_codes") or []
        validation_warnings = result.get("validation_warnings") or []
        referenced_tables = result.get("referenced_tables") or []
        referenced_columns = result.get("referenced_columns") or {}
        row_limit_applied = bool(result.get("row_limit_applied"))
        requested_limit = result.get("requested_limit")
        effective_limit = result.get("effective_limit") or max_rows
        effective_timeout_ms = result.get("sql_timeout_ms") or sql_timeout_ms
        timeout_supported = bool(result.get("timeout_supported"))
        execution_error_code = result.get("execution_error_code")
        execution_retryable = bool(result.get("execution_retryable"))
        repair_count = int(result.get("repair_count", 0) or 0)
        repair_reason = result.get("repair_reason")
        execution_completed = "sql_execution" in workflow_steps
        status = (
            "error"
            if execution_error or not execution_completed
            else "success" if execution_results else "empty"
        )
        visualization = result.get("visualization")
        viz_config = result.get("visualization_config")

        # Promote terminal validation failures (especially read-only policy
        # violations) to the common ToolResult error surface. Previously the
        # code lived only inside nested validation metadata, so the unified
        # executor could not classify the outcome as ``policy_rejection``.
        primary_error_code = execution_error_code or (
            validation_error_codes[0] if validation_error_codes else None
        )

        if execution_error:
            errors.append(str(execution_error))
        elif not execution_completed and validation_errors:
            errors.extend(str(error) for error in validation_errors)

        records_payload = {
            "answer": answer,
            "rows": execution_results,
            "sql": sql_statement,
            "visualization": visualization,
        }
        if viz_config:
            records_payload["visualization_config"] = viz_config

        logger.info(
            "[TOOL_EXEC] done | tool=text2sql_query | task=%s | errors=%s | rows=%s | sql=%s",
            question,
            len(errors),
            len(execution_results) if isinstance(execution_results, list) else 0,
            sql_statement,
        )
        trace_event(
            trace,
            "tool_execution",
            "done",
            success=execution_success(status),
            tool="text2sql_query",
            question=question,
            latency_ms=elapsed_ms(started_at),
            error="; ".join(errors) if errors else None,
            metadata={
                "sql_execution_success": execution_success(status),
                "execution_success": execution_success(status),
                "status": status,
                "sql": sql_statement,
                "row_count": len(execution_results) if isinstance(execution_results, list) else 0,
                "validation_error_codes": validation_error_codes,
                "row_limit_applied": row_limit_applied,
                "effective_limit": effective_limit,
                "sql_timeout_ms": effective_timeout_ms,
                "execution_error_code": execution_error_code,
                "repair_count": repair_count,
                "max_repairs": max_repairs,
            },
        )
        return {
            "cyphers": [
                Text2SQLToolOutputState(
                    **{
                        "task": question,
                        "query": sql_statement,
                        "errors": errors,
                        "records": records_payload,
                        "steps": ["execute_text2sql_query"],
                    }
                )
            ],
            "tool_results": [
                ToolResult(
                    tool_name="text2sql_query",
                    tool_kind="sql",
                    status=status,
                    task=question,
                    statement=(
                        Statement(language="sql", text=sql_statement)
                        if sql_statement
                        else None
                    ),
                    data=execution_results,
                    answer=answer,
                    evidence=[
                        Evidence(
                            kind="sql_row",
                            content=row,
                            source="mysql",
                            source_id=f"sql_row:{index + 1}",
                            rank=index + 1,
                            citation=f"SQL row {index + 1}",
                            metadata={"row_index": index},
                        )
                        for index, row in enumerate(execution_results)
                    ] if execution_completed and not execution_error else [],
                    metadata={
                        "steps": workflow_steps,
                        "validation": {
                            "error_codes": validation_error_codes,
                            "warnings": validation_warnings,
                            "referenced_tables": referenced_tables,
                            "referenced_columns": referenced_columns,
                        },
                        "execution_policy": {
                            "row_limit_applied": row_limit_applied,
                            "requested_limit": requested_limit,
                            "effective_limit": effective_limit,
                            "sql_timeout_ms": effective_timeout_ms,
                            "timeout_supported": timeout_supported,
                        },
                        "repair": {
                            "repair_count": repair_count,
                            "max_repairs": max_repairs,
                            "reason": repair_reason,
                        },
                        "error": errors[-1] if errors else None,
                        "error_code": primary_error_code,
                        "execution_error_code": execution_error_code,
                        "execution_retryable": execution_retryable,
                    },
                )
            ],
            "steps": ["execute_text2sql_query"],
        }

    return text2sql_query


class SQLAgent:
    """Primary SQL tool boundary returning the shared ``ToolResult`` contract.

    The current implementation delegates to the compatibility LangGraph node so
    SQL behavior remains unchanged during Phase 1.  Phase 2 can call this class
    directly without depending on the historical KG/Text2Cypher package.
    """

    tool_name = "sql_agent"
    tool_kind = "sql"

    def __init__(self, _legacy_graph: object | None = None) -> None:
        self._node = create_sql_agent_node(_legacy_graph)

    async def execute(
        self,
        task: str,
        context: Optional[Dict[str, Any]] = None,
        *,
        trace: Any = None,
    ) -> ToolResult:
        state: Dict[str, Any] = {
            "task": task,
            "query_parameters": context or {},
        }
        if trace is not None:
            state["eval_trace"] = trace

        result = await self._node(state)
        tool_results = result.get("tool_results") or []
        if not tool_results:
            return ToolResult(
                tool_name="text2sql_query",
                tool_kind="sql",
                status="error",
                task=task,
                answer="抱歉，SQL 工具未返回执行结果。",
                metadata={"error": "SQLAgent compatibility node returned no ToolResult"},
            )
        return tool_results[0]


# Historical public name kept for callers that have not migrated yet.
create_text2sql_tool_node = create_sql_agent_node


__all__ = [
    "SQLAgent",
    "Text2SQLToolOutputState",
    "create_sql_agent_node",
    "create_text2sql_tool_node",
]
