"""Deterministic SQL validation helpers for the canonical SQL Agent.

Phase 3 deliberately keeps this layer small: SQLGlot provides MySQL-aware AST
parsing and column qualification, while Multi-Source Data Agent contributes the allowed-schema
and read-only policy.  LLM calls are not used in the validation happy path.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Tuple

import sqlglot
from sqlglot import exp
from sqlglot.errors import OptimizeError, ParseError, SchemaError
from sqlglot.optimizer.qualify import qualify


@dataclass
class SQLSemanticValidation:
    """Compact validation result passed through the Text2SQL workflow state."""

    is_valid: bool
    errors: List[str] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)
    error_codes: List[str] = field(default_factory=list)
    retryable: bool = False
    referenced_tables: List[str] = field(default_factory=list)
    referenced_columns: Dict[str, List[str]] = field(default_factory=dict)


def _dialect_name(db_type: str) -> str:
    normalized = (db_type or "mysql").strip().lower()
    mapping = {
        "mysql": "mysql",
        "mariadb": "mysql",
        "postgresql": "postgres",
        "postgres": "postgres",
        "pg": "postgres",
        "sqlite": "sqlite",
    }
    return mapping.get(normalized, normalized)


def _schema_mapping(schema_context: Dict[str, Any]) -> Dict[str, Dict[str, str]]:
    """Convert the existing INFORMATION_SCHEMA payload to SQLGlot's mapping."""
    mapping: Dict[str, Dict[str, str]] = {}
    for table in (schema_context or {}).get("tables", []) or []:
        table_name = str(table.get("table_name") or "").strip()
        if not table_name:
            continue

        columns: Dict[str, str] = {}
        for column in table.get("columns", []) or []:
            column_name = str(column.get("column_name") or "").strip()
            if not column_name:
                continue
            data_type = str(column.get("data_type") or "UNKNOWN").strip() or "UNKNOWN"
            columns[column_name] = data_type

        mapping[table_name] = columns
    return mapping


def _parse_single_statement(sql: str, dialect: str) -> Tuple[exp.Expression | None, List[str]]:
    try:
        statements = [statement for statement in sqlglot.parse(sql, read=dialect) if statement]
    except ParseError as exc:
        return None, [f"SQL 解析失败: {exc}"]

    if len(statements) != 1:
        return None, ["只允许单条 SQL 查询"]
    return statements[0], []


def validate_sql_syntax(sql: str, db_type: str = "MySQL") -> Tuple[bool, List[str]]:
    """Validate that SQLGlot can parse exactly one statement for the dialect."""
    if not sql or not sql.strip():
        return False, ["SQL 语句为空"]

    _, errors = _parse_single_statement(sql, _dialect_name(db_type))
    return not errors, errors


def validate_sql_security(sql: str, db_type: str = "MySQL") -> Tuple[bool, List[str]]:
    """Require one read-only query using both the existing lexer and the AST."""
    if not sql or not sql.strip():
        return False, ["SQL 语句为空"]

    # Keep the battle-tested Phase 1 lexer as the first safety boundary.  The
    # local import avoids making sql_execution depend on sql_validation.
    from multisource_data_agent.application.agents.text2sql.components.sql_execution.node import (
        _is_read_only_query,
    )

    if not _is_read_only_query(sql):
        return False, ["仅允许单条只读 SELECT/CTE 查询"]

    expression, parse_errors = _parse_single_statement(sql, _dialect_name(db_type))
    if parse_errors:
        return False, parse_errors

    # SQL Agent business queries are SELECT-style queries. SHOW/EXPLAIN remain
    # available to internal infrastructure, but are not accepted from the LLM.
    if not isinstance(expression, exp.Query):
        return False, ["SQL Agent 仅允许 SELECT/CTE 查询"]

    return True, []


def _classify_optimize_error(message: str) -> str:
    lowered = message.lower()
    if "ambiguous" in lowered:
        return "AMBIGUOUS_COLUMN"
    if "unknown column" in lowered or "could not be resolved" in lowered:
        return "UNKNOWN_COLUMN"
    return "SCHEMA_RESOLUTION_ERROR"


def _collect_physical_tables(expression: exp.Expression) -> List[str]:
    cte_names = {
        str(cte.alias_or_name or "").lower()
        for cte in expression.find_all(exp.CTE)
        if cte.alias_or_name
    }

    tables: List[str] = []
    seen = set()
    for table in expression.find_all(exp.Table):
        name = str(table.name or "").strip()
        if not name or name.lower() in cte_names:
            continue
        lowered = name.lower()
        if lowered in seen:
            continue
        seen.add(lowered)
        tables.append(name)
    return tables


def _collect_columns(expression: exp.Expression) -> Dict[str, List[str]]:
    columns: Dict[str, List[str]] = {}
    seen: Dict[str, set[str]] = {}

    for column in expression.find_all(exp.Column):
        column_name = str(column.name or "").strip()
        if not column_name:
            continue
        table_name = str(column.table or "").strip()
        key = table_name or "<unqualified>"
        bucket = seen.setdefault(key, set())
        lowered = column_name.lower()
        if lowered in bucket:
            continue
        bucket.add(lowered)
        columns.setdefault(key, []).append(column_name)

    return columns


def _ambiguous_unqualified_columns(
    expression: exp.Expression,
    schema_mapping: Dict[str, Dict[str, str]],
) -> List[str]:
    """Find obvious ambiguous unqualified columns inside each SELECT scope.

    SQLGlot may report an unqualified column shared by two joined tables as
    merely "could not be resolved" rather than explicitly "ambiguous".  For
    the practical Phase 3 validator we handle the common case deterministically
    before qualification: if the same unqualified name exists in two or more
    physical tables visible in the same SELECT scope, the query must qualify it.

    This intentionally stays small. Derived-table output inference is left to
    SQLGlot/database execution instead of recreating a SQL compiler here.
    """
    schema_lookup = {
        table_name.lower(): {column_name.lower() for column_name in columns}
        for table_name, columns in schema_mapping.items()
    }
    ambiguous: List[str] = []
    seen: set[str] = set()

    for select in expression.find_all(exp.Select):
        scope_tables: List[str] = []
        for table in select.find_all(exp.Table):
            # Ignore tables owned by nested SELECT scopes.
            if table.find_ancestor(exp.Select) is not select:
                continue
            table_name = str(table.name or "").strip()
            if table_name.lower() in schema_lookup:
                scope_tables.append(table_name.lower())

        if len(scope_tables) < 2:
            continue

        for column in select.find_all(exp.Column):
            if column.find_ancestor(exp.Select) is not select:
                continue
            if column.table:
                continue
            column_name = str(column.name or "").strip()
            if not column_name or column_name == "*":
                continue

            lowered = column_name.lower()
            matches = sum(
                1
                for table_name in scope_tables
                if lowered in schema_lookup.get(table_name, set())
            )
            if matches > 1 and lowered not in seen:
                seen.add(lowered)
                ambiguous.append(column_name)

    return ambiguous


def validate_sql_semantics(
    sql: str,
    schema_context: Dict[str, Any],
    db_type: str = "MySQL",
) -> SQLSemanticValidation:
    """Validate read-only SQL against the schema already retrieved for the task.

    This intentionally does *not* try to reproduce a database compiler.  It
    covers the high-value deterministic checks that are easy to explain in an
    interview: parse one query, enforce read-only access, verify table names,
    and let SQLGlot qualify/resolve columns against INFORMATION_SCHEMA data.
    """
    if not sql or not sql.strip():
        return SQLSemanticValidation(
            is_valid=False,
            errors=["SQL 语句为空"],
            error_codes=["EMPTY_SQL"],
            retryable=True,
        )

    dialect = _dialect_name(db_type)
    expression, parse_errors = _parse_single_statement(sql, dialect)
    if parse_errors:
        code = "MULTI_STATEMENT" if parse_errors == ["只允许单条 SQL 查询"] else "SQL_PARSE_ERROR"
        return SQLSemanticValidation(
            is_valid=False,
            errors=parse_errors,
            error_codes=[code],
            retryable=code == "SQL_PARSE_ERROR",
        )

    security_ok, security_errors = validate_sql_security(sql, db_type)
    if not security_ok:
        return SQLSemanticValidation(
            is_valid=False,
            errors=security_errors,
            error_codes=["READ_ONLY_POLICY_VIOLATION"],
            retryable=False,
        )

    assert expression is not None  # guarded by _parse_single_statement
    schema_mapping = _schema_mapping(schema_context)
    if not schema_mapping:
        return SQLSemanticValidation(
            is_valid=False,
            errors=["缺少可用于 SQL 校验的数据库 Schema"],
            error_codes=["SCHEMA_UNAVAILABLE"],
            retryable=False,
        )

    referenced_tables = _collect_physical_tables(expression)
    table_lookup = {name.lower(): name for name in schema_mapping}
    unknown_tables = [name for name in referenced_tables if name.lower() not in table_lookup]
    if unknown_tables:
        return SQLSemanticValidation(
            is_valid=False,
            errors=[f"Schema 中不存在表: {', '.join(unknown_tables)}"],
            error_codes=["UNKNOWN_TABLE"],
            retryable=True,
            referenced_tables=referenced_tables,
        )

    ambiguous_columns = _ambiguous_unqualified_columns(expression, schema_mapping)
    if ambiguous_columns:
        return SQLSemanticValidation(
            is_valid=False,
            errors=[
                "存在未限定且可匹配多个表的字段: "
                + ", ".join(ambiguous_columns)
            ],
            error_codes=["AMBIGUOUS_COLUMN"],
            retryable=True,
            referenced_tables=referenced_tables,
        )

    try:
        # Qualify a clone only for validation.  The generated SQL is not
        # replaced by SQLGlot's rewritten SQL in this step.
        qualified = qualify(
            expression.copy(),
            dialect=dialect,
            schema=schema_mapping,
            expand_stars=False,
            validate_qualify_columns=True,
            quote_identifiers=False,
            identify=False,
            sql=sql,
        )
    except (OptimizeError, SchemaError) as exc:
        message = str(exc)
        return SQLSemanticValidation(
            is_valid=False,
            errors=[f"Schema 字段解析失败: {message}"],
            error_codes=[_classify_optimize_error(message)],
            retryable=True,
            referenced_tables=referenced_tables,
        )

    return SQLSemanticValidation(
        is_valid=True,
        referenced_tables=referenced_tables,
        referenced_columns=_collect_columns(qualified),
    )


__all__ = [
    "SQLSemanticValidation",
    "validate_sql_security",
    "validate_sql_semantics",
    "validate_sql_syntax",
]
