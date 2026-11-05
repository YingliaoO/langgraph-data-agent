from typing import Literal, Optional

from pydantic import BaseModel, Field

from .text2cypher.state import CypherOutputState
from .visualize.state import VisualizationOutputState


class Task(BaseModel):
    question: str = Field(..., description="The question to be addressed.")
    parent_task: str = Field(
        ..., description="The parent task this task is derived from."
    )
    task_id: str = Field(
        default="",
        description="Stable planner task identifier used for traceability.",
    )
    tool_hint: Literal["auto", "sql_agent", "knowledge_rag", "graph_tool"] = Field(
        default="auto",
        description="Canonical tool intent produced by the generic planner.",
    )
    depends_on: list[str] = Field(
        default_factory=list,
        description="Logical evidence dependencies on earlier planner tasks.",
    )
    requires_visualization: bool = Field(
        default=False,
        description="Whether this task requires a visual to be returned.",
    )
    data: Optional[CypherOutputState] = Field(
        default=None, description="The Cypher query result details."
    )
    visualization: Optional[VisualizationOutputState] = Field(
        default=None, description="The visualization details."
    )

    @property
    def is_complete(self) -> bool:
        viz_bool = (self.requires_visualization and self.visualization is not None) or (
            not self.requires_visualization and self.visualization is None
        )
        return viz_bool and self.data is not None
