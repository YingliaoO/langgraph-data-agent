"""Phase 3 Step 2 tests for bounded SQL repair orchestration.

These tests stay deterministic: they verify routing, MySQL error classification,
and repair-budget invariants without calling a live LLM or database.
"""

from __future__ import annotations

from pathlib import Path
import re

from multisource_data_agent.application.agents.text2sql.components.sql_execution.node import (
    _classify_execution_error,
)
from multisource_data_agent.application.agents.text2sql.components.sql_repair.node import (
    _build_repair_reason,
)
from multisource_data_agent.application.agents.text2sql.workflow import (
    _route_after_execution,
    _route_after_validation,
)


def test_validated_sql_executes_without_repair() -> None:
    assert _route_after_validation({"is_valid": True}) == "execute"


def test_retryable_validation_error_gets_one_repair() -> None:
    state = {
        "is_valid": False,
        "validation_retryable": True,
        "validation_error_codes": ["UNKNOWN_COLUMN"],
        "repair_count": 0,
        "max_repairs": 1,
    }
    assert _route_after_validation(state) == "repair"


def test_validation_error_stops_after_repair_budget_is_used() -> None:
    state = {
        "is_valid": False,
        "validation_retryable": True,
        "validation_error_codes": ["UNKNOWN_COLUMN"],
        "repair_count": 1,
        "max_repairs": 1,
    }
    assert _route_after_validation(state) == "end"


def test_security_validation_error_never_repairs() -> None:
    state = {
        "is_valid": False,
        "validation_retryable": False,
        "validation_error_codes": ["READ_ONLY_POLICY_VIOLATION"],
        "repair_count": 0,
        "max_repairs": 1,
    }
    assert _route_after_validation(state) == "end"


def test_successful_execution_goes_to_visualization() -> None:
    assert _route_after_execution({"execution_error": None}) == "visualize"


def test_repairable_db_generation_error_gets_one_repair() -> None:
    state = {
        "execution_error": "Unknown column 'recipe_name'",
        "execution_error_code": "UNKNOWN_COLUMN",
        "execution_retryable": True,
        "repair_count": 0,
        "max_repairs": 1,
    }
    assert _route_after_execution(state) == "repair"


def test_timeout_never_triggers_llm_repair() -> None:
    state = {
        "execution_error": "Query execution was interrupted",
        "execution_error_code": "SQL_TIMEOUT",
        "execution_retryable": False,
        "repair_count": 0,
        "max_repairs": 1,
    }
    assert _route_after_execution(state) == "end"


def test_mysql_error_classification_is_deliberately_small() -> None:
    assert _classify_execution_error(Exception(1054, "Unknown column")) == (
        "UNKNOWN_COLUMN",
        True,
    )
    assert _classify_execution_error(Exception(1064, "Syntax error")) == (
        "SQL_PARSE_ERROR",
        True,
    )
    assert _classify_execution_error(Exception(3024, "timeout")) == (
        "SQL_TIMEOUT",
        False,
    )
    assert _classify_execution_error(Exception(1045, "access denied")) == (
        "DB_PERMISSION_ERROR",
        False,
    )
    assert _classify_execution_error(Exception(9999, "unknown")) == (
        "SQL_EXECUTION_ERROR",
        False,
    )


def test_repair_reason_contains_validation_or_execution_context() -> None:
    validation_reason = _build_repair_reason(
        {
            "validation_error_codes": ["UNKNOWN_TABLE"],
            "validation_errors": ["Schema 中不存在表: recipez"],
        }
    )
    execution_reason = _build_repair_reason(
        {
            "execution_error_code": "UNKNOWN_COLUMN",
            "execution_error": "Unknown column recipe_name",
        }
    )

    assert "UNKNOWN_TABLE" in validation_reason
    assert "recipez" in validation_reason
    assert "UNKNOWN_COLUMN" in execution_reason
    assert "recipe_name" in execution_reason


def test_workflow_revalidates_repair_and_does_not_regenerate_in_loop() -> None:
    workflow_source = Path(
        "multisource_data_agent/application/agents/text2sql/workflow.py"
    ).read_text(encoding="utf-8")
    agent_source = Path(
        "multisource_data_agent/application/agents/tool_execution/sql_agent.py"
    ).read_text(encoding="utf-8")

    assert 'builder.add_node("repair_sql", repair_sql)' in workflow_source
    assert 'builder.add_edge("repair_sql", "validate_sql")' in workflow_source
    assert '"retry": "generate_sql"' not in workflow_source
    repair_cap_pattern = r"min\(\s*1\s*,\s*max\(\s*0"
    assert re.search(repair_cap_pattern, workflow_source)
    assert re.search(repair_cap_pattern, agent_source)
