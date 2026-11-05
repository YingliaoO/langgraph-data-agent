"""Phase 3 Step 1 tests for practical SQL semantic safety.

No LLM or live database is required. SQLGlot validates generated SQL against a
small INFORMATION_SCHEMA-shaped fixture, and execution policy helpers are
checked independently.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

from multisource_data_agent.application.agents.text2sql.components.sql_execution.node import (
    _apply_result_limit,
    _configure_statement_timeout,
    create_sql_execution_node,
)
from multisource_data_agent.application.agents.text2sql.components.schema_retrieval.node import (
    create_schema_retrieval_node,
)
from multisource_data_agent.application.agents.text2sql.components.sql_validation.node import (
    create_sql_validation_node,
)
from multisource_data_agent.application.agents.text2sql.components.sql_validation.validators import (
    validate_sql_semantics,
)


SCHEMA = {
    "tables": [
        {
            "table_name": "recipes",
            "columns": [
                {"column_name": "id", "data_type": "bigint"},
                {"column_name": "name", "data_type": "varchar"},
                {"column_name": "cuisine", "data_type": "varchar"},
            ],
        },
        {
            "table_name": "ingredients",
            "columns": [
                {"column_name": "id", "data_type": "bigint"},
                {"column_name": "name", "data_type": "varchar"},
            ],
        },
        {
            "table_name": "recipe_ingredients",
            "columns": [
                {"column_name": "recipe_id", "data_type": "bigint"},
                {"column_name": "ingredient_id", "data_type": "bigint"},
            ],
        },
    ],
    "relationships": [],
}


def test_valid_select_is_schema_aware() -> None:
    report = validate_sql_semantics(
        "SELECT cuisine, COUNT(*) AS cnt FROM recipes GROUP BY cuisine",
        SCHEMA,
    )

    assert report.is_valid is True
    assert report.error_codes == []
    assert report.referenced_tables == ["recipes"]
    assert "recipes" in report.referenced_columns
    assert "cuisine" in report.referenced_columns["recipes"]


def test_valid_cte_and_subquery_do_not_look_like_unknown_tables() -> None:
    cte = validate_sql_semantics(
        "WITH r AS (SELECT id, name FROM recipes) SELECT name FROM r",
        SCHEMA,
    )
    subquery = validate_sql_semantics(
        "SELECT name FROM recipes WHERE id IN "
        "(SELECT recipe_id FROM recipe_ingredients)",
        SCHEMA,
    )

    assert cte.is_valid is True
    assert cte.referenced_tables == ["recipes"]
    assert subquery.is_valid is True
    assert set(subquery.referenced_tables) == {"recipes", "recipe_ingredients"}


def test_unknown_table_is_retryable_generation_error() -> None:
    report = validate_sql_semantics("SELECT id FROM recipez", SCHEMA)

    assert report.is_valid is False
    assert report.error_codes == ["UNKNOWN_TABLE"]
    assert report.retryable is True


def test_unknown_column_is_retryable_generation_error() -> None:
    report = validate_sql_semantics("SELECT missing_column FROM recipes", SCHEMA)

    assert report.is_valid is False
    assert report.error_codes == ["UNKNOWN_COLUMN"]
    assert report.retryable is True


def test_ambiguous_column_is_retryable_generation_error() -> None:
    report = validate_sql_semantics(
        "SELECT id FROM recipes r JOIN ingredients i ON r.id = i.id",
        SCHEMA,
    )

    assert report.is_valid is False
    assert report.error_codes == ["AMBIGUOUS_COLUMN"]
    assert report.retryable is True


def test_write_query_is_terminal_policy_failure() -> None:
    report = validate_sql_semantics("DELETE FROM recipes", SCHEMA)

    assert report.is_valid is False
    assert report.error_codes == ["READ_ONLY_POLICY_VIOLATION"]
    assert report.retryable is False


def test_multiple_statements_are_terminal_policy_failure() -> None:
    report = validate_sql_semantics(
        "SELECT id FROM recipes; SELECT name FROM recipes",
        SCHEMA,
    )

    assert report.is_valid is False
    assert report.error_codes == ["MULTI_STATEMENT"]
    assert report.retryable is False


def test_keywords_inside_string_literals_do_not_trigger_write_policy() -> None:
    report = validate_sql_semantics(
        "SELECT 'DROP TABLE recipes' AS note FROM recipes",
        SCHEMA,
    )

    assert report.is_valid is True


def test_validation_node_stops_policy_failures_without_llm_retry() -> None:
    node = create_sql_validation_node("MySQL")
    output = asyncio.run(
        node(
            {
                "sql_statement": "DELETE FROM recipes",
                "schema_context": SCHEMA,
                "retry_count": 0,
                "max_retries": 3,
            }
        )
    )

    assert output["is_valid"] is False
    assert output["validation_retryable"] is False
    assert output["retry_count"] == 3


def test_validation_node_keeps_schema_error_retryable_for_step2() -> None:
    node = create_sql_validation_node("MySQL")
    output = asyncio.run(
        node(
            {
                "sql_statement": "SELECT missing_column FROM recipes",
                "schema_context": SCHEMA,
                "retry_count": 0,
                "max_retries": 3,
            }
        )
    )

    assert output["is_valid"] is False
    assert output["validation_retryable"] is True
    assert output["retry_count"] == 1
    assert output["validation_error_codes"] == ["UNKNOWN_COLUMN"]


def test_result_limit_is_added_when_missing() -> None:
    sql, applied, requested = _apply_result_limit(
        "SELECT id, name FROM recipes ORDER BY id",
        max_rows=200,
    )

    assert applied is True
    assert requested is None
    assert "LIMIT 200" in sql.upper()


def test_existing_small_limit_is_preserved() -> None:
    original = "SELECT id FROM recipes LIMIT 10"
    sql, applied, requested = _apply_result_limit(original, max_rows=200)

    assert sql == original
    assert applied is False
    assert requested == 10


def test_existing_large_limit_is_clamped() -> None:
    sql, applied, requested = _apply_result_limit(
        "SELECT id FROM recipes LIMIT 10000",
        max_rows=200,
    )

    assert applied is True
    assert requested == 10000
    assert "LIMIT 200" in sql.upper()
    assert "10000" not in sql


class _FakeConnection:
    def __init__(self, dialect_name: str) -> None:
        self.dialect = SimpleNamespace(name=dialect_name)
        self.statements: list[str] = []

    def exec_driver_sql(self, sql: str) -> None:
        self.statements.append(sql)


def test_mysql_timeout_is_applied_as_session_policy() -> None:
    connection = _FakeConnection("mysql")

    supported = _configure_statement_timeout(connection, 5000)  # type: ignore[arg-type]

    assert supported is True
    assert connection.statements == ["SET SESSION MAX_EXECUTION_TIME = 5000"]


def test_non_mysql_timeout_is_not_faked() -> None:
    connection = _FakeConnection("sqlite")

    supported = _configure_statement_timeout(connection, 5000)  # type: ignore[arg-type]

    assert supported is False
    assert connection.statements == []


def test_schema_retrieval_and_execution_share_explicit_connection_string(monkeypatch) -> None:
    from multisource_data_agent.application.agents.text2sql.components.schema_retrieval import node as schema_module
    from multisource_data_agent.application.agents.text2sql.components.sql_execution import node as execution_module

    captured = {}
    database_url = "mysql+pymysql://user:pass@db-b:3306/retail"

    async def fake_retrieve(_question, connection_string=None):
        captured["schema"] = connection_string
        return {"tables": [], "relationships": []}

    async def fake_execute(connection_string, _sql, max_rows=200, timeout_ms=5000):
        captured["execution"] = connection_string
        return [], False

    monkeypatch.setattr(schema_module, "_retrieve_schema_from_mysql", fake_retrieve)
    monkeypatch.setattr(execution_module, "_execute_sql_query", fake_execute)

    schema_node = create_schema_retrieval_node(connection_string=database_url)
    execution_node = create_sql_execution_node(connection_string=database_url)
    asyncio.run(schema_node({"question": "查询订单"}))
    asyncio.run(
        execution_node(
            {
                "sql_statement": "SELECT 1",
                "is_valid": True,
                "db_type": "MySQL",
                "max_rows": 10,
                "sql_timeout_ms": 5000,
            }
        )
    )

    assert captured == {"schema": database_url, "execution": database_url}
