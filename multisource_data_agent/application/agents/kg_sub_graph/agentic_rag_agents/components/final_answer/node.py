from typing import Any, Callable, Coroutine

from ...components.state import OverallState
from multisource_data_agent.application.agents.observability import execution_success, trace_event
from multisource_data_agent.infrastructure.core.logger import get_logger

logger = get_logger(service="final-answer-node")


def _cypher_history_entry(entry: Any) -> dict[str, Any]:
    """Keep legacy history fields for dicts and Pydantic tool outputs."""
    if isinstance(entry, dict):
        data = entry
    else:
        model_dump = getattr(entry, "model_dump", None)
        if callable(model_dump):
            data = model_dump()
        else:
            data = {
                field: getattr(entry, field, default)
                for field, default in (
                    ("task", ""),
                    ("query", ""),
                    ("statement", ""),
                    ("parameters", {}),
                    ("errors", []),
                    ("records", {}),
                    ("steps", []),
                )
            }
    if not isinstance(data, dict):
        data = {}
    return {
        "task": data.get("task", ""),
        "query": data.get("query", ""),
        "statement": data.get("statement", ""),
        "parameters": data.get("parameters", {}),
        "errors": data.get("errors", []),
        "records": data.get("records", {}),
        "steps": data.get("steps", []),
    }


def create_final_answer_node() -> (
    Callable[[OverallState], Coroutine[Any, Any, dict[str, Any]]]
):
    """
    Create a final_answer node for a LangGraph workflow.

    Parameters
    ----------
    llm : BaseChatModel
        The LLM do perform processing.

    Returns
    -------
    Callable[[OverallState], OutputState]
        The LangGraph node.
    """

    async def final_answer(state: OverallState) -> dict[str, Any]:
        """
        Construct a final answer.
        """

        answer = state.get("summary", " ")
        cyphers = state.get("cyphers") or []
        trace = state.get("eval_trace")
        tool_results = state.get("tool_results") or []
        execution_outcomes = []
        for result in tool_results:
            status = result.status if hasattr(result, "status") else result.get("status", "error")
            tool_name = result.tool_name if hasattr(result, "tool_name") else result.get("tool_name")
            completed = execution_success(status)
            execution_outcomes.append(completed)
            trace_event(
                trace,
                "tool_result",
                "finalized",
                success=completed,
                route=state.get("route_type"),
                tool=tool_name,
                metadata={"status": status, "execution_success": completed},
            )
        trace_event(
            trace,
            "final_answer",
            "start",
            route=state.get("route_type"),
            question=state.get("question", ""),
            metadata={"tool_result_count": len(cyphers), "answer_len": len(str(answer))},
        )
        logger.info(
            "[FINAL_ANSWER] start | question=%s | answer_len=%s | tool_result_count=%s | steps=%s",
            state.get("question", ""),
            len(str(answer)),
            len(cyphers),
            state.get("steps", []),
        )

        history_record = {
            "question": state.get("question", ""),
            "answer": answer,
            "cyphers": [_cypher_history_entry(c) for c in cyphers],
        }

        logger.info(
            "[FINAL_ANSWER] done | history_tool_results=%s",
            len(history_record["cyphers"]),
        )
        trace_event(
            trace,
            "final_answer",
            "done",
            success=all(execution_outcomes),
            route=state.get("route_type"),
            question=state.get("question", ""),
            metadata={"history_tool_results": len(history_record["cyphers"])},
        )
        return {
            "answer": answer,
            "citation_ids": list(state.get("citation_ids") or []),
            "steps": ["final_answer"],
            "history": [history_record],
            "eval_trace": trace,
        }

    return final_answer
