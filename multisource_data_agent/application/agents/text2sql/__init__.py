"""Canonical Text2SQL domain package.

The package intentionally keeps workflow construction lazy.  Importing a
lightweight model such as ``multisource_data_agent.application.agents.text2sql.models`` must
not initialize the LangGraph workflow (or any legacy KG compatibility layer).
"""

from .models import (
    SQLAnalysis,
    SQLValidationResult,
    SchemaColumn,
    SchemaRelationship,
    SchemaTable,
    VisualizationRecommendation,
)
from .state import Text2SQLInputState, Text2SQLOutputState, Text2SQLState

__all__ = [
    "create_text2sql_workflow",
    "Text2SQLState",
    "Text2SQLInputState",
    "Text2SQLOutputState",
    "SQLAnalysis",
    "SQLValidationResult",
    "VisualizationRecommendation",
    "SchemaTable",
    "SchemaColumn",
    "SchemaRelationship",
]


def __getattr__(name: str):
    """Lazily expose the workflow factory while keeping package imports acyclic."""
    if name == "create_text2sql_workflow":
        from .workflow import create_text2sql_workflow

        return create_text2sql_workflow
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
