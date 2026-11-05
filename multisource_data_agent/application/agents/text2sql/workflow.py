"""Canonical Text2SQL LangGraph workflow.

Phase 3 keeps the SQL Agent deliberately bounded: deterministic validation and
execution safety on the happy path, with at most one LLM repair for SQL-
generation failures. Security, timeout and infrastructure failures never loop.
"""

from typing import Literal

from langchain_core.language_models import BaseChatModel
from langgraph.graph import END, START, StateGraph

from multisource_data_agent.infrastructure.core.logger import get_logger

from .components import (
    create_answer_formatter_node,
    create_query_analysis_node,
    create_schema_retrieval_node,
    create_sql_execution_node,
    create_sql_generation_node,
    create_sql_repair_node,
    create_sql_validation_node,
    create_visualization_node,
)
from .state import Text2SQLInputState, Text2SQLState, Text2SQLOutputState

logger = get_logger(service="text2sql.workflow")


def create_text2sql_workflow(
    llm: BaseChatModel,
    db_type: str = "MySQL",
    connection_string: str | None = None,
    max_retries: int = 1,
) -> StateGraph:
    """Assemble Text2SQL with a single bounded repair opportunity.

    ``max_retries`` is retained as a compatibility argument for historical
    callers. Repair budget is carried in workflow state and always capped at one.
    """
    logger.info("创建 Text2SQL 工作流")

    retrieve_schema = create_schema_retrieval_node(connection_string=connection_string)
    analyze_query = create_query_analysis_node(llm)
    generate_sql = create_sql_generation_node(llm, db_type)
    validate_sql = create_sql_validation_node(db_type)
    repair_sql = create_sql_repair_node(llm, db_type)
    execute_sql = create_sql_execution_node(connection_string)
    recommend_viz = create_visualization_node(llm)
    format_answer = create_answer_formatter_node()

    builder = StateGraph(
        Text2SQLState,
        input=Text2SQLInputState,
        output=Text2SQLOutputState,
    )

    builder.add_node("retrieve_schema", retrieve_schema)
    builder.add_node("analyze_query", analyze_query)
    builder.add_node("generate_sql", generate_sql)
    builder.add_node("validate_sql", validate_sql)
    builder.add_node("repair_sql", repair_sql)
    builder.add_node("execute_sql", execute_sql)
    builder.add_node("visualization_node", recommend_viz)
    builder.add_node("format_answer_node", format_answer)

    builder.add_edge(START, "retrieve_schema")
    builder.add_edge("retrieve_schema", "analyze_query")
    builder.add_edge("analyze_query", "generate_sql")
    builder.add_edge("generate_sql", "validate_sql")

    builder.add_conditional_edges(
        "validate_sql",
        _route_after_validation,
        {
            "execute": "execute_sql",
            "repair": "repair_sql",
            "end": "format_answer_node",
        },
    )

    # A repaired candidate always re-enters the complete validator. It is never
    # sent directly to the database.
    builder.add_edge("repair_sql", "validate_sql")

    builder.add_conditional_edges(
        "execute_sql",
        _route_after_execution,
        {
            "visualize": "visualization_node",
            "repair": "repair_sql",
            "end": "format_answer_node",
        },
    )
    builder.add_edge("visualization_node", "format_answer_node")
    builder.add_edge("format_answer_node", END)

    return builder.compile()


def _repair_budget_available(state: Text2SQLState) -> bool:
    repair_count = int(state.get("repair_count", 0) or 0)
    max_repairs = min(1, max(0, int(state.get("max_repairs", 1) or 0)))
    return repair_count < max_repairs


def _route_after_validation(
    state: Text2SQLState,
) -> Literal["execute", "repair", "end"]:
    """Execute valid SQL; repair only generation-quality validation failures."""
    if state.get("is_valid", False):
        return "execute"

    if state.get("validation_retryable", False) and _repair_budget_available(state):
        logger.warning(
            "SQL validation failed; using bounded repair | codes={} | repair_count={}",
            state.get("validation_error_codes") or [],
            state.get("repair_count", 0),
        )
        return "repair"

    logger.error(
        "SQL validation failed terminally | codes={} | retryable={} | repair_count={}",
        state.get("validation_error_codes") or [],
        state.get("validation_retryable", False),
        state.get("repair_count", 0),
    )
    return "end"


def _route_after_execution(
    state: Text2SQLState,
) -> Literal["visualize", "repair", "end"]:
    """Repair only SQL-generation DB errors; all other execution errors stop."""
    if not state.get("execution_error"):
        return "visualize"

    if state.get("execution_retryable", False) and _repair_budget_available(state):
        logger.warning(
            "SQL execution failed with repairable error | code={} | repair_count={}",
            state.get("execution_error_code"),
            state.get("repair_count", 0),
        )
        return "repair"

    logger.error(
        "SQL execution failed terminally | code={} | retryable={} | repair_count={}",
        state.get("execution_error_code"),
        state.get("execution_retryable", False),
        state.get("repair_count", 0),
    )
    return "end"


# Historical private helper name retained for import compatibility. It now uses
# the bounded repair decision rather than the old three-generation retry loop.
def _should_execute_or_retry(state: Text2SQLState) -> Literal["execute", "retry", "end"]:
    route = _route_after_validation(state)
    return "retry" if route == "repair" else route


__all__ = [
    "create_text2sql_workflow",
]
