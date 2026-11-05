"""Legacy Text2SQL adapter kept for import compatibility.

The canonical SQL tool boundary now lives in
``multisource_data_agent.application.agents.tool_execution.sql_agent``.  New code must import
from that package rather than from the Text2Cypher component tree.
"""

from multisource_data_agent.application.agents.tool_execution.sql_agent import (
    SQLAgent,
    Text2SQLToolOutputState,
    create_sql_agent_node,
    create_text2sql_tool_node,
)

__all__ = [
    "SQLAgent",
    "Text2SQLToolOutputState",
    "create_sql_agent_node",
    "create_text2sql_tool_node",
]
