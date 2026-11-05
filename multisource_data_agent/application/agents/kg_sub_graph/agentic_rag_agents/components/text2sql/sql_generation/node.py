"""Legacy compatibility wrapper for canonical Text2SQL SQL generation."""

from multisource_data_agent.application.agents.text2sql.components.sql_generation.node import (
    _clean_sql_statement,
    create_sql_generation_node,
)

__all__ = ["create_sql_generation_node"]
