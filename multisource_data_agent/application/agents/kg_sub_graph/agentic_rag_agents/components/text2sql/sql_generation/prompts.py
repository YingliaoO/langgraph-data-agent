"""Legacy compatibility wrapper for canonical Text2SQL SQL-generation prompts."""

from multisource_data_agent.application.agents.text2sql.components.sql_generation.prompts import (
    create_sql_generation_prompt,
    format_schema_as_text,
)

__all__ = ["create_sql_generation_prompt", "format_schema_as_text"]
