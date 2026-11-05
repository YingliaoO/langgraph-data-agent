from multisource_data_agent.application.agents.lg_prompts import (
    ROUTER_SYSTEM_PROMPT,
    GET_ADDITIONAL_SYSTEM_PROMPT,
    GENERAL_QUERY_SYSTEM_PROMPT,
    GUARDRAILS_SYSTEM_PROMPT,
    CHECK_HALLUCINATIONS,
)
from langchain_core.runnables import RunnableConfig
from multisource_data_agent.config import settings
from multisource_data_agent.infrastructure.core.logger import get_logger
from typing import cast, Literal, List, Dict, Any, Optional
from langchain_core.messages import BaseMessage
from langgraph.checkpoint.memory import MemorySaver
from langgraph.graph import END, START, StateGraph
from multisource_data_agent.application.agents.lg_states import AgentState, InputState, Router, GradeHallucinations
from multisource_data_agent.application.agents.routing import CANONICAL_ROUTE_TYPES, normalize_route_type
from multisource_data_agent.application.agents.kg_sub_graph.agentic_rag_agents.components.planner.node import create_planner_node
from multisource_data_agent.application.agents.kg_sub_graph.agentic_rag_agents.workflows.multi_agent.multi_tool import (
    create_multi_tool_workflow,
)
from pydantic import BaseModel, Field
from langchain_core.messages import AIMessage
from multisource_data_agent.application.agents.kg_sub_graph.agentic_rag_agents.components.utils.utils import \
    retrieve_and_parse_schema_from_graph_for_prompts
from langchain_core.prompts import ChatPromptTemplate
import os
import asyncio
import aiohttp
import time
from pathlib import Path

from langchain_openai import ChatOpenAI
from multisource_data_agent.application.agents.llm_observability import LLMObservationCallback
from multisource_data_agent.application.agents.observability import (
    attach_trace_to_message,
    elapsed_ms,
    get_trace,
    merge_trace,
    trace_event,
    trace_execution_success,
)
from multisource_data_agent.application.agents.execution_contract import (
    execution_summary,
    normalize_tool_results,
)
class AdditionalGuardrailsOutput(BaseModel):
    """
    格式化输出，用于判断用户的问题是否与图谱内容相关
    """
    decision: Literal["end", "proceed"] = Field(
        description="Decision on whether the question is related to the graph contents."
    )


# 构建日志记录器
logger = get_logger(service="lg_builder")


def _ensure_router(router_obj: Any, *, fallback_question: str = "") -> Router:
    """Convert current/legacy router payloads into the canonical product-route model."""
    candidate: Router | None = None
    if isinstance(router_obj, Router):
        candidate = router_obj
    elif isinstance(router_obj, dict):
        try:
            candidate = Router.model_validate(router_obj)
        except Exception:
            candidate = None

    if candidate is None:
        candidate = Router(type="data-query", logic="missing router", question=fallback_question)

    canonical = normalize_route_type(candidate.type)
    return Router(
        type=canonical,
        logic=candidate.logic,
        question=candidate.question or fallback_question,
        decision=candidate.decision,
        confidence=candidate.confidence,
        reasoning=candidate.reasoning,
    )


def _extract_configurable(config: Any) -> Dict[str, Any]:
    """提取 LangGraph RunnableConfig 中的 configurable 字段，确保返回字典。"""
    if not config:
        return {}
    if isinstance(config, dict):
        value = config.get("configurable", {})
        return value if isinstance(value, dict) else {}
    # LangGraph RunnableConfig 支持属性访问或字典接口
    configurable = getattr(config, "configurable", None)
    if isinstance(configurable, dict):
        return configurable
    getter = getattr(config, "get", None)
    if callable(getter):
        try:
            value = getter("configurable", {})
            if isinstance(value, dict):
                return value
        except Exception:  # pragma: no cover - 容错
            pass
    return {}

def _coerce_to_bool(value: Any, *, default: bool = False) -> bool:
    """Best-effort conversion of dynamic configuration values to boolean."""
    if value is None:
        return default
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "y", "on"}
    return bool(value)

# 意图识别路由节点
async def analyze_and_route_query(
        state: AgentState, *, config: RunnableConfig
) -> dict[str, Router]:
    """分析用户提问并确定合适的路由方式。

    本函数借助大语言模型对用户查询进行分类，以此判定在对话流程中该如何分配请求路由。

    参数：
        state (AgentState)：智能体当前状态，包含对话历史记录。
        config (RunnableConfig)：运行配置，内含本次查询分析所使用的模型信息。

    返回值：
        dict[str, Router]：字典类型，包含键「router」，对应值为分类结果（含分类类型与判定逻辑）。
"""

    if state.uploaded_file:
        return {"router": Router(type="file-query", logic="Authenticated upload", question="")}
    if not settings.OPENAI_API_KEY:
        raise RuntimeError("OPENAI_API_KEY is not configured for router analysis.")

    model = ChatOpenAI(callbacks=[LLMObservationCallback()],
        openai_api_key=settings.OPENAI_API_KEY,
        model_name=settings.OPENAI_MODEL,
        openai_api_base=settings.OPENAI_API_BASE,
        # Routing is a classification boundary.  Keep it deterministic so
        # identical data questions do not randomly fall into general-query.
        temperature=0,
        tags=["router"],
    )

    # 拼接提示模版 + 用户的实时问题（包含历史上下文对话）
    messages = [
                   {"role": "system", "content": ROUTER_SYSTEM_PROMPT}
               ] + state.messages
    logger.info("-----Analyze user query type-----")
    logger.info(f"History messages: {state.messages}")

    question_text = state.messages[-1].content if state.messages else ""
    trace = get_trace(state, config)
    trace_event(trace, "router", "start", question=question_text)
    fallback_router = Router(
        type="data-query",
        logic="fallback: default to Data Agent planning",
        question=question_text,
    )

    allowed_types = set(CANONICAL_ROUTE_TYPES)

    try:
        raw_response = await asyncio.wait_for(
            model.with_structured_output(Router).ainvoke(messages),
            timeout=float(settings.ROUTER_TIMEOUT_SECONDS),
        )
    except Exception as exc:
        logger.warning("Router LLM failed: {}. Falling back to data-query.", exc)
        trace_event(
            trace,
            "router",
            "decision",
            success=False,
            route=fallback_router.type,
            question=question_text,
            error=exc,
            error_code=("router_timeout" if isinstance(exc, asyncio.TimeoutError) else None),
            fallback=True,
            metadata={
                "logic": fallback_router.logic,
                "timeout_budget_ms": round(float(settings.ROUTER_TIMEOUT_SECONDS) * 1000, 2),
            },
        )
        return {"router": fallback_router}

    response = raw_response if isinstance(raw_response, Router) else Router.model_validate(raw_response)
    raw_router_type = response.type
    router_type = normalize_route_type(raw_router_type, default="")
    logic = response.logic or ""

    if router_type not in allowed_types:
        logger.warning("Router returned invalid type `{}`; falling back to data-query.", raw_router_type)
        trace_event(
            trace,
            "router",
            "decision",
            success=False,
            route="data-query",
            question=question_text,
            fallback=True,
            metadata={"logic": logic or "fallback: invalid router output", "raw_route": raw_router_type},
        )
        return {
            "router": Router(
                type="data-query",
                logic=logic or "fallback: invalid router output",
                question=question_text,
            )
        }

    sanitized_router = Router(
        type=router_type,
        logic=logic,
        question=response.question or question_text,
        decision=response.decision,
        confidence=response.confidence,
        reasoning=response.reasoning,
    )

    # Heuristic router is only used when the LLM output is invalid (handled above).
    logger.info(f"Analyze user query type completed, result: {sanitized_router}")
    trace_event(
        trace,
        "router",
        "decision",
        success=True,
        route=sanitized_router.type,
        question=question_text,
        metadata={
            "logic": sanitized_router.logic,
            "confidence": sanitized_router.confidence,
            "reasoning": sanitized_router.reasoning,
            "raw_route": raw_router_type,
            "canonical_route": sanitized_router.type,
            "legacy_normalized": raw_router_type != sanitized_router.type,
        },
    )
    return {"router": sanitized_router}


def route_query(
        state: AgentState,
) -> Literal[
    "respond_to_general_query", "get_additional_info", "create_research_plan", "create_file_query"]:
    """根据查询分类确定下一步操作。

    Args:
        state (AgentState): 当前代理状态，包括路由器的分类。

    Returns:
        Literal["respond_to_general_query", "get_additional_info", "create_research_plan", "create_file_query"]: 下一步操作。
    """
    router = _ensure_router(getattr(state, "router", None), fallback_question=state.messages[-1].content if state.messages else "")
    state.router = router
    _type = normalize_route_type(router.type)
    trace = get_trace(state)

    # Uploaded tabular datasets are the only non-chat payload handled by the
    # product router. Image/Vision processing is intentionally out of scope.
    if state.uploaded_file:
        trace_event(trace, "route_query", "next_node", route="file-query", next_node="create_file_query", metadata={"reason": "authenticated_file_id"})
        return "create_file_query"

    if _type == "general-query":
        next_node = "respond_to_general_query"
    elif _type == "additional-query":
        next_node = "get_additional_info"
    elif _type == "data-query":
        next_node = "create_research_plan"
    elif _type == "file-query":
        next_node = "create_file_query"
    else:
        raise ValueError(f"Unknown router type {_type}")
    trace_event(
        trace,
        "route_query",
        "next_node",
        route=_type,
        next_node=next_node,
        metadata={"logic": router.logic},
    )
    return next_node


async def respond_to_general_query(
        state: AgentState, *, config: RunnableConfig
) -> Dict[str, List[BaseMessage]]:
    """生成对一般查询的响应，完全基于大模型，不会触发任何外部服务的调用，包括自定义工具、知识库查询等。
    当路由器将查询分类为一般问题时，将调用此节点。
    Args:
        state (AgentState): 当前代理状态，包括对话历史和路由逻辑。
        config (RunnableConfig): 用于配置响应生成的模型。
    Returns:
        Dict[str, List[BaseMessage]]: 包含'messages'键的字典，其中包含生成的响应。
    """
    logger.info("-----generate general-query response-----")
    trace = get_trace(state, config)
    started_at = time.perf_counter()
    question_text = state.messages[-1].content if state.messages else ""
    trace_event(trace, "general_query", "start", question=question_text)

    # 使用大模型生成回复
    model = ChatOpenAI(callbacks=[LLMObservationCallback()], openai_api_key=settings.OPENAI_API_KEY, model_name=settings.OPENAI_MODEL,
                       openai_api_base=settings.OPENAI_API_BASE, temperature=0.7,
                       tags=["general_query"])

    router = _ensure_router(getattr(state, "router", None), fallback_question=state.messages[-1].content if state.messages else "")
    state.router = router
    system_prompt = GENERAL_QUERY_SYSTEM_PROMPT.format(
        logic=router.logic
    )

    messages = [{"role": "system", "content": system_prompt}] + state.messages
    response = await model.ainvoke(messages)
    trace_event(
        trace,
        "general_query",
        "done",
        success=True,
        route="general-query",
        question=question_text,
        latency_ms=elapsed_ms(started_at),
        metadata={"answer_length": len(str(getattr(response, "content", "")))},
    )
    attach_trace_to_message(response, trace)
    return {"messages": [response]}

#大模型生成输出多了一些额外消息
async def get_additional_info(
        state: AgentState, *, config: RunnableConfig
) -> Dict[str, List[BaseMessage]]:
    """生成一个响应，要求用户提供更多信息。

    当路由确定需要从用户那里获取更多信息时，将调用此函数。

    Args:
        state (AgentState): 当前代理状态，包括对话历史和路由逻辑。
        config (RunnableConfig): 用于配置响应生成的模型。

    Returns:
        Dict[str, List[BaseMessage]]: 包含'messages'键的字典，其中包含生成的响应。
    """
    logger.info("------continue to get additional info------")
    trace = get_trace(state, config)
    started_at = time.perf_counter()
    question_text = state.messages[-1].content if state.messages else ""
    trace_event(trace, "additional_query", "start", question=question_text)

    # 使用大模型生成回复
    model = ChatOpenAI(callbacks=[LLMObservationCallback()], openai_api_key=settings.OPENAI_API_KEY, model_name=settings.OPENAI_MODEL,
                       openai_api_base=settings.OPENAI_API_BASE, temperature=0.7,
                       tags=["additional_info"])
    # 如果用户的问题与当前企业零售数据域无关，则返回范围外提示

    # 首先连接 Neo4j 图数据库
    try:
        neo4j_graph = get_neo4j_graph()
        logger.info("success to get Neo4j graph database connection")
    except Exception as e:
        logger.error(f"failed to get Neo4j graph database connection: {e}")
        neo4j_graph = None

    # 定义 Enterprise Retail Data Agent 服务范围（用户友好的业务描述）
    scope_description = """
    Enterprise Retail Data Agent 服务范围：为企业零售演示数据提供可解释的数据查询和文档检索，包括但不限于：

    📦 结构化业务数据
    - 客户、商品、订单、订单明细和退款查询
    - 计数、聚合、过滤、排序、销售和退款率分析

    🔗 企业实体关系
    - 商品、供应商、商品分类和活动之间的关系与路径
    - 供应商供货商品、商品所属分类和活动关联

    📚 企业文档知识
    - 退货政策、退款异常 SOP、活动规则和供应商 SLA
    - 从知识库返回可引用的政策和流程证据

    暂不支持：政治、娱乐八卦、新闻时事、天气预报、医疗诊断等非企业零售数据内容。
    """

    scope_context = (
        f"参考此范围描述来决策:\n{scope_description}"
        if scope_description is not None
        else ""
    )

    # 动态从 Neo4j 图表中获取图表结构
    graph_context = (
        f"\n参考图表结构来回答:\n{retrieve_and_parse_schema_from_graph_for_prompts(neo4j_graph)}"
        if neo4j_graph is not None
        else ""
    )

    message = scope_context + graph_context + "\nQuestion: {question}"

    # 拼接提示模版
    full_system_prompt = ChatPromptTemplate.from_messages(
        [
            (
                "system",
                GUARDRAILS_SYSTEM_PROMPT,
            ),
            (
                "human",
                (message),
            ),
        ]
    )

    # 构建格式化输出的 Chain， 如果匹配，返回 continue，否则返回 end
    guardrails_chain = full_system_prompt | model.with_structured_output(AdditionalGuardrailsOutput)
    guardrails_output: AdditionalGuardrailsOutput = await guardrails_chain.ainvoke(
        {"question": state.messages[-1].content if state.messages else ""}
    )

    # 空值检查：如果 LLM 返回 None，默认为 proceed
    if guardrails_output is None:
        logger.warning("Guardrails returned None, defaulting to proceed")
        guardrails_output = AdditionalGuardrailsOutput(decision="proceed")
    trace_event(
        trace,
        "additional_guardrails",
        "decision",
        success=True,
        route="additional-query",
        question=question_text,
        metadata={"decision": guardrails_output.decision},
    )

    # 根据格式化输出的结果，返回不同的响应
    if guardrails_output.decision == "end":
        logger.info("-----Fail to pass guardrails check-----")
        ai_message = AIMessage(content="您好～抱歉，这个问题不属于当前企业零售数据范围。您可以询问订单、商品、供应商、活动或政策信息。")
        trace_event(
            trace,
            "additional_query",
            "end",
            success=True,
            route="additional-query",
            question=question_text,
            latency_ms=elapsed_ms(started_at),
            fallback=True,
            metadata={"decision": "end"},
        )
        attach_trace_to_message(ai_message, trace)
        return {"messages": [ai_message]}
    else:
        logger.info("-----Pass guardrails check-----")
        router = _ensure_router(getattr(state, "router", None), fallback_question=state.messages[-1].content if state.messages else "")
        state.router = router
        system_prompt = GET_ADDITIONAL_SYSTEM_PROMPT.format(
            logic=router.logic
        )
        messages = [{"role": "system", "content": system_prompt}] + state.messages
        response = await model.ainvoke(messages)
        trace_event(
            trace,
            "additional_query",
            "done",
            success=True,
            route="additional-query",
            question=question_text,
            latency_ms=elapsed_ms(started_at),
            metadata={"decision": "proceed", "answer_length": len(str(getattr(response, "content", "")))},
        )
        attach_trace_to_message(response, trace)
        return {"messages": [response]}


async def create_file_query(
        state: AgentState, *, config: RunnableConfig
) -> Dict[str, List[BaseMessage]]:
    """Ingest an uploaded CSV/Excel dataset through the PGVector ingestion service.

    Phase 5 intentionally narrows the old generic file-QA path to tabular dataset
    ingestion only.  Document-style uploads no longer initialize the legacy
    ``KnowledgeService``/Milvus stack.
    """

    logger.info("-----Found User Dataset Upload-----")
    trace = get_trace(state, config)
    user_question = state.messages[-1].content if state.messages else ""
    config_opts = _extract_configurable(config)
    upload = state.uploaded_file or {}
    from multisource_data_agent.infrastructure.core.uploads import controlled_path
    file_path = str(controlled_path(upload["storage_name"])) if upload.get("storage_name") else None
    ingest_service_url = settings.INGEST_SERVICE_URL
    trace_event(
        trace,
        "file_query",
        "start",
        route="file-query",
        question=user_question,
        metadata={"file_path_present": bool(file_path)},
    )

    if not file_path:
        logger.warning("User Upload File Path is None")
        message = AIMessage(content="请上传 CSV、XLSX 或 XLS 数据集后再进行导入。")
        trace_event(trace, "file_query", "done", route="file-query", success=False, metadata={"reason": "missing_file_path"})
        attach_trace_to_message(message, trace)
        return {"messages": [message]}

    p = Path(file_path)
    if not p.exists() or not p.is_file():
        logger.warning("User Upload File Not Found: {}", file_path)
        message = AIMessage(content="抱歉，未找到该数据集文件，请重新上传。")
        trace_event(trace, "file_query", "done", route="file-query", success=False, metadata={"reason": "file_not_found"})
        attach_trace_to_message(message, trace)
        return {"messages": [message]}

    suffix = p.suffix.lower()
    supported_suffixes = {".csv", ".xlsx", ".xls"}
    if suffix not in supported_suffixes:
        message = AIMessage(content="当前数据集导入仅支持 CSV、XLSX 和 XLS 文件。")
        trace_event(
            trace,
            "file_query",
            "done",
            route="file-query",
            success=False,
            metadata={"reason": "unsupported_dataset_type", "suffix": suffix},
        )
        attach_trace_to_message(message, trace)
        return {"messages": [message]}

    if not ingest_service_url:
        message = AIMessage(content="未配置数据摄取服务 INGEST_SERVICE_URL，暂时无法导入数据集。")
        trace_event(trace, "file_query", "done", route="file-query", success=False, metadata={"reason": "ingest_service_unconfigured"})
        attach_trace_to_message(message, trace)
        return {"messages": [message]}

    try:
        size_bytes = p.stat().st_size
        if size_bytes > settings.FILE_UPLOAD_MAX_MB * 1024 * 1024:
            message = AIMessage(content=f"文件过大（>{settings.FILE_UPLOAD_MAX_MB}MB），请拆分数据集后重新上传。")
            trace_event(trace, "file_query", "done", route="file-query", success=False, metadata={"reason": "file_too_large", "size_bytes": size_bytes})
            attach_trace_to_message(message, trace)
            return {"messages": [message]}

        incremental_flag = _coerce_to_bool(
            config_opts.get("incremental"),
            default=settings.INGEST_INCREMENTAL_DEFAULT,
        )

        form = aiohttp.FormData()
        form.add_field("incremental", "true" if incremental_flag else "false")
        with p.open("rb") as file_handle:
            form.add_field(
                "file",
                file_handle,
                filename=upload.get("original_name", p.name),
                content_type="application/octet-stream",
            )
            timeout = aiohttp.ClientTimeout(total=60)
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.post(
                    f"{ingest_service_url.rstrip('/')}/api/v1/knowledge/ingest/dataset/upload",
                    data=form,
                    headers={"X-Knowledge-Service-Key": settings.KNOWLEDGE_SERVICE_TOKEN or ""},
                ) as resp:
                    body = await resp.text()
                    if resp.status not in (200, 202):
                        logger.error("Dataset ingestion request failed: status={} body={}", resp.status, body)
                        message = AIMessage(content=f"数据集导入请求失败（{resp.status}），请稍后重试。")
                        trace_event(
                            trace,
                            "file_query",
                            "done",
                            route="file-query",
                            success=False,
                            metadata={"reason": "ingest_request_failed", "status_code": resp.status},
                        )
                        attach_trace_to_message(message, trace)
                        return {"messages": [message]}

        dataset_type = "csv" if suffix == ".csv" else "excel"
        mode = "增量" if incremental_flag else "全量"
        message = AIMessage(
            content=f"已提交 {p.name} 数据集导入任务（{mode}模式）。处理完成后，可通过 Data Agent 检索相关内容。"
        )
        trace_event(
            trace,
            "file_query",
            "done",
            route="file-query",
            success=True,
            metadata={
                "dataset_type": dataset_type,
                "incremental": incremental_flag,
                "size_bytes": size_bytes,
                "backend": "pgvector",
            },
        )
        attach_trace_to_message(message, trace)
        return {"messages": [message]}
    except Exception as exc:
        logger.exception("Failed to ingest uploaded dataset: {}", exc)
        message = AIMessage(content="数据集导入出现异常，请稍后再试或联系管理员。")
        trace_event(trace, "file_query", "done", route="file-query", success=False, metadata={"reason": "ingest_exception"})
        attach_trace_to_message(message, trace)
        return {"messages": [message]}

# 图工具 查询节点
async def create_research_plan(
        state: AgentState, *, config: RunnableConfig
) -> Dict[str, List[str] | str]:
    """Execute the unified Data Agent plan across SQL, RAG and Graph tools.

    Args:
        state (AgentState): 当前代理状态，包括对话历史。
        config (RunnableConfig): 用于配置计划生成的模型。

    Returns:
        Dict[str, List[str] | str]: 包含'steps'键的字典，其中包含研究步骤列表。
    """
    logger.info("------execute unified Data Agent query------")
    trace = get_trace(state, config)
    started_at = time.perf_counter()

    # 使用大模型生成查询/多跳、并行查询计划
    if not settings.OPENAI_API_KEY:
        raise RuntimeError("OPENAI_API_KEY is not configured for research plan generation.")

    model = ChatOpenAI(callbacks=[LLMObservationCallback()],
        openai_api_key=settings.OPENAI_API_KEY,
        openai_api_base=settings.OPENAI_API_BASE,
        model_name=settings.OPENAI_MODEL,
        temperature=0.7,
        tags=["research_plan"],
    )

    # Top-level routing no longer selects a physical data source.  The unified
    # executor lazily creates Neo4j/retriever resources only if Planner + Tool
    # Selection actually dispatches graph_tool.
    last_message = state.messages[-1].content if state.messages else ""
    route_type = normalize_route_type(_ensure_router(getattr(state, "router", None)).type)
    neo4j_graph = None
    cypher_retriever = None

    # Phase 2 canonical selector exposes exactly SQL / RAG / Graph. Legacy tool
    # names remain compatibility aliases below the orchestration layer only.
    from multisource_data_agent.application.agents.kg_sub_graph.agentic_rag_agents.components.tool_selection import (
        get_core_tool_schemas,
    )

    tool_schemas: List[type[BaseModel]] = get_core_tool_schemas()

    # 预定义的 Cypher 查询覆盖默认 Enterprise Retail 场景，同时保留历史模板供回归使用
    from multisource_data_agent.application.agents.kg_sub_graph.agentic_rag_agents.components.predefined_cypher.cypher_dict import \
        predefined_cypher_dict

    # 定义 Enterprise Retail Data Agent 服务范围
    scope_description = """
    Enterprise Retail Data Agent 服务范围：为企业零售演示数据提供可解释的数据查询和文档检索，包括但不限于：

    📦 结构化业务数据
    - 客户、商品、订单、订单明细和退款查询
    - 计数、聚合、过滤、排序、销售和退款率分析

    🔗 企业实体关系
    - 商品、供应商、商品分类和活动之间的关系与路径
    - 供应商供货商品、商品所属分类和活动关联

    📚 企业文档知识
    - 退货政策、退款异常 SOP、活动规则和供应商 SLA
    - 从知识库返回可引用的政策和流程证据

    暂不支持：政治、娱乐八卦、新闻时事、天气预报、医疗诊断等非企业零售数据内容。
    """

    # 创建多工具工作流
    multi_tool_workflow = create_multi_tool_workflow(
        llm=model,
        graph=neo4j_graph,
        tool_schemas=tool_schemas,
        predefined_cypher_dict=predefined_cypher_dict,
        cypher_example_retriever=cypher_retriever,
        scope_description=scope_description,
        llm_cypher_validation=True,
    )

    # return multi_tool_workflow
    # 准备输入状态
    trace_event(trace, "data_query", "start", route=route_type, question=last_message)
    input_state = {
        "question": last_message,
        "data": [],
        "history": [],
        "route_type": route_type,
        "eval_trace": trace,
    }

    logger.info(
    "[TRACE] enter create_research_plan",
    extra={
        "question": last_message,
        "route_type": input_state.get("route_type"),
    },
    )

    # 执行工作流
    response = await multi_tool_workflow.ainvoke(input_state)
    canonical_tool_results = normalize_tool_results(response.get("tool_results") or [])
    canonical_execution_summary = execution_summary(canonical_tool_results, route="data-query")
    if isinstance(response.get("eval_trace"), dict):
        trace = merge_trace(trace, response.get("eval_trace"))
    logger.info(
        "[GRAPH_RAG_TRACE] workflow_completed | question=%s | steps=%s | answer_len=%s | history_items=%s",
        last_message,
        response.get("steps", []),
        len(str(response.get("answer", ""))),
        len(response.get("history", []) or []),
    )
    graph_trace = {
        "trace_id": trace.get("trace_id") if trace else None,
        "workflow": "data-query",
        "question": last_message,
        "steps": response.get("steps", []),
        "history": response.get("history", []),
        "cyphers": response.get("cyphers", []),
        "tool_results": [
            item.model_dump(mode="json") if hasattr(item, "model_dump") else item
            for item in canonical_tool_results
        ],
        "events": (trace or {}).get("events", []),
        "selected_tools": (trace or {}).get("selected_tools", []),
        "fallback": (trace or {}).get("fallback", False),
    }
    ai_message = AIMessage(content=response["answer"])
    trace_event(
        trace,
        "data_query",
        "done",
        success=trace_execution_success(trace) is not False,
        route=route_type,
        question=last_message,
        latency_ms=elapsed_ms(started_at),
        metadata={
            "steps": response.get("steps", []),
            "tool_result_count": len(canonical_tool_results),
            "answer_length": len(str(response.get("answer", ""))),
        },
    )
    graph_trace["events"] = (trace or {}).get("events", [])
    graph_trace["selected_tools"] = (trace or {}).get("selected_tools", [])
    ai_message.additional_kwargs["eval_trace"] = graph_trace
    return {
        "messages": [ai_message],
        "tasks": response.get("tasks", []) or [],
        "tool_results": canonical_tool_results,
        "evidence": [item for result in canonical_tool_results for item in result.evidence],
        "citation_ids": response.get("citation_ids", []) or [],
        "execution_summary": canonical_execution_summary,
        "steps": response.get("steps", []) or [],
    }


async def check_hallucinations(
        state: AgentState, *, config: RunnableConfig
) -> dict[str, Any]:
    """Analyze the user's query and checks if the response is supported by the set of facts based on the document retrieved,
    providing a binary score result.

    This function uses a language model to analyze the user's query and gives a binary score result.

    Args:
        state (AgentState): The current state of the agent, including conversation history.
        config (RunnableConfig): Configuration with the model used for query analysis.

    Returns:
        dict[str, Router]: A dictionary containing the 'router' key with the classification result (classification type and logic).
    """
    if not settings.OPENAI_API_KEY:
        raise RuntimeError("OPENAI_API_KEY is not configured for hallucination checks.")

    model = ChatOpenAI(callbacks=[LLMObservationCallback()],
        openai_api_key=settings.OPENAI_API_KEY,
        openai_api_base=settings.OPENAI_API_BASE,
        model_name=settings.OPENAI_MODEL,
        temperature=0.7,
        tags=["hallucinations"],
    )

    system_prompt = CHECK_HALLUCINATIONS.format(
        documents=state.documents,
        generation=state.messages[-1]
    )

    messages = [
                   {"role": "system", "content": system_prompt}
               ] + state.messages

    logger.info("---CHECK HALLUCINATIONS---")

    response = cast(GradeHallucinations, await model.with_structured_output(GradeHallucinations).ainvoke(messages))

    return {"hallucination": response}


checkpointer = MemorySaver()

# 定义状态图
builder = StateGraph(AgentState, input=InputState)
# 添加节点
builder.add_node(analyze_and_route_query) # 意图识别
builder.add_node(respond_to_general_query)#默认回复
builder.add_node(get_additional_info) # 图结构信息
builder.add_node("create_research_plan", create_research_plan)  # unified SQL/RAG/Graph Data Agent
builder.add_node(create_file_query)


# 添加边
builder.add_edge(START, "analyze_and_route_query")
builder.add_conditional_edges("analyze_and_route_query", route_query)

graph = builder.compile(checkpointer=checkpointer)

# png_bytes = graph.get_graph().draw_mermaid_png()
# output_path = Path(__file__).resolve().parent / "lg_builder_workflow.png"
# output_path.write_bytes(png_bytes)
# logger.info("工作流图已保存到 %s", output_path)
#
# try:
#     from IPython.display import Image as IPythonImage, display as ipython_display
# except ImportError:  # pragma: no cover - optional dependency
#     logger.info("IPython 未安装，跳过图像内联展示。")
# else:
#     ipython_display(IPythonImage(png_bytes))

def build_supervisor_graph():
    """向后兼容的 Supervisor Graph 构建接口。"""
    return graph


async def safety_guardrails(
    state: AgentState, *, config: RunnableConfig
) -> Dict[str, List[BaseMessage]]:
    """兼容旧接口，复用 get_additional_info 的护栏逻辑。"""
    return await get_additional_info(state, config=config)
