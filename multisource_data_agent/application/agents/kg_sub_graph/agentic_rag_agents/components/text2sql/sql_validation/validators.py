"""Legacy compatibility wrapper for canonical Text2SQL validators."""

from multisource_data_agent.application.agents.text2sql.components.sql_validation.validators import (
    validate_sql_security,
    validate_sql_syntax,
)

__all__ = ["validate_sql_syntax", "validate_sql_security"]
