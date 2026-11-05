"""Canonical Text2SQL workflow components.

Production implementations live in this package.  Exports are loaded lazily so
importing a single component does not initialize the entire Text2SQL workflow.
Legacy KG-scoped modules re-export these implementations for compatibility.
"""

from importlib import import_module

_EXPORTS = {
    "create_schema_retrieval_node": ".schema_retrieval",
    "create_query_analysis_node": ".query_analysis",
    "create_sql_generation_node": ".sql_generation",
    "create_sql_validation_node": ".sql_validation",
    "create_sql_repair_node": ".sql_repair",
    "create_sql_execution_node": ".sql_execution",
    "create_visualization_node": ".visualization",
    "create_answer_formatter_node": ".formatting",
}

__all__ = list(_EXPORTS)


def __getattr__(name: str):
    module_name = _EXPORTS.get(name)
    if module_name is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    return getattr(import_module(module_name, __name__), name)
