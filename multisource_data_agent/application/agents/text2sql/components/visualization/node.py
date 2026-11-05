"""
Visualization recommendation node.
"""

import json
from typing import Any, Callable, Coroutine, Dict, List

from langchain_core.language_models import BaseChatModel

from multisource_data_agent.application.agents.text2sql.models import VisualizationRecommendation
from multisource_data_agent.infrastructure.core.logger import get_logger

from ..sql_generation.prompts import format_schema_as_text
from .prompts import create_visualization_prompt

logger = get_logger(service="text2sql.visualization")


_CHART_KEYWORDS = (
    "图表",
    "柱状图",
    "折线图",
    "饼图",
    "可视化",
    "趋势",
    "分布",
    "chart",
    "visualization",
)


def create_visualization_node(
    llm: BaseChatModel,
) -> Callable[[Dict[str, Any]], Coroutine[Any, Any, Dict[str, Any]]]:
    """
    Build a LangGraph node that recommends a visualization configuration based
    on the SQL query results.
    """
    prompt = create_visualization_prompt()
    viz_llm = llm.with_structured_output(VisualizationRecommendation)

    async def recommend(state: Dict[str, Any]) -> Dict[str, Any]:
        logger.info("-----开始生成可视化建议-----")

        results: List[Dict[str, Any]] = state.get("execution_results") or []
        question = state.get("question", "")
        column_count = len(results[0]) if results and isinstance(results[0], dict) else 0
        normalized_question = (question or "").lower()

        if results and (
            len(results) <= 1
            or column_count <= 1
            or not any(keyword in normalized_question for keyword in _CHART_KEYWORDS)
        ):
            skip_reason = (
                "row_count<=1"
                if len(results) <= 1
                else "column_count<=1"
                if column_count <= 1
                else "no_chart_keyword"
            )
            logger.info(
                "跳过 Visualization | reason=%s | rows=%s | columns=%s | question=%s",
                skip_reason,
                len(results),
                column_count,
                question,
            )
            return {
                "visualization": VisualizationRecommendation(
                    chart_type="table",
                    title="查询结果",
                ).model_dump(),
                "steps": ["visualization_skipped"],
            }

        if not results:
            logger.info("跳过 Visualization | reason=no_results")
            logger.info("查询结果为空，默认返回表格展示")
            return {
                "visualization": VisualizationRecommendation(
                    chart_type="table",
                    title="查询结果",
                ).model_dump(),
                "steps": ["visualization_default"],
            }

        sample_rows = json.dumps(results[:10], ensure_ascii=False, indent=2, default=str)
        question = state.get("question", "")
        analysis_summary = state.get("analysis_text") or ""
        sql_statement = state.get("sql_statement") or ""

        schema_text = format_schema_as_text(state.get("schema_context") or {})

        inputs = {
            "question": question,
            "analysis_summary": analysis_summary or schema_text,
            "sql_statement": sql_statement,
            "sample_rows": sample_rows,
        }

        try:
            recommendation = await viz_llm.ainvoke(prompt.format_messages(**inputs))
            logger.info("可视化推荐完成: %s", recommendation.chart_type)
            return {
                "visualization": recommendation.model_dump(),
                "steps": ["visualization"],
            }
        except Exception as exc:  # pragma: no cover - defensive logging
            logger.exception("可视化推荐失败: %s", exc)
            return {
                "visualization": VisualizationRecommendation(
                    chart_type="table",
                    title="查询结果",
                ).model_dump(),
                "steps": ["visualization_failed"],
            }

    return recommend
