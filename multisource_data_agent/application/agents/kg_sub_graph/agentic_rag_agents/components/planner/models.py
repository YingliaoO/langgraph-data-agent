"""Structured models for the generic Data Agent planner."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


ToolHint = Literal["auto", "sql_agent", "knowledge_rag", "graph_tool"]


class PlannerTask(BaseModel):
    """One independently executable evidence-gathering task.

    ``depends_on`` records logical evidence dependencies for traceability and
    later synthesis.  Tasks are still required to be self-contained so the
    current LangGraph map/reduce executor can run them in parallel safely.
    """

    task_id: str = Field(
        default="",
        description="Stable task identifier such as task_1. Leave empty to let the planner normalize it.",
    )
    question: str = Field(
        ...,
        min_length=1,
        description="A self-contained sub-question that one data tool can answer directly.",
    )
    tool_hint: ToolHint = Field(
        default="auto",
        description=(
            "Preferred canonical evidence source. Use auto when the tool selector should decide. "
            "Only sql_agent, knowledge_rag, graph_tool, or auto are allowed."
        ),
    )
    depends_on: list[str] = Field(
        default_factory=list,
        description=(
            "IDs of earlier tasks whose evidence is logically related to this task. "
            "This is planning metadata, not a runtime blocking dependency."
        ),
    )


class PlannerOutput(BaseModel):
    tasks: list[PlannerTask] = Field(
        default_factory=list,
        description="A bounded list of non-duplicative evidence-gathering tasks.",
    )


__all__ = ["PlannerOutput", "PlannerTask", "ToolHint"]
