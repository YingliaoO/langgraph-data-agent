"""Legacy compatibility wrapper for canonical Text2SQL query-analysis prompts."""

from multisource_data_agent.application.agents.text2sql.components.query_analysis.prompts import (
    create_query_analysis_prompt,
)

__all__ = ["create_query_analysis_prompt"]
