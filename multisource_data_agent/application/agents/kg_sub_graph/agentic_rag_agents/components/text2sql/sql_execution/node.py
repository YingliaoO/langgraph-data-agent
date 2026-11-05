"""Legacy compatibility wrapper for canonical Text2SQL SQL execution.

Private read-only helpers are temporarily re-exported because Phase 1 regression
checks and older internal callers import them from this historical path.
"""

from multisource_data_agent.application.agents.text2sql.components.sql_execution.node import (
    _FILE_EXPORT_KEYWORDS,
    _READ_ONLY_KEYWORDS,
    _WRITE_KEYWORDS,
    _execute_sql_query,
    _get_connection_string,
    _has_file_export_clause,
    _is_read_only_query,
    _map_db_type_to_driver,
    _run_query_sync,
    _tokenize_sql,
    _with_main_keyword,
    create_sql_execution_node,
)

__all__ = ["create_sql_execution_node"]
