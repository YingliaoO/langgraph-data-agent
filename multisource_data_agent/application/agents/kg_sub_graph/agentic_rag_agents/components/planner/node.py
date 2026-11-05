"""Generic task planner for the three-tool Data Agent."""

from __future__ import annotations

import time
from typing import Any, Callable, Coroutine, Dict, Iterable

from langchain_core.language_models import BaseChatModel
from langchain_core.runnables.base import Runnable

from multisource_data_agent.application.agents.kg_sub_graph.agentic_rag_agents.components.models import Task
from multisource_data_agent.application.agents.kg_sub_graph.agentic_rag_agents.components.planner.models import (
    PlannerOutput,
    PlannerTask,
)
from multisource_data_agent.application.agents.kg_sub_graph.agentic_rag_agents.components.planner.prompts import (
    create_planner_prompt_template,
)
from multisource_data_agent.application.agents.kg_sub_graph.agentic_rag_agents.components.state import InputState
from multisource_data_agent.application.agents.observability import elapsed_ms, trace_event
from multisource_data_agent.infrastructure.core.logger import get_logger


logger = get_logger(service="planner_node")
planner_prompt = create_planner_prompt_template()

MAX_PLANNER_TASKS = 6
CANONICAL_TOOL_HINTS = {"auto", "sql_agent", "knowledge_rag", "graph_tool"}
_ROUTE_TOOL_HINTS = {
    "text2sql-query": "sql_agent",
    "kb-query": "knowledge_rag",
}


def _clean_task_id(value: Any) -> str:
    return str(value or "").strip()


def normalize_planner_tasks(
    question: str,
    route_type: str | None,
    tasks: Iterable[PlannerTask],
) -> list[Task]:
    """Normalize LLM planner output into a bounded, topologically ordered plan.

    The current executor is parallel map/reduce.  ``depends_on`` therefore
    carries evidence lineage rather than blocking execution: every question is
    still self-contained.  Dependencies are restricted to earlier tasks so the
    plan is a valid DAG and can be scheduled sequentially in a later phase
    without changing the contract.
    """

    parent = str(question or "").strip()
    raw_tasks = list(tasks or [])[:MAX_PLANNER_TASKS]
    if not raw_tasks:
        raw_tasks = [PlannerTask(question=parent or "请处理当前数据问题")]

    route_hint = _ROUTE_TOOL_HINTS.get(str(route_type or "").strip().lower())
    used_ids: set[str] = set()
    original_to_normalized: dict[str, str] = {}
    staged: list[tuple[PlannerTask, str]] = []

    for index, item in enumerate(raw_tasks, 1):
        original_id = _clean_task_id(item.task_id)
        candidate = original_id
        if not candidate or candidate in used_ids:
            candidate = f"task_{index}"
            suffix = 2
            while candidate in used_ids:
                candidate = f"task_{index}_{suffix}"
                suffix += 1
        used_ids.add(candidate)
        if original_id and original_id not in original_to_normalized:
            original_to_normalized[original_id] = candidate
        staged.append((item, candidate))

    normalized: list[Task] = []
    available_dependencies: set[str] = set()
    for item, task_id in staged:
        task_question = str(item.question or "").strip() or parent
        tool_hint = str(item.tool_hint or "auto").strip().lower()
        if tool_hint not in CANONICAL_TOOL_HINTS:
            tool_hint = "auto"
        if tool_hint == "auto" and route_hint:
            tool_hint = route_hint

        dependencies: list[str] = []
        for raw_dependency in item.depends_on or []:
            dependency = _clean_task_id(raw_dependency)
            dependency = original_to_normalized.get(dependency, dependency)
            if (
                dependency
                and dependency != task_id
                and dependency in available_dependencies
                and dependency not in dependencies
            ):
                dependencies.append(dependency)

        normalized.append(
            Task(
                question=task_question,
                parent_task=parent,
                task_id=task_id,
                tool_hint=tool_hint,
                depends_on=dependencies,
            )
        )
        available_dependencies.add(task_id)

    return normalized


def create_planner_node(
    llm: BaseChatModel,
    ignore_node: bool = False,
    next_action: str = "tool_selection",
) -> Callable[[InputState], Coroutine[Any, Any, Dict[str, Any]]]:
    """Create the generic Data Agent planner node.

    The planner produces self-contained tasks plus canonical tool intent and
    logical dependency metadata.  It does not execute tools and it does not
    expose legacy tool names.
    """

    planner_chain: Runnable[Dict[str, Any], Any] = (
        planner_prompt | llm.with_structured_output(PlannerOutput)
    )

    async def planner(state: InputState) -> Dict[str, Any]:
        trace = state.get("eval_trace")
        started_at = time.perf_counter()
        question = str(state.get("question", "") or "")
        route_type = str(state.get("route_type") or "")
        trace_event(
            trace,
            "planner",
            "start",
            route=route_type,
            question=question,
            metadata={"max_tasks": MAX_PLANNER_TASKS},
        )

        fallback = False
        error: Exception | None = None
        if ignore_node:
            planner_output = PlannerOutput(tasks=[])
        else:
            try:
                planner_output = await planner_chain.ainvoke(
                    {
                        "question": question,
                        "route_type": route_type or "unknown",
                    }
                )
            except Exception as exc:  # keep planning failures from killing the agent
                fallback = True
                error = exc
                logger.warning("[PLANNER] structured planning failed; using single task: {}", exc)
                planner_output = PlannerOutput(tasks=[])

        tasks = normalize_planner_tasks(question, route_type, planner_output.tasks)
        planner_task_decomposition = {
            "next_action": next_action,
            "tasks": tasks,
        }

        logger.info("=" * 80)
        logger.info("[PLANNER] parent question: {}", question)
        logger.info("[PLANNER] total_sub_tasks={}", len(tasks))
        for index, task in enumerate(tasks, 1):
            logger.info(
                "[PLANNER] sub_task_{} | id={} | tool_hint={} | depends_on={} | question={}",
                index,
                task.task_id,
                task.tool_hint,
                task.depends_on,
                task.question,
            )
        logger.info("=" * 80)

        trace_event(
            trace,
            "planner",
            "done",
            success=not fallback,
            fallback=fallback,
            route=route_type,
            question=question,
            latency_ms=elapsed_ms(started_at),
            error=error,
            metadata={
                "task_count": len(tasks),
                "tasks": [
                    {
                        "task_id": task.task_id,
                        "question": task.question,
                        "parent_task": task.parent_task,
                        "tool_hint": task.tool_hint,
                        "depends_on": list(task.depends_on),
                    }
                    for task in tasks
                ],
            },
        )
        planner_task_decomposition["eval_trace"] = trace
        return planner_task_decomposition

    return planner


__all__ = [
    "CANONICAL_TOOL_HINTS",
    "MAX_PLANNER_TASKS",
    "create_planner_node",
    "normalize_planner_tasks",
]
