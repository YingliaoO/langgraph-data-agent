"""Canonical semantic tool selection for the Data Agent.

Phase 2 exposes only three first-class tools to the selector:

- ``sql_agent`` for structured relational data;
- ``knowledge_rag`` for grounded document retrieval;
- ``graph_tool`` for Neo4j relationship queries.

Legacy names such as ``text2sql_query``, ``predefined_cypher``,
``cypher_query`` and ``customer_tools`` are not selectable here. Compatibility
aliases remain available below the orchestration layer through the unified tool
registry, while GraphTool owns its predefined/Text2Cypher strategy internally.
"""

from __future__ import annotations

from typing import Any, Callable, Coroutine, Dict, List, Optional, Set

from langchain_core.language_models import BaseChatModel
from langchain_core.output_parsers import PydanticToolsParser
from langchain_core.runnables.base import Runnable
from langgraph.types import Command, Send
from pydantic import BaseModel

from multisource_data_agent.application.agents.kg_sub_graph.agentic_rag_agents.components.state import (
    ToolSelectionInputState,
)
from multisource_data_agent.application.agents.kg_sub_graph.agentic_rag_agents.components.tool_selection.models import (
    get_core_tool_schemas,
)
from multisource_data_agent.application.agents.kg_sub_graph.agentic_rag_agents.components.tool_selection.prompts import (
    create_tool_selection_prompt_template,
)
from multisource_data_agent.application.agents.kg_sub_graph.agentic_rag_agents.components.tool_selection.source_intent import (
    explicit_source_tool_hint,
)
from multisource_data_agent.application.agents.observability import trace_event
from multisource_data_agent.application.agents.tool_execution.interface import CORE_TOOL_NAMES
from multisource_data_agent.infrastructure.core.logger import get_logger


tool_selection_prompt = create_tool_selection_prompt_template()
logger = get_logger(service="tool-selection")

_ROUTE_TOOL_HINTS: Dict[str, str] = {
    # Preserve old top-level routing semantics while the Router is still Phase-1
    # compatible. These are semantic route hints, not keyword heuristics.
    "text2sql-query": "sql_agent",
    "kb-query": "knowledge_rag",
}


def _route_tool_hint(route_type: str) -> Optional[str]:
    return _ROUTE_TOOL_HINTS.get(str(route_type or "").strip().lower())


def _planner_tool_hint(context: Dict[str, Any]) -> Optional[str]:
    planner_task = context.get("planner_task")
    if not isinstance(planner_task, dict):
        return None
    hint = str(planner_task.get("tool_hint") or "").strip().lower()
    return hint if hint in CORE_TOOL_NAMES else None


def _planner_context(context: Dict[str, Any]) -> Dict[str, Any]:
    planner_task = context.get("planner_task")
    return dict(planner_task) if isinstance(planner_task, dict) else {}


def _schema_title(schema: type[BaseModel]) -> str:
    return str(schema.model_json_schema().get("title") or schema.__name__).strip()


def _canonical_selection_schemas(
    tool_schemas: Optional[List[type[BaseModel]]],
) -> List[type[BaseModel]]:
    """Return exactly the canonical three selection schemas.

    ``tool_schemas`` remains an accepted argument for old callers. If a legacy
    caller passes the historical Cypher/LightRAG/Text2SQL schema list, it is
    intentionally replaced rather than allowing those names back into the
    first-class selection surface.
    """

    supplied = list(tool_schemas or [])
    supplied_names = {_schema_title(schema) for schema in supplied}
    canonical_names = set(CORE_TOOL_NAMES)
    if supplied and supplied_names == canonical_names:
        return supplied
    if supplied:
        logger.info(
            "[TOOL_SELECTION] replacing legacy tool schema surface {} with canonical {}",
            sorted(supplied_names),
            list(CORE_TOOL_NAMES),
        )
    return get_core_tool_schemas()


def _make_command(
    target: str,
    payload: Dict[str, Any],
    *,
    reason: str,
) -> Command[Any]:
    trace = payload.get("eval_trace")
    question = payload.get("task") or payload.get("question") or ""
    trace_event(
        trace if isinstance(trace, dict) else None,
        "tool_selection",
        "dispatch",
        success=True,
        tool=target,
        question=question,
        metadata={
            "query_name": payload.get("query_name", ""),
            "reason": reason,
            "canonical_tool": target,
            "task_id": (payload.get("planner_context") or {}).get("task_id")
            if isinstance(payload.get("planner_context"), dict)
            else None,
        },
    )
    logger.info(
        "[TOOL_SELECTION] dispatch_target={} | reason={} | task={}",
        target,
        reason,
        question,
    )
    return Command(goto=Send(target, payload))


def create_tool_selection_node(
    llm: BaseChatModel,
    tool_schemas: Optional[List[type[BaseModel]]] = None,
    default_to_text2cypher: bool = True,
) -> Callable[[ToolSelectionInputState], Coroutine[Any, Any, Command[Any]]]:
    """Create the semantic selector over the canonical three-tool surface.

    ``default_to_text2cypher`` is retained as a compatibility argument. When it
    is true, an empty LLM tool call falls back to the canonical ``graph_tool``;
    GraphTool itself decides whether predefined or dynamic Cypher is required.
    """

    selection_schemas = _canonical_selection_schemas(tool_schemas)
    tool_selection_chain: Runnable[Dict[str, Any], Any] = (
        tool_selection_prompt
        | llm.bind_tools(tools=selection_schemas)
        | PydanticToolsParser(tools=selection_schemas, first_tool_only=True)
    )
    available_tools: Set[str] = {_schema_title(schema) for schema in selection_schemas}

    async def tool_selection(state: ToolSelectionInputState) -> Command[Any]:
        question_text = state.get("question", "")
        trace = state.get("eval_trace")
        context = state.get("context") or {}
        route_type = str(context.get("route_type") or "").lower()
        trace_event(
            trace,
            "tool_selection",
            "start",
            route=route_type,
            question=question_text,
            metadata={"available_tools": list(CORE_TOOL_NAMES)},
        )
        logger.info(
            "[TOOL_SELECTION] invoked | route_type={} | question={}",
            route_type or "unknown",
            question_text,
        )

        planner_context = _planner_context(context)

        # An explicit user-selected source is a hard constraint, not a semantic
        # routing hint. Check both the sub-task and its original parent because
        # the planner may paraphrase the sub-task and omit the source name.
        explicit_hint = (
            explicit_source_tool_hint(question_text)
            or explicit_source_tool_hint(planner_context.get("parent_task"))
        )
        if explicit_hint:
            return _make_command(
                explicit_hint,
                {
                    "task": question_text,
                    "query_name": explicit_hint,
                    "query_parameters": {},
                    "planner_context": planner_context,
                    "steps": ["tool_selection"],
                    "eval_trace": trace,
                },
                reason="explicit_source_constraint",
            )

        planner_hint = _planner_tool_hint(context)
        if planner_hint:
            return _make_command(
                planner_hint,
                {
                    "task": question_text,
                    "query_name": planner_hint,
                    "query_parameters": {},
                    "planner_context": planner_context,
                    "steps": ["tool_selection"],
                    "eval_trace": trace,
                },
                reason="planner_tool_hint",
            )

        hinted_tool = _route_tool_hint(route_type)
        if hinted_tool:
            return _make_command(
                hinted_tool,
                {
                    "task": question_text,
                    "query_name": hinted_tool,
                    "query_parameters": {},
                    "planner_context": planner_context,
                    "steps": ["tool_selection"],
                    "eval_trace": trace,
                },
                reason=f"route_hint:{route_type}",
            )

        tool_selection_output: BaseModel | None = await tool_selection_chain.ainvoke(
            {"question": question_text}
        )
        if tool_selection_output is not None:
            tool_name = _schema_title(type(tool_selection_output))
            tool_args = tool_selection_output.model_dump(exclude_none=True)
            if tool_name in available_tools and tool_name in CORE_TOOL_NAMES:
                return _make_command(
                    tool_name,
                    {
                        "task": question_text,
                        "query_name": tool_name,
                        "query_parameters": tool_args,
                        "planner_context": planner_context,
                        "steps": ["tool_selection"],
                        "eval_trace": trace,
                    },
                    reason="llm_semantic_selection",
                )

            logger.warning(
                "[TOOL_SELECTION] LLM returned non-core tool={} | available={}",
                tool_name,
                sorted(available_tools),
            )

        if default_to_text2cypher:
            return _make_command(
                "graph_tool",
                {
                    "task": question_text,
                    "query_name": "graph_tool",
                    "query_parameters": {},
                    "planner_context": planner_context,
                    "steps": ["tool_selection"],
                    "eval_trace": trace,
                },
                reason="fallback_default_graph_tool",
            )

        logger.warning("[TOOL_SELECTION] unable to choose a canonical tool")
        return _make_command(
            "error_tool_selection",
            {
                "task": question_text,
                "errors": [f"Unable to assign core tool to question: `{question_text}`"],
                "planner_context": planner_context,
                "steps": ["tool_selection"],
                "eval_trace": trace,
            },
            reason="no_tool_selected",
        )

    return tool_selection


__all__ = [
    "create_tool_selection_node",
]
