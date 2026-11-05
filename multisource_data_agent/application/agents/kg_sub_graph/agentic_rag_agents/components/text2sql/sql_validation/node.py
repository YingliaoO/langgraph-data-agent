"""Legacy compatibility wrapper for canonical Text2SQL SQL validation."""

from multisource_data_agent.application.agents.text2sql.components.sql_validation.node import (
    create_sql_validation_node,
)

__all__ = ["create_sql_validation_node"]
