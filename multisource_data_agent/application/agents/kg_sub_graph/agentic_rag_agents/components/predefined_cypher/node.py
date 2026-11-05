from typing import Any, Callable, Coroutine, Dict, List
import re

from langchain_neo4j import Neo4jGraph
from langchain_openai import ChatOpenAI

from multisource_data_agent.application.agents.tool_contracts import Evidence, Statement, ToolResult

from multisource_data_agent.application.agents.kg_sub_graph.agentic_rag_agents.constants import NO_CYPHER_RESULTS
from multisource_data_agent.application.agents.kg_sub_graph.agentic_rag_agents.components.state import PredefinedCypherInputState
from multisource_data_agent.application.agents.kg_sub_graph.agentic_rag_agents.components.text2cypher.state import CypherOutputState
from multisource_data_agent.application.agents.kg_sub_graph.agentic_rag_agents.components.predefined_cypher.utils import create_vector_query_matcher
from multisource_data_agent.application.agents.kg_sub_graph.agentic_rag_agents.components.predefined_cypher.descriptions import QUERY_DESCRIPTIONS
from multisource_data_agent.config import settings

from multisource_data_agent.infrastructure.core.logger import get_logger
logger = get_logger(service="predefined-cypher")

def create_predefined_cypher_node(
    graph: Neo4jGraph, predefined_cypher_dict: Dict[str, str]
) -> Callable[
    [PredefinedCypherInputState],
    Coroutine[Any, Any, Dict[str, List[CypherOutputState] | List[str]]],
]:
    """
    Create a predefined Cypher execution node for a LangGraph workflow.

    Parameters
    ----------
    graph : Neo4jGraph
        The Neo4j graph wrapper.
    predefined_cypher_dict : Dict[str, str]
        A Python dictionary with Cypher query names as keys and parameterized Cypher queries as values.

    Returns
    -------
    Callable[[PredefinedCypherInputState], Dict[str, List[CypherOutputState] | List[str]]]
        The LangGraph node named `predefined_cypher`.
    """
    matcher = create_vector_query_matcher(predefined_cypher_dict, QUERY_DESCRIPTIONS)

    openai_kwargs: Dict[str, Any] = {
        "model": settings.OPENAI_MODEL,
        "temperature": 0,
    }
    if settings.OPENAI_API_KEY:
        openai_kwargs["openai_api_key"] = settings.OPENAI_API_KEY
    if settings.OPENAI_API_BASE:
        openai_kwargs["openai_api_base"] = settings.OPENAI_API_BASE
    chat_llm = ChatOpenAI(**openai_kwargs)

    async def predefined_cypher(
        state: PredefinedCypherInputState,
    ) -> Dict[str, List[CypherOutputState] | List[str]]:
        """
        Executes a predefined Cypher statement with found parameters.
        """
        errors: List[str] = []

        question = state.get("task", "")
        incoming_params = state.get("query_parameters", {}) or {}
        query_name = incoming_params.get("query") or state.get("query_name", "")
        logger.info(
            "[TOOL_EXEC] start | tool=predefined_cypher | task=%s | query_name=%s",
            question,
            query_name,
        )

        if not query_name:
            matches = matcher.match_query(question, top_k=1)
            if matches:
                query_name = matches[0]["query_name"]
            else:
                errors.append("无法为当前问题匹配到预定义查询。")

        statement = predefined_cypher_dict.get(query_name) if query_name else None
        parameters: Dict[str, Any] = incoming_params.get("parameters") or {}

        logger.info(f"[PREDEFINED_CYPHER] query_name={query_name}, parameters={parameters}")
        logger.info(f"[PREDEFINED_CYPHER] statement={statement}")

        if statement and not parameters:
            parameters = matcher.extract_parameters(question, query_name, llm=chat_llm)

        # 确保参数类型为字符串键
        parameters = {str(k): v for k, v in parameters.items()}

        records: List[Dict[str, Any]] | List[str] = []
        query_completed = False
        if not statement:
            errors.append(f"未找到对应的 Cypher 模板：{query_name}")
        else:
            required_params = {name for name in re.findall(r"\$(\w+)", statement)}
            missing = [name for name in required_params if not parameters.get(name)]
            if missing:
                errors.append(f"缺少查询参数: {', '.join(missing)}")
            else:
                try:
                    records = graph.query(query=statement, params=parameters) or []
                    query_completed = True
                except Exception as exc:
                    logger.exception("Predefined Cypher query failed: %s", exc)
                    errors.append(str(exc))

        graph_status = (
            "error"
            if errors or not query_completed
            else "success" if records else "empty"
        )
        logger.info(
            "[TOOL_EXEC] done | tool=predefined_cypher | task=%s | query_name=%s | errors=%s | records_count=%s",
            question,
            query_name,
            len(errors),
            len(records) if isinstance(records, list) else 1,
        )
        return {
            "cyphers": [
                CypherOutputState(
                    **{
                        "task": state.get("task", ""),
                        "statement": statement or "",
                        "parameters": {
                            "query": query_name,
                            "parameters": parameters,
                        },
                        "errors": errors,
                        "records": records if records else NO_CYPHER_RESULTS,
                        "steps": ["execute_predefined_cypher"],
                    }
                )
            ],
            "tool_results": [
                ToolResult(
                    tool_name="predefined_cypher",
                    tool_kind="graph",
                    status=graph_status,
                    task=question,
                    statement=(
                        Statement(
                            language="cypher",
                            text=statement,
                            parameters=parameters,
                        )
                        if statement else None
                    ),
                    data=records,
                    evidence=[
                        Evidence(
                            kind="graph_record",
                            content=record,
                            source="neo4j",
                            metadata={"row_index": index},
                        )
                        for index, record in enumerate(records)
                    ] if graph_status == "success" else [],
                    metadata={"query_name": query_name, "errors": errors},
                )
            ],
            "steps": ["execute_predefined_cypher"],
        }

    return predefined_cypher
