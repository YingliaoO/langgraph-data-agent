"""SQL execution node."""

from __future__ import annotations

import asyncio
from typing import Any, Callable, Coroutine, Dict, List, Optional, Tuple

import sqlglot
from sqlglot import exp
from sqlglot.errors import ParseError
from sqlalchemy import create_engine, text
from sqlalchemy.engine import Connection, Engine
from sqlalchemy.exc import DBAPIError

from multisource_data_agent.config import settings
from multisource_data_agent.infrastructure.core.logger import get_logger

logger = get_logger(service="text2sql.sql_execution")


def create_sql_execution_node(
    connection_string: Optional[str] = None,
) -> Callable[[Dict[str, Any]], Coroutine[Any, Any, Dict[str, Any]]]:
    """Build a LangGraph node that executes bounded read-only SQL."""

    async def execute_sql(state: Dict[str, Any]) -> Dict[str, Any]:
        logger.info("-----开始执行 SQL 查询-----")

        sql_statement = (state.get("sql_statement") or "").strip()
        connection_id = state.get("connection_id")
        max_rows = max(1, int(state.get("max_rows") or settings.SQL_AGENT_MAX_ROWS))
        timeout_ms = max(0, int(state.get("sql_timeout_ms") or settings.SQL_AGENT_TIMEOUT_MS))
        db_type = state.get("db_type") or "MySQL"

        if not sql_statement:
            logger.warning("SQL 语句为空，跳过执行")
            return {
                "execution_results": None,
                "execution_error": "SQL 语句为空",
                "execution_error_code": "EMPTY_SQL",
                "execution_retryable": False,
                "steps": ["sql_execution_skipped"],
            }

        if not _is_read_only_query(sql_statement):
            logger.warning("检测到非只读查询，已阻止执行")
            return {
                "execution_results": None,
                "execution_error": "仅支持只读 SELECT 查询",
                "execution_error_code": "READ_ONLY_POLICY_VIOLATION",
                "execution_retryable": False,
                "steps": ["sql_execution_blocked"],
            }

        if not state.get("is_valid"):
            logger.warning("SQL 验证未通过，跳过执行")
            return {
                "execution_results": None,
                "execution_error": "SQL 验证未通过",
                "execution_error_code": "VALIDATION_FAILED",
                "execution_retryable": False,
                "steps": ["sql_execution_skipped_invalid"],
            }

        execution_sql, row_limit_applied, requested_limit = _apply_result_limit(
            sql_statement,
            max_rows=max_rows,
            db_type=db_type,
        )
        effective_limit = (
            requested_limit
            if requested_limit is not None and requested_limit <= max_rows
            else max_rows
        )

        conn_str = (
            connection_string
            or state.get("connection_string")
            or _get_connection_string(connection_id)
        )
        if not conn_str:
            logger.error("无法获取数据库连接信息 connection_id={}", connection_id)
            return {
                "execution_results": None,
                "execution_error": "无法获取数据库连接信息",
                "execution_error_code": "DB_CONNECTION_UNAVAILABLE",
                "execution_retryable": False,
                "steps": ["sql_execution_failed"],
            }

        try:
            results, timeout_supported = await _execute_sql_query(
                conn_str,
                execution_sql,
                max_rows=max_rows,
                timeout_ms=timeout_ms,
            )
            logger.info(
                "SQL 执行成功，返回 {} 行结果 | row_limit_applied={} | timeout_ms={}",
                len(results),
                row_limit_applied,
                timeout_ms,
            )
            return {
                # Expose the exact SQL sent to the database after deterministic
                # row-limit enforcement so Statement.text remains truthful.
                "sql_statement": execution_sql,
                "execution_results": results,
                "execution_error": None,
                "execution_error_code": None,
                "execution_retryable": False,
                "row_limit_applied": row_limit_applied,
                "requested_limit": requested_limit,
                "effective_limit": effective_limit,
                "sql_timeout_ms": timeout_ms,
                "timeout_supported": timeout_supported,
                "steps": ["sql_execution"],
            }
        except Exception as exc:  # pragma: no cover - defensive logging
            error_code, retryable = _classify_execution_error(exc)
            logger.exception(
                "SQL 执行失败 | code={} | retryable={}: {}",
                error_code,
                retryable,
                exc,
            )
            return {
                "sql_statement": execution_sql,
                "execution_results": None,
                "execution_error": str(exc),
                "execution_error_code": error_code,
                "execution_retryable": retryable,
                "row_limit_applied": row_limit_applied,
                "requested_limit": requested_limit,
                "effective_limit": effective_limit,
                "sql_timeout_ms": timeout_ms,
                "steps": ["sql_execution_failed"],
            }

    return execute_sql



def _mysql_error_number(exc: Exception) -> Optional[int]:
    """Best-effort extraction of a MySQL/PyMySQL numeric error code."""
    candidate: Any = exc
    if isinstance(exc, DBAPIError) and exc.orig is not None:
        candidate = exc.orig
    args = getattr(candidate, "args", ()) or ()
    if not args:
        return None
    try:
        return int(args[0])
    except (TypeError, ValueError):
        return None


def _classify_execution_error(exc: Exception) -> Tuple[str, bool]:
    """Classify only the small set of SQL-generation failures we repair.

    Security, timeout, permission, connection and unknown failures are terminal.
    This is intentionally not a full MySQL error taxonomy.
    """
    mysql_code = _mysql_error_number(exc)
    repairable = {
        1064: "SQL_PARSE_ERROR",
        1146: "UNKNOWN_TABLE",
        1054: "UNKNOWN_COLUMN",
        1052: "AMBIGUOUS_COLUMN",
    }
    if mysql_code in repairable:
        return repairable[mysql_code], True
    if mysql_code == 3024:
        return "SQL_TIMEOUT", False
    if mysql_code in {1044, 1045}:
        return "DB_PERMISSION_ERROR", False
    return "SQL_EXECUTION_ERROR", False

def _map_db_type_to_driver(db_type: str) -> str:
    """Legacy compatibility helper kept for historical Text2SQL adapters.

    The canonical SQL Agent now receives a full connection URL directly, but
    older wrappers still import this helper. Keeping this tiny pure mapping
    avoids breaking those compatibility paths while Phase 3 hardens execution.
    """
    mapping = {
        "mysql": "mysql+pymysql",
        "mariadb": "mysql+pymysql",
        "postgresql": "postgresql+psycopg2",
        "postgres": "postgresql+psycopg2",
        "pg": "postgresql+psycopg2",
        "sqlite": "sqlite",
    }
    normalized = (db_type or "").lower()
    return mapping.get(normalized, db_type)


def _get_connection_string(connection_id: Optional[int]) -> Optional[str]:
    """Return the configured business database used by the SQL Agent."""
    logger.debug("使用已配置的业务数据库连接")
    return settings.DATABASE_URL


def _dialect_name(db_type: str) -> str:
    normalized = (db_type or "mysql").strip().lower()
    return {
        "mysql": "mysql",
        "mariadb": "mysql",
        "postgresql": "postgres",
        "postgres": "postgres",
        "pg": "postgres",
        "sqlite": "sqlite",
    }.get(normalized, normalized)


def _literal_limit(expression: exp.Expression) -> Optional[int]:
    limit = expression.args.get("limit")
    if limit is None:
        return None
    value = getattr(limit, "expression", None)
    if not isinstance(value, exp.Literal) or value.is_string:
        return None
    try:
        return int(value.this)
    except (TypeError, ValueError):
        return None


def _apply_result_limit(
    sql: str,
    max_rows: int,
    db_type: str = "MySQL",
) -> Tuple[str, bool, Optional[int]]:
    """Add/clamp the outer query LIMIT using the AST, never string concatenation."""
    max_rows = max(1, int(max_rows))
    try:
        statements = [
            statement
            for statement in sqlglot.parse(sql, read=_dialect_name(db_type))
            if statement
        ]
    except ParseError:
        # Validation should have caught this already; keep execution defensive.
        return sql, False, None

    if len(statements) != 1 or not isinstance(statements[0], exp.Query):
        return sql, False, None

    expression = statements[0]
    requested_limit = _literal_limit(expression)
    if requested_limit is not None and requested_limit <= max_rows:
        return sql, False, requested_limit

    limited = expression.copy().limit(max_rows)
    return limited.sql(dialect=_dialect_name(db_type)), True, requested_limit


def _configure_statement_timeout(connection: Connection, timeout_ms: int) -> bool:
    """Apply a MySQL/MariaDB SELECT timeout for the current connection."""
    timeout_ms = int(timeout_ms)
    if timeout_ms <= 0:
        return False

    dialect_name = str(connection.dialect.name or "").lower()
    if dialect_name not in {"mysql", "mariadb"}:
        return False

    connection.exec_driver_sql(f"SET SESSION MAX_EXECUTION_TIME = {timeout_ms}")
    return True


async def _execute_sql_query(
    connection_string: str,
    sql: str,
    max_rows: int = 200,
    timeout_ms: int = 5000,
) -> Tuple[List[Dict[str, Any]], bool]:
    return await asyncio.to_thread(
        _run_query_sync,
        connection_string,
        sql,
        max_rows,
        timeout_ms,
    )


def _run_query_sync(
    connection_string: str,
    sql: str,
    max_rows: int,
    timeout_ms: int = 5000,
) -> Tuple[List[Dict[str, Any]], bool]:
    engine: Engine = create_engine(connection_string, future=True)
    try:
        with engine.connect() as connection:
            timeout_supported = _configure_statement_timeout(connection, timeout_ms)
            result = connection.execution_options(stream_results=True).execute(text(sql))
            columns = list(result.keys())
            rows = result.fetchmany(max_rows)
            payload = [
                {column: row[idx] for idx, column in enumerate(columns)}
                for row in rows
            ]
            return payload, timeout_supported
    finally:
        engine.dispose()


_READ_ONLY_KEYWORDS = {"SELECT", "SHOW", "EXPLAIN"}
_WRITE_KEYWORDS = {
    "INSERT",
    "UPDATE",
    "DELETE",
    "DROP",
    "ALTER",
    "TRUNCATE",
    "CREATE",
    "REPLACE",
    "MERGE",
    "GRANT",
    "REVOKE",
    "EXEC",
    "EXECUTE",
    "CALL",
}
_FILE_EXPORT_KEYWORDS = {"OUTFILE", "DUMPFILE"}


def _tokenize_sql(sql: str) -> List[tuple[str, int]]:
    """Tokenize SQL enough to identify statement boundaries and main verbs.

    This deliberately avoids treating words inside strings, quoted identifiers,
    or comments as SQL keywords. The depth value tracks parentheses so a CTE
    body can be skipped without mistaking its SELECT for the outer statement.
    """
    tokens: List[tuple[str, int]] = []
    depth = 0
    index = 0
    length = len(sql)

    while index < length:
        char = sql[index]

        if char.isspace():
            index += 1
            continue

        if sql.startswith("--", index) or char == "#":
            newline = sql.find("\n", index)
            index = length if newline < 0 else newline + 1
            continue

        if sql.startswith("/*", index):
            end_comment = sql.find("*/", index + 2)
            if end_comment < 0:
                return []
            index = end_comment + 2
            continue

        if char in {"'", '"', "`"}:
            quote = char
            index += 1
            while index < length:
                if sql[index] == quote:
                    if index + 1 < length and sql[index + 1] == quote:
                        index += 2
                        continue
                    index += 1
                    break
                if sql[index] == "\\" and index + 1 < length:
                    index += 2
                else:
                    index += 1
            continue

        if char == "(":
            tokens.append((char, depth))
            depth += 1
            index += 1
            continue

        if char == ")":
            if depth == 0:
                return []
            depth -= 1
            tokens.append((char, depth))
            index += 1
            continue

        if char in {",", ";"}:
            tokens.append((char, depth))
            index += 1
            continue

        if char.isalpha() or char == "_":
            start = index
            index += 1
            while index < length and (sql[index].isalnum() or sql[index] in {"_", "$"}):
                index += 1
            tokens.append((sql[start:index].upper(), depth))
            continue

        index += 1

    return tokens if depth == 0 else []


def _with_main_keyword(tokens: List[tuple[str, int]]) -> Optional[str]:
    """Return the statement keyword following one or more CTE definitions."""
    index = 1  # WITH
    if index < len(tokens) and tokens[index] == ("RECURSIVE", 0):
        index += 1

    while index < len(tokens):
        as_index = index
        while as_index < len(tokens):
            token, token_depth = tokens[as_index]
            if token_depth == 0 and token == "AS":
                break
            as_index += 1
        if as_index >= len(tokens) or as_index + 1 >= len(tokens):
            return None

        opening, opening_depth = tokens[as_index + 1]
        if opening != "(" or opening_depth != 0:
            return None

        closing_index = as_index + 2
        while closing_index < len(tokens):
            token, token_depth = tokens[closing_index]
            if token == ")" and token_depth == opening_depth:
                break
            closing_index += 1
        if closing_index >= len(tokens):
            return None

        index = closing_index + 1
        if index < len(tokens) and tokens[index] == (",", 0):
            index += 1
            continue
        break

    while index < len(tokens) and tokens[index][1] != 0:
        index += 1
    if index >= len(tokens):
        return None
    return tokens[index][0]


def _has_file_export_clause(tokens: List[tuple[str, int]]) -> bool:
    """Return whether the outer statement writes a result to a file."""
    for index, (token, depth) in enumerate(tokens[:-1]):
        next_token, next_depth = tokens[index + 1]
        if (
            depth == 0
            and token == "INTO"
            and next_depth == 0
            and next_token in _FILE_EXPORT_KEYWORDS
        ):
            return True
    return False


def _is_read_only_query(sql: str) -> bool:
    """Return whether SQL has a single read-only outer statement."""
    tokens = _tokenize_sql(sql.strip())
    if not tokens:
        return False

    semicolons = [index for index, (token, _) in enumerate(tokens) if token == ";"]
    if len(semicolons) > 1:
        return False
    if semicolons and semicolons[0] != len(tokens) - 1:
        return False
    if _has_file_export_clause(tokens):
        return False

    first_keyword = tokens[0][0]
    if first_keyword == "WITH":
        statement_keyword = _with_main_keyword(tokens)
    else:
        statement_keyword = first_keyword

    if statement_keyword is None or statement_keyword in _WRITE_KEYWORDS:
        return False
    if statement_keyword not in _READ_ONLY_KEYWORDS and statement_keyword != "WITH":
        return False

    if statement_keyword == "EXPLAIN":
        next_keyword = next(
            (token for token, depth in tokens[1:] if depth == 0 and token not in {";"}),
            None,
        )
        if next_keyword not in {"SELECT", "WITH"}:
            return False

    return True
