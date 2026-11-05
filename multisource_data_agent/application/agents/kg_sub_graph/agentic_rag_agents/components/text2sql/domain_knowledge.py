"""Legacy compatibility wrapper for canonical Text2SQL domain knowledge."""

from multisource_data_agent.application.agents.text2sql.components.domain_knowledge import (
    COLUMN_DESCRIPTIONS,
    DOMAIN_SUMMARY,
    RELATIONSHIP_FACTS,
    TABLE_DESCRIPTIONS,
)

__all__ = [
    "TABLE_DESCRIPTIONS",
    "COLUMN_DESCRIPTIONS",
    "RELATIONSHIP_FACTS",
    "DOMAIN_SUMMARY",
]
