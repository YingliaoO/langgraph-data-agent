from typing import Optional, TypedDict

from langchain_core.messages import ToolCall
from pydantic import BaseModel, Field


class sql_agent(BaseModel):
    """Query structured relational data through the canonical SQL Agent."""

    connection_id: Optional[int] = Field(
        default=None,
        description="Optional database connection ID. Leave empty to use the default connection.",
    )
    db_type: str = Field(
        default="MySQL",
        description="Relational database type, for example MySQL or PostgreSQL.",
    )
    max_rows: Optional[int] = Field(
        default=None,
        ge=1,
        le=10000,
        description="Optional result-row cap. Leave empty to use the configured SQL Agent default.",
    )


class knowledge_rag(BaseModel):
    """Retrieve grounded facts from the canonical knowledge retrieval tool."""

    top_k: Optional[int] = Field(
        default=None,
        ge=1,
        le=20,
        description="Optional retrieval depth. Leave empty to use the configured default.",
    )
    similarity_threshold: Optional[float] = Field(
        default=None,
        ge=0.0,
        le=1.0,
        description="Optional minimum semantic similarity threshold.",
    )


class graph_tool(BaseModel):
    """Query Neo4j relationships through the canonical Graph Tool.

    Predefined Cypher and dynamic Text2Cypher are internal GraphTool strategies,
    not separate first-class tools, so no strategy argument is exposed here.
    """

    pass


def get_core_tool_schemas() -> list[type[BaseModel]]:
    """Return the only three schemas exposed to semantic tool selection."""

    return [sql_agent, knowledge_rag, graph_tool]


class ToolSelectionOutputState(TypedDict):
    tool_selection_task: str
    tool_call: Optional[ToolCall]
    next_action: str


__all__ = [
    "ToolSelectionOutputState",
    "get_core_tool_schemas",
    "graph_tool",
    "knowledge_rag",
    "sql_agent",
]
