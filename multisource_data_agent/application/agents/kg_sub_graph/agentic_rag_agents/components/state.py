from operator import add
from typing import Any, Dict, List, Optional

try:  # pragma: no cover - prefer typing_extensions for Pydantic compatibility
    from typing_extensions import Annotated, TypedDict  # type: ignore
except ImportError:  # pragma: no cover - minimal stdlib fallback
    from typing import Annotated, TypedDict

from langchain_core.messages import ToolCall

from multisource_data_agent.application.agents.tool_contracts import Evidence, ToolResult

from ..components.models import Task
from .text2cypher.state import CypherOutputState
from .visualize.state import VisualizationOutputState


class CypherHistoryRecord(TypedDict):
    """A simplified representation of the CypherOutputState"""

    task: str
    statement: str
    records: List[Dict[str, Any]]


class HistoryRecord(TypedDict):
    """Information that may be relevant to future user questions."""

    question: str
    answer: str
    cyphers: List[CypherHistoryRecord]


def update_history(
    history: List[HistoryRecord], new: List[HistoryRecord]
) -> List[HistoryRecord]:
    """
    Update the history record. Allow only a max number of records to be stored at any time.

    Parameters
    ----------
    history : List[HistoryRecord]
        The current history list.
    new : List[HistoryRecord]
        The new record to add. Should be a single entry list.

    Returns
    -------
    List[HistoryRecord]
        A new List with the record added and old records removed to maintain size.
    """

    SIZE: int = 5

    history.extend(new)
    return history[-SIZE:]


class InputState(TypedDict, total=False):
    """The input state for multi agent workflows."""

    question: str
    data: List[Dict[str, Any]]
    history: Annotated[List[HistoryRecord], update_history]
    route_type: Optional[str]
    eval_trace: Dict[str, Any]


class OverallState(TypedDict, total=False):
    """The main state in multi agent workflows."""

    question: str
    tasks: Annotated[List[Task], add]
    next_action: str
    cyphers: Annotated[List[CypherOutputState], add]
    tool_results: Annotated[List[ToolResult], add]
    evidence: Annotated[List[Evidence], add]
    citation_ids: List[str]
    summary: str
    steps: Annotated[List[str], add]
    history: Annotated[List[HistoryRecord], update_history]
    route_type: Optional[str]
    eval_trace: Dict[str, Any]


class OutputState(TypedDict, total=False):
    """The final output for multi agent workflows."""

    answer: str
    question: str
    tasks: List[Task]
    steps: List[str]
    cyphers: List[CypherOutputState]
    # Canonical semantic results produced by the unified tool boundary.
    # ``cyphers`` remains a compatibility history field for older consumers.
    tool_results: List[ToolResult]
    evidence: List[Evidence]
    citation_ids: List[str]
    execution_summary: Dict[str, Any]
    visualizations: List[VisualizationOutputState]
    history: Annotated[List[HistoryRecord], update_history]
    eval_trace: Dict[str, Any]


class TaskState(TypedDict):
    """The state of a task."""

    question: str
    parent_task: str
    requires_visualization: bool
    data: CypherOutputState
    visualization: VisualizationOutputState


class PredefinedCypherInputState(TypedDict):
    """The input state for a predefined Cypher node."""

    task: str
    query_name: str
    query_parameters: Dict[str, Any]
    steps: List[str]


class ToolSelectionInputState(TypedDict):
    """The input state for canonical Tool Selection."""
    question: str
    parent_task: str
    context: Dict[str, Any]
    eval_trace: Dict[str, Any]


class ToolSelectionOutputState(TypedDict):
    tool_selection_task: str
    tool_call: Optional[ToolCall]
    steps: List[str]


class ToolSelectionErrorState(TypedDict):
    """The input state to the tool selection error handling node."""
    task: str
    errors: List[str]
    steps: List[str]
