"""SQL validation node."""

from typing import Any, Callable, Coroutine, Dict

from multisource_data_agent.infrastructure.core.logger import get_logger

from .validators import validate_sql_semantics

logger = get_logger(service="text2sql.sql_validation")


def create_sql_validation_node(
    db_type: str = "MySQL",
) -> Callable[[Dict[str, Any]], Coroutine[Any, Any, Dict[str, Any]]]:
    """Build the deterministic schema-aware validation node."""

    async def validate_sql(state: Dict[str, Any]) -> Dict[str, Any]:
        logger.info("-----开始验证 SQL 语句-----")

        sql_statement = state.get("sql_statement", "")
        current_retry = int(state.get("retry_count", 0) or 0)
        max_retries = int(state.get("max_retries", 3) or 3)
        schema_context = state.get("schema_context") or {}
        effective_db_type = state.get("db_type") or db_type

        try:
            report = validate_sql_semantics(
                sql_statement,
                schema_context=schema_context,
                db_type=effective_db_type,
            )
        except Exception as exc:  # pragma: no cover - defensive logging
            logger.exception("SQL 验证过程出错: {}", exc)
            return {
                "is_valid": False,
                "validation_errors": [f"验证过程出错: {exc}"],
                "validation_error_codes": ["VALIDATION_INTERNAL_ERROR"],
                "validation_warnings": [],
                "validation_retryable": False,
                "retry_count": max_retries,
                "steps": ["sql_validation_error"],
            }

        if report.is_valid:
            logger.info(
                "SQL 验证通过 | tables={} | columns={}",
                report.referenced_tables,
                report.referenced_columns,
            )
            next_retry = current_retry
        else:
            logger.warning(
                "SQL 验证失败 | codes={} | retryable={} | errors={}",
                report.error_codes,
                report.retryable,
                report.errors,
            )
            # Security/policy failures are terminal even before Phase 3 Step 2's
            # controlled repair loop. They must never burn LLM retries.
            next_retry = current_retry + 1 if report.retryable else max_retries

        return {
            "is_valid": report.is_valid,
            "validation_errors": report.errors,
            "validation_error_codes": report.error_codes,
            "validation_warnings": report.warnings,
            "validation_retryable": report.retryable,
            "referenced_tables": report.referenced_tables,
            "referenced_columns": report.referenced_columns,
            "retry_count": next_retry,
            "steps": ["sql_validation" if report.is_valid else "sql_validation_failed"],
        }

    return validate_sql
