"""Compatibility exports for the legacy KG-scoped Text2SQL package.

Canonical Text2SQL implementations now live under
``multisource_data_agent.application.agents.text2sql.components``.  Exports stay lazy to
avoid recreating the historical package-initialization cycle.
"""

from multisource_data_agent.application.agents.text2sql import components as _canonical

__all__ = [
    "create_schema_retrieval_node",
    "create_query_analysis_node",
    "create_sql_generation_node",
    "create_sql_validation_node",
    "create_sql_execution_node",
    "create_visualization_node",
    "create_answer_formatter_node",
]


def __getattr__(name: str):
    if name in __all__:
        return getattr(_canonical, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
