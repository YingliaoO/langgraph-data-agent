"""
Query analysis node.
"""

from typing import Any, Callable, Coroutine, Dict

from langchain_core.language_models import BaseChatModel

from multisource_data_agent.application.agents.text2sql.models import SQLAnalysis
from multisource_data_agent.application.agents.text2sql.utils import render_analysis_markdown
from multisource_data_agent.infrastructure.core.logger import get_logger

from ..sql_generation.prompts import format_schema_as_text
from .prompts import create_query_analysis_prompt

logger = get_logger(service="text2sql.query_analysis")


_AGGREGATION_KEYWORDS = (
    "统计",
    "数量",
    "平均",
    "最大",
    "最小",
    "多少",
    "count",
    "sum",
    "avg",
    "max",
    "min",
    "group by",
    "order by",
)


def _can_skip_query_analysis(question: str, schema_context: Dict[str, Any]) -> bool:
    tables = schema_context.get("tables") or []
    normalized_question = (question or "").lower()
    return len(tables) == 1 and not any(
        keyword in normalized_question for keyword in _AGGREGATION_KEYWORDS
    )


def create_query_analysis_node(
    llm: BaseChatModel,
) -> Callable[[Dict[str, Any]], Coroutine[Any, Any, Dict[str, Any]]]:
    """Build a LangGraph node that performs structured query analysis."""

    prompt = create_query_analysis_prompt()
    analysis_chain = prompt | llm.with_structured_output(SQLAnalysis)

    async def analyze(state: Dict[str, Any]) -> Dict[str, Any]:
        logger.info("-----开始分析查询意图-----")

        question = state.get("question", "")
        schema_context = state.get("schema_context") or {}

        if _can_skip_query_analysis(question, schema_context):
            table = (schema_context.get("tables") or [])[0]
            table_name = table.get("table_name", "") if isinstance(table, dict) else ""
            analysis = SQLAnalysis(
                query_intent=question,
                required_tables=[table_name] if table_name else [],
                required_columns=[],
                notes="simple single-table query; query analysis skipped",
            )
            logger.info(
                "跳过 Query Analysis | single_table=%s | question=%s",
                table_name or "unknown",
                question,
            )
            return {
                "analysis": analysis.model_dump(),
                "analysis_text": render_analysis_markdown(analysis, None),
                "steps": ["query_analysis_skipped"],
            }

        logger.info(
            "保留 Query Analysis | table_count=%s | question=%s",
            len(schema_context.get("tables") or []),
            question,
        )
        # value_mappings 功能已简化移除
        mappings_str = ""

        schema_text = format_schema_as_text(schema_context)

        inputs = {
            "db_type": state.get("db_type", "MySQL"),
            "schema": schema_text,
            "value_mappings": mappings_str or "无值映射信息",
            "question": question,
        }

        analysis: SQLAnalysis = await analysis_chain.ainvoke(inputs)
        analysis_dict = analysis.model_dump()
        rendered_markdown = render_analysis_markdown(analysis, None)

        logger.info(
            "查询分析完成，涉及表：%s", ", ".join(analysis.required_tables or [])
        )

        return {
            "analysis": analysis_dict,
            "analysis_text": rendered_markdown,
            "steps": ["query_analysis"],
        }

    return analyze
