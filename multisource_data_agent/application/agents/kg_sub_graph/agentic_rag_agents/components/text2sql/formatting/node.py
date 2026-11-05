"""Legacy compatibility wrapper for canonical Text2SQL answer formatting."""

from multisource_data_agent.application.agents.text2sql.components.formatting.node import (
    create_answer_formatter_node,
)

__all__ = ["create_answer_formatter_node"]
