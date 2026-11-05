"""Controlled one-shot SQL repair node."""

from __future__ import annotations

from typing import Any, Callable, Coroutine, Dict

from langchain_core.language_models import BaseChatModel
from langchain_core.output_parsers import StrOutputParser

from multisource_data_agent.infrastructure.core.logger import get_logger

from ..sql_generation.node import _clean_sql_statement
from ..sql_generation.prompts import format_schema_as_text
from .prompts import create_sql_repair_prompt

logger = get_logger(service="text2sql.sql_repair")


def _build_repair_reason(state: Dict[str, Any]) -> str:
    """Build a concise repair reason from validation or execution failure state."""
    validation_codes = [str(code) for code in state.get("validation_error_codes") or []]
    validation_errors = [str(error) for error in state.get("validation_errors") or []]
    execution_code = state.get("execution_error_code")
    execution_error = state.get("execution_error")

    parts = []
    if validation_codes:
        parts.append("validation_codes=" + ",".join(validation_codes))
    if validation_errors:
        parts.append("validation_errors=" + "；".join(validation_errors[-3:]))
    if execution_code:
        parts.append(f"execution_code={execution_code}")
    if execution_error:
        parts.append(f"execution_error={execution_error}")
    return " | ".join(parts) or "SQL 生成结果未通过校验"


def create_sql_repair_node(
    llm: BaseChatModel,
    db_type: str = "MySQL",
) -> Callable[[Dict[str, Any]], Coroutine[Any, Any, Dict[str, Any]]]:
    """Build the single bounded LLM repair node.

    The workflow controller guarantees at most one visit by default. The node
    also defends itself by refusing to exceed ``max_repairs``.
    """
    prompt = create_sql_repair_prompt()
    repair_chain = prompt | llm.with_config(temperature=0.0) | StrOutputParser()

    async def repair_sql(state: Dict[str, Any]) -> Dict[str, Any]:
        current_count = int(state.get("repair_count", 0) or 0)
        max_repairs = min(1, max(0, int(state.get("max_repairs", 1) or 0)))
        failed_sql = (state.get("sql_statement") or "").strip()

        if current_count >= max_repairs:
            logger.warning("SQL repair budget exhausted | repair_count={}", current_count)
            return {
                "repair_count": current_count,
                "repair_reason": "REPAIR_BUDGET_EXHAUSTED",
                "steps": ["sql_repair_skipped_budget"],
            }

        schema_context = state.get("schema_context") or {}
        reason = _build_repair_reason(state)
        inputs = {
            "db_type": state.get("db_type") or db_type,
            "schema": format_schema_as_text(schema_context),
            "question": state.get("question") or "",
            "failed_sql": failed_sql,
            "failure_reason": reason,
        }

        logger.info(
            "-----开始受控修复 SQL | repair_attempt={} | reason={}-----",
            current_count + 1,
            reason,
        )

        try:
            repaired_raw = await repair_chain.ainvoke(inputs)
            repaired_sql = _clean_sql_statement(repaired_raw)
        except Exception as exc:  # pragma: no cover - defensive logging
            logger.exception("SQL 修复调用失败: {}", exc)
            return {
                "repair_count": current_count + 1,
                "repair_reason": reason,
                "validation_retryable": False,
                "execution_retryable": False,
                "execution_error": f"SQL 修复失败: {exc}",
                "execution_error_code": "SQL_REPAIR_FAILED",
                "steps": ["sql_repair_failed"],
            }

        if not repaired_sql or repaired_sql.strip() == failed_sql.strip():
            logger.warning("SQL repair produced no effective change")
            return {
                "repair_count": current_count + 1,
                "repair_reason": "REPAIR_NO_CHANGE",
                "validation_retryable": False,
                "execution_retryable": False,
                "steps": ["sql_repair_no_change"],
            }

        return {
            "sql_statement": repaired_sql,
            "repair_count": current_count + 1,
            "repair_reason": reason,
            # Clear the previous attempt's transient failure state. The repaired
            # candidate must pass the full validator again before execution.
            "validation_errors": [],
            "validation_error_codes": [],
            "validation_warnings": [],
            "validation_retryable": False,
            "execution_error": None,
            "execution_error_code": None,
            "execution_retryable": False,
            "execution_results": None,
            "steps": ["sql_repair"],
        }

    return repair_sql


__all__ = ["_build_repair_reason", "create_sql_repair_node"]
