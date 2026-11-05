"""Legacy compatibility wrapper for canonical Text2SQL query analysis."""

from multisource_data_agent.application.agents.text2sql.components.query_analysis.node import (
    _AGGREGATION_KEYWORDS,
    _can_skip_query_analysis,
    create_query_analysis_node,
)

__all__ = ["create_query_analysis_node"]
