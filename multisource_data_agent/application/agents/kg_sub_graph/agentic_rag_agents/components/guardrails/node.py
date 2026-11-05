"""
This code is based on content found in the LangGraph documentation: https://python.langchain.com/docs/tutorials/graph/#advanced-implementation-with-langgraph
"""

from typing import Any, Callable, Coroutine, Dict, Optional

from langchain_core.language_models import BaseChatModel
from langchain_core.runnables.base import Runnable
from langchain_neo4j import Neo4jGraph


from multisource_data_agent.application.agents.kg_sub_graph.agentic_rag_agents.components.guardrails.models import GuardrailsOutput
from multisource_data_agent.application.agents.kg_sub_graph.agentic_rag_agents.components.guardrails.prompts import create_guardrails_prompt_template
from multisource_data_agent.application.agents.kg_sub_graph.agentic_rag_agents.components.state import InputState
from multisource_data_agent.application.agents.kg_sub_graph.agentic_rag_agents.components.tool_selection.source_intent import (
    explicit_source_tool_hint,
)
from multisource_data_agent.application.agents.observability import trace_event
from multisource_data_agent.infrastructure.core.logger import get_logger

# 获取日志记录器
logger = get_logger(service="guardrails_node")


def create_guardrails_node(
    llm: BaseChatModel,
    graph: Optional[Neo4jGraph] = None,
    scope_description: Optional[str] = None,
) -> Callable[[InputState], Coroutine[Any, Any, dict[str, Any]]]:
    """
    Create a guardrails node to be used in a LangGraph workflow.

    Parameters
    ----------
    llm : BaseChatModel
        The LLM used to process data.
    graph: Optional[Neo4jGraph], optional
        The `Neo4jGraph` object used to generated a schema definition, by default None
    scope_description : Optional[str], optional
        A description of the application scope, by default None

    Returns
    -------
    Callable[[InputState], OverallState]
        The LangGraph node.
    """

    # 获取包含了图表结构和范围描述的guardrails完整提示词
    guardrails_prompt = create_guardrails_prompt_template(
        graph=graph, scope_description=scope_description
    )

    # 使用LLM进行结构化输出
    guardrails_chain: Runnable[Dict[str, Any], Any] = (
        guardrails_prompt | llm.with_structured_output(GuardrailsOutput)
    )

    async def guardrails(state: InputState) -> Dict[str, Any]:
        """
        Decides if the question is in scope.
        """

        # 提取到输入的问题
        question = state.get("question", "")
        trace = state.get("eval_trace")
        trace_event(trace, "guardrails", "start", route=state.get("route_type"), question=question)

        heuristics_keywords = [
            "retail_",
            "订单",
            "客户",
            "商品",
            "产品",
            "供应商",
            "品类",
            "分类",
            "退款",
            "退货",
            "活动",
            "会员",
            "政策",
            "sop",
            "sla",
            "统计",
            "多少",
            "数量",
            "销售",
            "销量",
            "退款率",
        ]

        lowered = question.lower()

        # Explicit source selection must reach the canonical tool boundary so
        # SQL safety policy can return a structured policy rejection. Treating
        # it as out-of-scope here would turn policy into an empty fallback.
        explicit_source_hint = explicit_source_tool_hint(question)
        if explicit_source_hint:
            logger.info(
                "Guardrails 检测到用户显式指定数据源 %s，直接进入 planner。",
                explicit_source_hint,
                extra={"question": question},
            )
            trace_event(
                trace,
                "guardrails",
                "decision",
                success=True,
                route=state.get("route_type"),
                next_node="planner",
                question=question,
                metadata={
                    "reason": "explicit_source_constraint",
                    "tool_hint": explicit_source_hint,
                },
            )
            return {
                "next_action": "planner",
                "summary": None,
                "steps": ["guardrails"],
                "eval_trace": trace,
            }

        # Router 已经判定为结构化数据请求时，直接交给 planner。
        # 避免 Guardrails 因为问题没有出现某个业务领域词而误判为超出范围。
        if state.get("route_type") == "text2sql-query":
            logger.info(
                "Guardrails 检测到 Router 已判定为 Text2SQL 请求，直接进入 planner。",
                extra={"question": question},
            )
            trace_event(
                trace,
                "guardrails",
                "decision",
                success=True,
                route=state.get("route_type"),
                next_node="planner",
                question=question,
                metadata={"reason": "router_text2sql"},
            )
            return {
                "next_action": "planner",
                "summary": None,
                "steps": ["guardrails"],
                "eval_trace": trace,
            }

        if any(keyword in question for keyword in heuristics_keywords) or "?" in question or "？" in question:
            logger.info(
                "Guardrails 前置规则命中企业零售/统计关键词，直接进入 planner。",
                extra={"question": question},
            )
            trace_event(trace, "guardrails", "decision", success=True, route=state.get("route_type"), next_node="planner", question=question, metadata={"reason": "heuristic"})
            return {"next_action": "planner", "summary": None, "steps": ["guardrails"], "eval_trace": trace}

        # 使用LLM进行结构化输出
        try:
            guardrails_output: GuardrailsOutput = await guardrails_chain.ainvoke(
                {"question": question}
            )
        except Exception as exc:  # pragma: no cover - 容错
            logger.warning("Guardrails LLM 调用失败，回退到 planner: %s", exc)
            trace_event(trace, "guardrails", "decision", success=False, route=state.get("route_type"), next_node="planner", question=question, error=exc, fallback=True)
            return {"next_action": "planner", "summary": None, "steps": ["guardrails"], "eval_trace": trace}

        decision = guardrails_output.decision
        summary = None
        if decision == "end":
            if any(keyword in question for keyword in heuristics_keywords) or "?" in question or "？" in question:
                logger.info(
                    "Guardrails 触发兜底：问题含有企业零售领域关键词，进入 planner。",
                    extra={"question": question},
                )
                decision = "planner"

        if decision == "end":
            summary = "抱歉，暂时没有检索到相关企业零售数据或文档。"

        decision_info = {
            "next_action": decision,
            "summary": summary,
            "steps": ["guardrails"],
        }

        logger.info(f"Guardrails Decision Info: {decision_info}")
        trace_event(trace, "guardrails", "decision", success=True, route=state.get("route_type"), next_node=decision, question=question, fallback=decision == "end", metadata={"summary": summary})
        decision_info["eval_trace"] = trace

        return decision_info


    return guardrails
