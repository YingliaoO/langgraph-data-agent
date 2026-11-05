"""Legacy compatibility wrapper for canonical Text2SQL schema retrieval."""

from multisource_data_agent.application.agents.text2sql.components.schema_retrieval.node import (
    _STOP_WORDS,
    _extract_keywords,
    _retrieve_schema_from_mysql,
    _score_table,
    create_schema_retrieval_node,
)

__all__ = ["create_schema_retrieval_node"]
