from typing import Any, Dict, List, Optional, Literal

from operator import add

import aiohttp

try:  # pragma: no cover - prefer typing_extensions for Pydantic compatibility
    from typing_extensions import Annotated, TypedDict  # type: ignore
except ImportError:  # pragma: no cover - minimal stdlib fallback
    from typing import Annotated, TypedDict

from langchain_core.language_models import BaseChatModel
from langchain_neo4j import Neo4jGraph
from langgraph.constants import END, START
from langgraph.graph.state import CompiledStateGraph, StateGraph
from pydantic import BaseModel, Field
from langchain_core.prompts import ChatPromptTemplate

# 导入输入输出状态定义
from multisource_data_agent.application.agents.kg_sub_graph.agentic_rag_agents.components.state import (
    InputState,
    OutputState,
    OverallState,
)
# 导入guardrails逻辑
from multisource_data_agent.application.agents.kg_sub_graph.agentic_rag_agents.components.guardrails.node import create_guardrails_node
# 导入分解节点
from multisource_data_agent.application.agents.kg_sub_graph.agentic_rag_agents.components.planner import create_planner_node
# 导入工具选择节点
from multisource_data_agent.application.agents.kg_sub_graph.agentic_rag_agents.components.tool_selection import create_tool_selection_node
# 导入Cypher示例检索器基类
from multisource_data_agent.application.agents.kg_sub_graph.agentic_rag_agents.retrievers.cypher_examples.base import BaseCypherExampleRetriever
from multisource_data_agent.application.agents.tool_execution.graph_tool import GraphTool
from multisource_data_agent.application.agents.tool_execution.interface import (
    create_default_tool_executor,
    create_unified_tool_node,
)

from multisource_data_agent.application.agents.tool_contracts import ToolResult
from multisource_data_agent.config import settings
from multisource_data_agent.application.agents.observability import elapsed_ms, trace_event
from multisource_data_agent.infrastructure.core.logger import get_logger
import time


from ...components.errors import create_error_tool_selection_node
from ...components.final_answer import create_final_answer_node



from ...components.summarize import create_summarization_node
from ...components.summarize.evidence_context import build_evidence_context
from ...components.summarize.answer_generator import generate_answer
from multisource_data_agent.application.agents.observability import execution_success



from .edges import (
    guardrails_conditional_edge,
    map_reduce_planner_to_tool_selection,
)

from dataclasses import dataclass, field
# 强制要求数据类中的所有字段必须以关键字参数的形式提供。即不能以位置参数的方式传递。
@dataclass(kw_only=True)
class AgentState(InputState):
    """The router's classification of the user's query."""
    steps: list[str] = field(default_factory=list)
    """Populated by the retriever. This is a list of documents that the agent can reference."""
    question: str = field(default_factory=str) # 这个参数用来与子图进行交互
    answer: str = field(default_factory=str)  # 这个参数用来与子图进行交互


def create_multi_tool_workflow(
    llm: BaseChatModel,
    graph: Neo4jGraph,
    tool_schemas: List[type[BaseModel]],
    predefined_cypher_dict: Dict[str, str],
    cypher_example_retriever: BaseCypherExampleRetriever,
    scope_description: Optional[str] = None,
    llm_cypher_validation: bool = True,
    max_attempts: int = 3,
    attempt_cypher_execution_on_final_attempt: bool = False,
    default_to_text2cypher: bool = True,
) -> CompiledStateGraph:
    """
    Create a multi tool Agent workflow using LangGraph.
    This workflow allows an agent to select from various tools to complete each identified task.

    Parameters
    ----------
    llm : BaseChatModel
        The LLM to use for processing
    graph : Neo4jGraph
        The Neo4j graph wrapper.
    tool_schemas : List[BaseModel]
        Compatibility input. Tool Selection exposes only the canonical three
        schemas even when a legacy caller supplies historical tool models.
    predefined_cypher_dict : Dict[str, str]
        A Python dictionary of Cypher query names as keys and Cypher queries as values.
    scope_description: Optional[str], optional
        A short description of the application scope, by default None
    cypher_example_retriever: BaseCypherExampleRetriever
        The retriever used to collect Cypher examples for few shot prompting.
    llm_cypher_validation : bool, optional
        Whether to perform LLM validation with the provided LLM, by default True
    max_attempts: int, optional
        The max number of allowed attempts to generate valid Cypher, by default 3
    attempt_cypher_execution_on_final_attempt, bool, optional
        THIS MAY BE DANGEROUS.
        Whether to attempt Cypher execution on the last attempt, regardless of if the Cypher contains errors, by default False
    default_to_text2cypher : bool, optional
        Compatibility flag. If no tool call is returned, fall back to the
        canonical GraphTool, which owns Text2Cypher internally.
    initial_state: Optional[InputState], optional
        An initial state passed from parent graph, by default None

    Returns
    -------
    CompiledStateGraph
        The workflow.
    """
    # 1. 创建guardrails节点
    # Guardrails 节点决定传入的问题是否在检索的范围内（比如是否和电商（自家的产品相关））。如果不在，则提供默认消息，并且工作流路由到最终的答案生成。
    guardrails = create_guardrails_node(
        llm=llm, graph=graph, scope_description=scope_description
    )

    # 2. 如果通过guardrails，则会针对用户的问题进行任务分解
    planner = create_planner_node(llm=llm)

    # 3. Phase 2 exposes exactly three canonical execution nodes. GraphTool owns
    # predefined/Text2Cypher strategy selection internally; LightRAG is no longer
    # part of the default orchestration surface.
    graph_tool_impl = GraphTool(
        graph=graph,
        llm=llm,
        predefined_cypher_dict=predefined_cypher_dict,
        cypher_example_retriever=cypher_example_retriever,
        llm_cypher_validation=llm_cypher_validation,
    )
    core_executor = create_default_tool_executor(graph_tool=graph_tool_impl)
    sql_agent = create_unified_tool_node(core_executor, "sql_agent")
    knowledge_rag = create_unified_tool_node(core_executor, "knowledge_rag")
    graph_tool = create_unified_tool_node(core_executor, "graph_tool")

    # Tool Selection may still receive a legacy schema list from compatibility
    # callers, but the selector normalizes that input to the canonical three.
    tool_selection = create_tool_selection_node(
        llm=llm,
        tool_schemas=tool_schemas,
        default_to_text2cypher=default_to_text2cypher,
    )
    summarize = create_summarization_node(llm=llm)
    error_tool_selection = create_error_tool_selection_node()
    final_answer = create_final_answer_node()

    # 创建状态图运行时会维护一个“全局状态”（OverallState），入口状态类型是 InputState，最终产出是 OutputState。节点函数读写的就是这个状态。
    main_graph_builder = StateGraph(OverallState, input=InputState, output=OutputState)

    main_graph_builder.add_node(guardrails)# 安全护栏敏感内容过滤、权限/配额校验
    main_graph_builder.add_node(planner) #决定下一步要用的工具/路径。
    main_graph_builder.add_node("sql_agent", sql_agent)
    main_graph_builder.add_node("knowledge_rag", knowledge_rag)
    main_graph_builder.add_node("graph_tool", graph_tool)
    main_graph_builder.add_node("error_tool_selection", error_tool_selection)
    main_graph_builder.add_node(summarize) # 总结
    main_graph_builder.add_node(tool_selection) #工具选择的中间控制节点（通常结合 planner 的输出）。
    main_graph_builder.add_node(final_answer)


    # 添加边
    main_graph_builder.add_edge(START, "guardrails")
    main_graph_builder.add_conditional_edges(
        "guardrails",
        guardrails_conditional_edge,
    ) #这是条件边：执行完 guardrails 后，不是固定跳到某个节点，而是调用 guardrails_conditional_edge(state) 来返回下一跳的节点名（或一个映射）。
    main_graph_builder.add_conditional_edges(
        "planner",
        map_reduce_planner_to_tool_selection, #据 planner 写进 state 的结果，返回下一个要去的节点名
        ["tool_selection"], #从 planner 出来只能跳到 "tool_selection"，且由 map_reduce_planner_to_tool_selection(state) 来决定（但这里其实被限制成只能选这一个）。
    )

    main_graph_builder.add_edge("sql_agent", "summarize")
    main_graph_builder.add_edge("knowledge_rag", "summarize")
    main_graph_builder.add_edge("graph_tool", "summarize")
    main_graph_builder.add_edge("error_tool_selection", "summarize")
    main_graph_builder.add_edge("summarize", "final_answer")

    main_graph_builder.add_edge("final_answer", END)

    return main_graph_builder.compile()


kb_logger = get_logger(service="kb-multi-tool")


class KBGuardrailsDecision(BaseModel):
    decision: Literal["proceed", "end"]
    summary: Optional[str] = None
    rationale: Optional[str] = None


class KBRouteDecision(BaseModel):
    route: Literal["local", "external", "hybrid"]
    rationale: str
    tools: List[Literal["milvus", "postgres"]] = Field(
        description="本地知识源检索工具列表，支持 milvus/postgres",
    )


class KBInputState(TypedDict):
    question: str
    history: List[Dict[str, str]]
    eval_trace: Dict[str, Any]


class KBWorkflowState(TypedDict):
    question: str
    history: List[Dict[str, str]]
    guardrails_decision: str
    summary: str
    route: str
    kb_tools: List[str]
    milvus_results: List[Dict[str, Any]]
    postgres_results: List[Dict[str, Any]]
    local_results: List[Dict[str, Any]]
    external_results: List[Dict[str, Any]]
    answer: str
    steps: Annotated[List[str], add]
    sources: Annotated[List[str], add]
    tool_results: Annotated[List[ToolResult], add]
    eval_trace: Dict[str, Any]


class KBOutputState(TypedDict):
    answer: str
    steps: List[str]
    sources: List[str]
    tool_results: List[ToolResult]
    eval_trace: Dict[str, Any]


def create_kb_multi_tool_workflow(
    llm: BaseChatModel,
    knowledge_service: Optional[Any] = None,
    *,
    top_k: Optional[int] = None,
    similarity_threshold: Optional[float] = None,
    filter_expr: Optional[str] = None,
    allow_external: Optional[bool] = None,
    external_search_url: Optional[str] = None,
    external_search_timeout: Optional[float] = None,
    scope_description: Optional[str] = None,
) -> CompiledStateGraph:
    """
    Create a multi-tool workflow for knowledge base queries.

    This workflow performs guardrails checking, routes the question to the most
    appropriate retrieval source (local vector store, external API, or both),
    and then synthesises a response with safety-aware instructions.
    """

    effective_top_k = top_k or settings.KB_TOP_K
    effective_threshold = (
        similarity_threshold
        if similarity_threshold is not None
        else settings.KB_SIMILARITY_THRESHOLD
    )

    allow_external_search = (
        allow_external
        if allow_external is not None
        else settings.KB_ENABLE_EXTERNAL_SEARCH
    )

    ingest_service_base = settings.INGEST_SERVICE_URL.rstrip("/") if settings.INGEST_SERVICE_URL else None

    postgres_search_url = (
        f"{ingest_service_base}/api/v1/knowledge/search" if ingest_service_base else None
    )

    external_url = external_search_url or settings.KB_EXTERNAL_SEARCH_URL

    if allow_external_search and not external_url:
        kb_logger.warning(
            "External search enabled but KB_EXTERNAL_SEARCH_URL 未配置，已自动关闭外部检索。"
        )
        allow_external_search = False

    external_is_postgres = bool(
        allow_external_search
        and external_url
        and postgres_search_url
        and external_url.rstrip("/") == postgres_search_url.rstrip("/")
    )

    if not allow_external_search:
        external_url = None

    request_timeout = (
        external_search_timeout
        if external_search_timeout is not None
        else settings.KB_EXTERNAL_SEARCH_TIMEOUT
    )

    knowledge_rag_node = create_knowledge_rag_node(
        knowledge_service=knowledge_service,
        top_k=effective_top_k,
        pgvector_search_url=postgres_search_url,
    )

    scope_text = scope_description or (
        "菜谱文化知识库仅处理菜谱的历史渊源、命名来历、地域流派、典故故事，以及历史名人与菜谱之间的关联信息。"
        "不包含实际烹饪步骤或食材搭配建议。"
        "禁止回答与烹饪做法、医疗养生、隐私、政治、成人内容或其他未授权主题相关的问题。"
    )

    guardrails_prompt = ChatPromptTemplate.from_messages(
        [
            (
                "system",
                (
                    "你是企业知识库的安全审查员。服务范围：\n"
                    f"{scope_text}\n\n"
                    "判断用户问题是否位于该范围，并确保不包含违法、隐私或未授权内容。"
                    "若问题不适宜或不在范围内，请返回 decision='end' 并给出中文 summary；"
                    "否则返回 decision='proceed'。"
                ),
            ),
            ("human", "用户问题：{question}"),
        ]
    )
    guardrails_chain = guardrails_prompt | llm.with_structured_output(KBGuardrailsDecision)

    router_prompt = ChatPromptTemplate.from_messages(
        [
            (
                "system",
                (
                    "你是菜谱文化知识检索路由器。专门负责将历史文化类问题路由到最合适的知识库。\n\n"
                    "## 服务范围\n"
                    '仅限于"菜谱历史/渊源/命名"、"历史名人与菜谱关系"、"菜谱条目级小传与典故"、"菜系流派介绍"。\n\n'
                    "## 本地知识源说明\n"
                    "- **postgres**（PostgreSQL pgvector）：**第一优先级**，存放结构化表格数据、Excel导入的枚举字段\n"
                    "  - 数据更准确、查询更快、覆盖面广\n"
                    "  - 适合：菜谱名称、菜系、历史事件、人物关系等结构化查询\n"
                    "  - 典型问题：菜谱相关的任何历史文化问题\n"
                    "  - 执行策略：系统会**先查 postgres**，如果有结果就直接使用，**不会查询 milvus**\n"
                    "- **milvus**（Milvus向量库）：**仅作为兜底**，存放长文本、文章、典故故事等非结构化内容\n"
                    "  - 只有在 postgres 无结果时才会查询\n"
                    "  - 典型问题：\"宫保鸡丁的完整历史故事\"、\"川菜的详细发展史\"（需要长篇叙事时）\n\n"
                    "## 路由决策规则（严格执行：postgres 优先）\n"
                    "请根据问题特征选择合适的路由和工具：\n\n"
                    "**1. 通用历史文化查询（默认推荐）**\n"
                    "   - 适用于：大部分历史、典故、由来、文化、背景、流派、特点等问题\n"
                    "   - route: local, tools: ['postgres', 'milvus']\n"
                    "   - 执行流程：postgres → 有结果则返回 → 无结果才用 milvus 兜底\n\n"
                    "**2. 明确的结构化查询（postgres 足够）**\n"
                    "   - 适用于：菜名查询、简短事实查询、人物关系、年代查询\n"
                    "   - route: local, tools: ['postgres']\n\n"
                    "**3. 明确需要长文本叙事（可能需要 milvus）**\n"
                    "   - 适用于：用户明确要求\"完整故事\"、\"详细历史\"、\"长篇介绍\"\n"
                    "   - route: local, tools: ['milvus']\n\n"
                    "**4. 外部检索类（需要外网资料）**\n"
                    "   - 本地知识库可能不足，需要外部检索\n"
                    "   - route: hybrid, tools: ['milvus']\n\n"
                    "**5. 超出范围类（拒绝回答）**\n"
                    "   - 问题涉及烹饪步骤、食材搭配等非文化内容\n"
                    "   - route: local, tools: []（空列表表示无法处理）\n\n"
                    "## 输出格式\n"
                    "请输出三个字段：\n"
                    "- route：local（本地）/ external（外部）/ hybrid（混合）\n"
                    "- tools：列表，元素为 'postgres' 和/或 'milvus'，若拒绝回答则为空列表 []\n"
                    "  - **默认推荐**: ['postgres', 'milvus'] 让系统自动优先使用 postgres\n"
                    "- rationale：中文简要说明选择理由（1-2句话）"
                ),
            ),
            (
                "human",
                "用户问题：{question}\n\n最近对话历史：\n{history}",
            ),
        ]
    )
    router_chain = router_prompt | llm.with_structured_output(KBRouteDecision)

    final_prompt = ChatPromptTemplate.from_messages(
        [
            (
                "system",
                (
                    "你是菜谱文化讲解助手，需要依据给定检索结果作答。请遵循：\n"
                    "1. 仅讨论菜谱的历史背景、命名来历、地域流派、典故或与名人的关联，不要给出烹饪步骤或食用建议。\n"
                    "2. 若信息不足，说明知识库暂无相关记载，并建议向其他模块查询。\n"
                    "3. 语气专业、友好，回答使用简体中文。\n"
                    "4. 如问题超出菜谱文化范围，应委婉拒答并说明理由。\n"
                    "5. 区分并融合来自不同数据源的要点，避免重复叙述。\n"
                    "6. 在结尾列出引用来源名称或编号（如有）。"
                ),
            ),
            (
                "human",
                (
                    "用户问题：{question}\n\n"
                    "Milvus 向量检索结果：\n{milvus_context}\n\n"
                    "PostgreSQL 结构化检索结果：\n{postgres_context}\n\n"
                    "外部检索结果：\n{external_context}"
                ),
            ),
        ]
    )

    def _history_to_text(history: List[Dict[str, str]], limit: int = 4) -> str:
        if not history:
            return "（无历史对话）"
        segments: List[str] = []
        for item in history[-limit:]:
            role = item.get("role", "user")
            content = item.get("content", "")
            segments.append(f"{role}: {content}")
        return "\n".join(segments)

    TOOL_LABELS = {
        "milvus": "Milvus",
        "postgres": "PostgreSQL",
    }

    def _format_results(
        results: List[Dict[str, Any]],
        *,
        default_label: str,
        empty_hint: str,
    ) -> str:
        if not results:
            return empty_hint
        snippets: List[str] = []
        for idx, doc in enumerate(results[:effective_top_k]):
            content = doc.get("content") or doc.get("document") or ""
            snippet = content.strip().replace("\n", " ")
            snippet = snippet[:500]
            meta = doc.get("metadata") or {}
            source = (
                doc.get("source")
                or doc.get("source_table")
                or meta.get("source")
                or meta.get("source_table")
                or meta.get("title")
                or ""
            )
            tool_label = TOOL_LABELS.get(str(doc.get("tool", "")).lower(), default_label)
            tag = f"[{tool_label}#{idx + 1}] {snippet}"
            if source:
                tag = f"{tag}\n来源：{source}"
            snippets.append(tag)
        return "\n\n".join(snippets)

    def _format_milvus_results(results: List[Dict[str, Any]]) -> str:
        return _format_results(
            results,
            default_label="Milvus",
            empty_hint="（Milvus 暂无检索结果）",
        )

    def _format_postgres_results(results: List[Dict[str, Any]]) -> str:
        return _format_results(
            results,
            default_label="PostgreSQL",
            empty_hint="（PostgreSQL 暂无检索结果）",
        )

    def _format_combined_local_results(results: List[Dict[str, Any]]) -> str:
        return _format_results(
            results,
            default_label="本地",
            empty_hint="（无本地检索结果）",
        )

    def _format_external_results(results: List[Dict[str, Any]]) -> str:
        if not results:
            return "（无外部检索结果）"
        snippets: List[str] = []
        for idx, item in enumerate(results[:effective_top_k]):
            content = item.get("content") or item.get("summary") or ""
            snippet = content.strip().replace("\n", " ")
            snippet = snippet[:500]
            meta = item.get("metadata") or {}
            source = (
                item.get("source")
                or item.get("source_table")
                or meta.get("source")
                or meta.get("source_table")
                or meta.get("url")
                or ""
            )
            tag = f"[外部#{idx + 1}] {snippet}"
            if source:
                tag = f"{tag}\n来源：{source}"
            snippets.append(tag)
        return "\n\n".join(snippets)

    def _collect_sources(
        *result_sets: List[Dict[str, Any]],
    ) -> List[str]:
        collected: List[str] = []
        for dataset in result_sets:
            for doc in dataset or []:
                meta = doc.get("metadata") or {}
                candidate = (
                    doc.get("source")
                    or doc.get("source_table")
                    or doc.get("document_id")
                    or doc.get("source_id")
                    or doc.get("id")
                    or meta.get("source")
                    or meta.get("source_table")
                    or meta.get("url")
                    or meta.get("title")
                )
                if candidate:
                    collected.append(str(candidate))
        # 去重但保留顺序
        seen: Dict[str, None] = {}
        for source in collected:
            seen.setdefault(source, None)
        return list(seen.keys())

    async def guardrails(state: KBWorkflowState) -> Dict[str, Any]:
        question = state.get("question", "")
        trace = state.get("eval_trace")
        started_at = time.perf_counter()
        trace_event(trace, "kb_guardrails", "start", route="kb-query", question=question)
        decision = await guardrails_chain.ainvoke({"question": question})
        summary = decision.summary or (
            "抱歉，该问题不在菜谱文化知识库的支持范围内，请询问菜谱历史、典故或名人故事相关内容。"
            if decision.decision == "end"
            else ""
        )
        kb_logger.info("KB guardrails decision: {}", decision.decision)
        trace_event(
            trace,
            "kb_guardrails",
            "decision",
            success=True,
            route="kb-query",
            question=question,
            latency_ms=elapsed_ms(started_at),
            fallback=decision.decision == "end",
            metadata={"decision": decision.decision, "rationale": decision.rationale, "summary": summary},
        )
        return {
            "guardrails_decision": decision.decision,
            "summary": summary,
            "steps": ["guardrails"],
            "eval_trace": trace,
        }

    async def router(state: KBWorkflowState) -> Dict[str, Any]:
        question = state.get("question", "")
        trace = state.get("eval_trace")
        started_at = time.perf_counter()
        history_text = _history_to_text(state.get("history", []))
        trace_event(trace, "kb_router", "start", route="kb-query", question=question)
        decision = await router_chain.ainvoke(
            {
                "question": question,
                "history": history_text,
            }
        )
        route = decision.route
        if route in {"external", "hybrid"} and not allow_external_search:
            kb_logger.info(
                "Router requested {} but external search is disabled; using local instead.",
                route,
            )
            route = "local"
        tools = [tool for tool in decision.tools or [] if tool in {"milvus", "postgres"}]
        if route != "external" and not tools:
            # 默认使用 postgres + milvus 兜底策略
            tools = ["postgres", "milvus"]
            kb_logger.info("Router 未指定工具，使用默认策略: postgres 优先 + milvus 兜底")
        kb_logger.info(
            "KB router decision: {} tools={} ({})",
            route,
            tools,
            decision.rationale,
        )
        trace_event(
            trace,
            "kb_router",
            "decision",
            success=True,
            route="kb-query",
            tools=tools,
            question=question,
            latency_ms=elapsed_ms(started_at),
            metadata={"kb_route": route, "rationale": decision.rationale},
        )
        return {
            "route": route,
            "kb_tools": tools,
            "steps": ["router"],
            "eval_trace": trace,
        }

    async def local_search(state: KBWorkflowState) -> Dict[str, Any]:
        """Execute the canonical KnowledgeRAGTool and preserve legacy KB state fields."""
        question = state.get("question", "")
        trace = state.get("eval_trace")
        started_at = time.perf_counter()
        trace_event(
            trace,
            "kb_local_search",
            "start",
            route="kb-query",
            question=question,
            tools=["knowledge_rag"],
            metadata={"legacy_backend_hint": state.get("kb_tools") or []},
        )

        node_result = await knowledge_rag_node(
            {
                "question": question,
                "eval_trace": trace,
                "top_k": effective_top_k,
                "similarity_threshold": effective_threshold,
                "postgres_similarity_threshold": settings.KB_POSTGRES_SIMILARITY_THRESHOLD,
                "filter_expr": filter_expr,
            }
        )
        postgres_results = node_result.get("postgres_results", [])
        milvus_results = node_result.get("milvus_results", [])
        combined_results = node_result.get("local_results", [])
        tool_results = node_result.get("tool_results", [])

        route = state.get("route", "local")
        if (
            not combined_results
            and route in {"local", "hybrid"}
            and allow_external_search
            and external_url
        ):
            kb_logger.info("Canonical KnowledgeRAGTool returned no local evidence; falling back to external search.")
            route = "external"
            trace_event(
                trace,
                "kb_local_search",
                "fallback_to_external",
                success=False,
                route="kb-query",
                question=question,
                fallback=True,
            )

        trace_event(
            trace,
            "kb_local_search",
            "done",
            success=True,
            route="kb-query",
            question=question,
            latency_ms=elapsed_ms(started_at),
            metadata={
                "route": route,
                "postgres_count": len(postgres_results),
                "milvus_count": len(milvus_results),
                "combined_count": len(combined_results),
                "tool_result_count": len(tool_results),
                "tool_boundary": "knowledge_rag",
            },
        )
        return {
            "milvus_results": milvus_results,
            "postgres_results": postgres_results,
            "local_results": combined_results,
            "tool_results": tool_results,
            "route": route,
            "steps": ["local_search"],
            "eval_trace": trace,
        }

    async def external_search(state: KBWorkflowState) -> Dict[str, Any]:
        trace = state.get("eval_trace")
        started_at = time.perf_counter()
        if not (allow_external_search and external_url):
            trace_event(trace, "kb_external_search", "skipped", success=True, route="kb-query", metadata={"reason": "disabled"})
            return {"external_results": [], "steps": ["external_search"], "eval_trace": trace}

        if external_is_postgres and "postgres" in (state.get("kb_tools") or []):
            kb_logger.debug(
                "Skip external search: router already执行了 PostgreSQL 工具，且外部检索与其同源。"
            )
            trace_event(trace, "kb_external_search", "skipped", success=True, route="kb-query", metadata={"reason": "external_is_postgres"})
            return {"external_results": [], "steps": ["external_search"], "eval_trace": trace}

        question = state.get("question", "")
        if not question.strip():
            trace_event(trace, "kb_external_search", "skipped", success=False, route="kb-query", fallback=True, metadata={"reason": "empty_question"})
            return {"external_results": [], "steps": ["external_search"], "eval_trace": trace}
        trace_event(trace, "kb_external_search", "start", route="kb-query", question=question)

        payload: Dict[str, Any] = {
            "query": question,
            "top_k": effective_top_k,
        }
        if effective_threshold is not None:
            payload["threshold"] = effective_threshold

        results: List[Dict[str, Any]] = []
        try:
            timeout_cfg = aiohttp.ClientTimeout(total=request_timeout)
            async with aiohttp.ClientSession(timeout=timeout_cfg) as session:
                async with session.post(external_url, json=payload) as response:
                    if response.status == 200:
                        body = await response.json()
                        data_results = body.get("results") or []
                        if isinstance(data_results, list):
                            results = data_results
                        else:
                            kb_logger.warning(
                                "Unexpected external search payload structure: {}",
                                body,
                            )
                    else:
                        error_text = await response.text()
                        kb_logger.warning(
                            "External KB search failed ({}): {}",
                            response.status,
                            error_text,
                        )
        except Exception as exc:  # pragma: no cover - defensive logging
            kb_logger.error("External KB search error: {}", exc)
            trace_event(
                trace,
                "tool_execution",
                "error",
                success=False,
                route="kb-query",
                tool="external_search",
                question=question,
                latency_ms=elapsed_ms(started_at),
                error=exc,
                fallback=True,
            )

        trace_event(
            trace,
            "kb_external_search",
            "done",
            success=True,
            route="kb-query",
            question=question,
            latency_ms=elapsed_ms(started_at),
            metadata={"result_count": len(results)},
        )
        return {
            "external_results": results,
            "steps": ["external_search"],
            "eval_trace": trace,
        }

    async def finalize(state: KBWorkflowState) -> KBOutputState:
        trace = state.get("eval_trace")
        started_at = time.perf_counter()
        if state.get("guardrails_decision") == "end":
            summary = state.get("summary") or "抱歉，该问题暂时无法回答。"
            trace_event(
                trace,
                "kb_finalize",
                "guardrails_end",
                success=True,
                route="kb-query",
                fallback=True,
                latency_ms=elapsed_ms(started_at),
            )
            return {"answer": summary, "sources": [], "tool_results": [], "steps": ["finalize"], "eval_trace": trace}

        milvus_results = state.get("milvus_results", [])
        postgres_results = state.get("postgres_results", [])
        local_results = state.get("local_results", []) or (milvus_results + postgres_results)
        external_results = state.get("external_results", [])

        milvus_context = _format_milvus_results(milvus_results)
        postgres_context = _format_postgres_results(postgres_results)
        local_context = _format_combined_local_results(local_results)
        external_context = _format_external_results(external_results)

        sources = _collect_sources(milvus_results, postgres_results, external_results)

        tool_results = state.get("tool_results") or []
        if tool_results:
            context = build_evidence_context(tool_results)
            answer = await generate_answer(context, llm)
            if answer == "No data to summarize.":
                answer = "抱歉，菜谱文化知识库暂未找到相关记载，请尝试描述得更具体一些或稍后再试。"
            for result in tool_results:
                status = result.status
                completed = execution_success(status)
                trace_event(
                    trace,
                    "tool_result",
                    "finalized",
                    success=completed,
                    route="kb-query",
                    tool=result.tool_name,
                    metadata={"status": status, "execution_success": completed},
                )
            trace_event(
                trace,
                "kb_finalize",
                "done",
                success=all(execution_success(result.status) for result in tool_results),
                route="kb-query",
                latency_ms=elapsed_ms(started_at),
                metadata={
                    "source_count": len(sources),
                    "answer_length": len(answer),
                    "tool_result_count": len(tool_results),
                    "answer_pipeline": "evidence_context",
                },
            )
            return {"answer": answer, "sources": sources, "tool_results": tool_results, "steps": ["finalize"], "eval_trace": trace}

        if not local_results and not external_results:
            fallback = "抱歉，菜谱文化知识库暂未找到相关记载，请尝试描述得更具体一些或稍后再试。"
            trace_event(
                trace,
                "kb_finalize",
                "no_results",
                success=True,
                route="kb-query",
                fallback=True,
                latency_ms=elapsed_ms(started_at),
                metadata={"source_count": len(sources)},
            )
            return {"answer": fallback, "sources": sources, "tool_results": tool_results, "steps": ["finalize"], "eval_trace": trace}

        messages = final_prompt.format_messages(
            question=state.get("question", ""),
            milvus_context=milvus_context,
            postgres_context=postgres_context,
            external_context=external_context,
        )
        try:
            response = await llm.ainvoke(messages)
            content = getattr(response, "content", None)
            if isinstance(content, str):
                answer = content.strip()
            else:
                answer = str(response)
        except Exception as exc:  # pragma: no cover - defensive logging
            kb_logger.error("Failed to synthesise KB answer: {}", exc)
            answer = local_context if local_context and local_context != "（无本地检索结果）" else ""
            if not answer:
                answer = "检索已完成，但当前无法生成可靠的菜谱文化回答。"

        if not answer:
            answer = "检索已完成，但当前无法生成可靠的菜谱文化回答。"

        if sources:
            sources = list(dict.fromkeys(sources))

        trace_event(
            trace,
            "kb_finalize",
            "done",
            success=True,
            route="kb-query",
            latency_ms=elapsed_ms(started_at),
            metadata={
                "source_count": len(sources),
                "answer_length": len(answer),
                "local_result_count": len(local_results),
                "external_result_count": len(external_results),
            },
        )
        return {
            "answer": answer,
            "sources": sources,
            "tool_results": tool_results,
            "steps": ["finalize"],
            "eval_trace": trace,
        }

    def guardrails_router(state: KBWorkflowState) -> str:
        return "finalize" if state.get("guardrails_decision") == "end" else "kb_router"

    def router_edge(state: KBWorkflowState) -> str:
        return "external_search" if state.get("route") == "external" else "local_search"

    def local_edge(state: KBWorkflowState) -> str:
        route = state.get("route", "local")
        if route in {"hybrid", "external"} and allow_external_search and external_url:
            return "external_search"
        return "finalize"

    graph_builder = StateGraph(
        KBWorkflowState,
        input=KBInputState,
        output=KBOutputState,
    )

    graph_builder.add_node("guardrails", guardrails)
    graph_builder.add_node("kb_router", router)
    graph_builder.add_node("local_search", local_search)
    graph_builder.add_node("external_search", external_search)
    graph_builder.add_node("finalize", finalize)

    graph_builder.add_edge(START, "guardrails")
    graph_builder.add_conditional_edges("guardrails", guardrails_router)
    graph_builder.add_conditional_edges("kb_router", router_edge)
    graph_builder.add_conditional_edges("local_search", local_edge)
    graph_builder.add_edge("external_search", "finalize")
    graph_builder.add_edge("finalize", END)

    return graph_builder.compile()
