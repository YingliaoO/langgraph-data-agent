"""
This code is based on content found in the LangGraph documentation: https://python.langchain.com/docs/tutorials/graph/#advanced-implementation-with-langgraph
"""

from itertools import islice
from typing import Any, Callable, Coroutine, Dict, List

from langchain_core.language_models import BaseChatModel
from multisource_data_agent.application.agents.kg_sub_graph.agentic_rag_agents.components.state import OverallState
from multisource_data_agent.application.agents.kg_sub_graph.agentic_rag_agents.components.summarize.evidence_context import build_evidence_context
from multisource_data_agent.application.agents.kg_sub_graph.agentic_rag_agents.components.summarize.answer_generator import generate_answer_with_citations, render_sections
from multisource_data_agent.application.agents.observability import trace_event
from multisource_data_agent.infrastructure.core.logger import get_logger

logger = get_logger(service="summarize-node")


def create_summarization_node(
    llm: BaseChatModel,
) -> Callable[[OverallState], Coroutine[Any, Any, dict[str, Any]]]:
    """
    Create a Summarization node for a LangGraph workflow.

    Parameters
    ----------
    llm : BaseChatModel
        The LLM do perform processing.

    Returns
    -------
    Callable[[OverallState], OutputState]
        The LangGraph node.
    """

    async def summarize(state: OverallState) -> Dict[str, Any]:
        """
        Summarize results of the performed Cypher queries.
        """
        tasks = state.get("tasks", [])
        tool_results = state.get("tool_results") or []
        # The legacy fields are read only when no unified result is available.
        cypher_entries = [] if tool_results else state.get("cyphers", [])
        legacy_sources = [] if tool_results else state.get("sources", [])
        trace = state.get("eval_trace")
        trace_event(
            trace,
            "summarize",
            "start",
            route=state.get("route_type"),
            question=state.get("question", ""),
            metadata={"task_count": len(tasks), "tool_result_count": len(tool_results) if tool_results else len(cypher_entries)},
        )
        logger.info(
            "[SUMMARIZE] start | task_count=%s | tool_result_count=%s | steps=%s",
            len(tasks),
            len(tool_results) if tool_results else len(cypher_entries),
            state.get("steps", []),
        )

        narrative_sections: List[str] = []
        metric_sections: List[str] = []
        error_sections: List[str] = []

        def _format_rows(rows: List[dict[str, Any]]) -> str:
            rows = _compact_rows(rows)
            if not rows:
                return ""
            if len(rows) == 1:
                row = rows[0]
                if len(row) == 1:
                    key, value = next(iter(row.items()))
                    return f"{key}：{value}"
                return "; ".join(f"{key}：{value}" for key, value in row.items())
            lines = []

            # 检查是否是烹饪步骤（包含"步骤序号"和"步骤说明"）
            is_cooking_steps = all("步骤序号" in row and "步骤说明" in row for row in rows if isinstance(row, dict))

            # 检查是否是食材列表（包含"食材"和"用量"）
            is_ingredients = all("食材" in row and "用量" in row for row in rows if isinstance(row, dict))

            for idx, row in enumerate(rows, 1):
                if is_cooking_steps and isinstance(row, dict):
                    # 烹饪步骤：只显示步骤说明
                    step_num = row.get("步骤序号", idx)
                    step_desc = row.get("步骤说明", "")
                    lines.append(f"{step_num}. {step_desc}")
                elif is_ingredients and isinstance(row, dict):
                    # 食材列表：只显示食材名和用量，隐藏关系类型
                    ingredient = row.get("食材", "")
                    amount = row.get("用量", "")
                    relation = row.get("关系类型", "")
                    # 主料用 ★ 标记
                    marker = "★ " if "MAIN" in relation else "  "
                    lines.append(f"{marker}{ingredient}：{amount}")
                else:
                    # 其他数据：显示所有字段
                    row_desc = ", ".join(f"{key}：{value}" for key, value in row.items())
                    lines.append(f"{idx}. {row_desc}")
            return "\n".join(lines)

        def _field(value: Any, name: str, default: Any = None) -> Any:
            return value.get(name, default) if isinstance(value, dict) else getattr(value, name, default)

        def _brief(value: Any, limit: int = 180, depth: int = 0) -> str:
            # Bound nested values before formatting; graph properties can contain large arrays.
            if isinstance(value, str):
                return value[:limit] + ("..." if len(value) > limit else "")
            if isinstance(value, dict):
                if depth >= 2:
                    return "{...}"
                parts = [f"{_brief(key, 40, depth + 1)}: {_brief(item, 90, depth + 1)}"
                         for key, item in islice(value.items(), 5)]
                return "{" + ", ".join(parts) + (", ..." if len(value) > 5 else "") + "}"
            if isinstance(value, (list, tuple)):
                if depth >= 2:
                    return "[...]"
                parts = [_brief(item, 90, depth + 1) for item in value[:5]]
                return "[" + ", ".join(parts) + (", ..." if len(value) > 5 else "") + "]"
            rendered = str(value)
            return rendered[:limit] + ("..." if len(rendered) > limit else "")

        def _compact_rows(rows: Any) -> List[dict[str, Any]]:
            if not isinstance(rows, list):
                return []
            return [
                {str(key)[:60]: _brief(value) for key, value in islice(row.items(), 8)}
                for row in rows[:5]
                if isinstance(row, dict)
            ]

        context = build_evidence_context(tool_results) if tool_results else None
        if context is not None:
            if context.document_facts:
                # Keep the original multi-document question: a list of child
                # retrieval tasks alone can make synthesis answer just one side.
                context.metadata["question"] = str(state.get("question") or "")[:1000]
            narratives, metrics, errors = context.summary_sections()
            narrative_sections.extend(narratives)
            metric_sections.extend(metrics)
            error_sections.extend(errors)

        # A sources-only legacy response has no cyphers to summarize.
        if not tool_results and not cypher_entries and isinstance(legacy_sources, list):
            source_lines = []
            for item in legacy_sources[:4]:
                if isinstance(item, dict):
                    source = item.get("source") or item.get("title") or item.get("id")
                    content = item.get("content") or item.get("snippet") or item.get("text")
                    if source or content:
                        source_lines.append(
                            f"{_brief(source, 120)}: {_brief(content, 350)}" if source else _brief(content, 350)
                        )
                elif isinstance(item, str) and item.strip():
                    source_lines.append(_brief(item, 350))
            if source_lines:
                narrative_sections.append("\n".join(source_lines))

        for idx, cypher in enumerate(cypher_entries):
            if hasattr(cypher, "model_dump"):
                data = cypher.model_dump()
            elif isinstance(cypher, dict):
                data = cypher
            else:
                data = {}

            task_label = ""
            if idx < len(tasks):
                task_label = tasks[idx].question
            else:
                task_label = data.get("task") or ""

            records = data.get("records") or {}
            errors = data.get("errors") or []
            records_count = (
                len(records)
                if isinstance(records, list)
                else len(records.keys())
                if isinstance(records, dict)
                else 1
            )
            logger.info(
                "[SUMMARIZE] consume_result_%s | task=%s | steps=%s | errors=%s | records_count=%s",
                idx + 1,
                task_label,
                data.get("steps", []),
                len(errors),
                records_count,
            )

            if errors:
                error_text = "\uFF1B".join(_brief(error, 100) for error in errors[:3])
                error_sections.append(f"{_brief(task_label, 120)}\uFF1A{error_text}" if task_label else error_text)
                continue

            if not records:
                continue

            if isinstance(records, dict):
                if isinstance(records.get("result"), str) and records["result"].strip():
                    narrative_sections.append(_brief(records["result"].strip(), 700))

                answer = records.get("answer")
                if answer:
                    metric_sections.append(f"{_brief(task_label, 120)}\uFF1A{_brief(answer, 700)}".strip())

                rows = records.get("rows")
                if isinstance(rows, list) and rows:
                    formatted_rows = _format_rows(rows)
                    if formatted_rows:
                        metric_sections.append(
                            f"{task_label}：\n{formatted_rows}".rstrip()
                            if task_label
                            else formatted_rows
                        )
            elif isinstance(records, list):
                # 如果 records 是列表，友好格式化输出
                formatted_rows = _format_rows(records)
                if formatted_rows:
                    metric_sections.append(
                        f"{task_label}：\n{formatted_rows}".rstrip()
                        if task_label
                        else formatted_rows
                    )
            else:
                metric_sections.append(_brief(records, 700))

        citation_ids: list[str] = []
        if context is not None:
            summary, citation_ids = await generate_answer_with_citations(context, llm)
        else:
            summary = render_sections(narrative_sections, metric_sections, error_sections)

        logger.info(
            "[SUMMARIZE] done | summary_len=%s | narrative_sections=%s | metric_sections=%s | error_sections=%s",
            len(summary),
            len(narrative_sections),
            len(metric_sections),
            len(error_sections),
        )
        trace_event(
            trace,
            "summarize",
            "done",
            success=True,
            route=state.get("route_type"),
            question=state.get("question", ""),
            metadata={"summary_len": len(summary), "error_sections": len(error_sections)},
        )
        return {
            "summary": summary,
            "citation_ids": citation_ids,
            "steps": ["summarize"],
            "eval_trace": trace,
        }

    return summarize
