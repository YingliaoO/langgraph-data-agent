"""Legacy compatibility wrapper for canonical Text2SQL visualization."""

from multisource_data_agent.application.agents.text2sql.components.visualization.node import (
    _CHART_KEYWORDS,
    create_visualization_node,
)

__all__ = ["create_visualization_node"]
