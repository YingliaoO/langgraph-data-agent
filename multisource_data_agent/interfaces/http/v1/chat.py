"""
Unified Chat API with Agent Integration

Provides a single endpoint for chat interactions with automatic routing.
"""
import asyncio
import json
import uuid
from typing import Any, Dict, List, Optional, Union, AsyncGenerator
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, BackgroundTasks, Query, status
from fastapi.responses import StreamingResponse
from loguru import logger
from pydantic import BaseModel, Field

from multisource_data_agent.application.agents.lg_builder import graph
from multisource_data_agent.application.agents.execution_contract import (
    ExecutionDTO,
    build_execution_dto,
    normalize_tool_results,
)
from multisource_data_agent.application.agents.observability import (
    create_trace,
    finish_trace,
    merge_trace,
    request_trace_scope,
    trace_event,
)
from multisource_data_agent.config import settings
from multisource_data_agent.infrastructure.core.database import get_db, SessionLocal
from multisource_data_agent.infrastructure.persistence.crud import chat_message, chat_session
from multisource_data_agent.interfaces.http.models.chat_message import ChatMessageCreate, ChatMessageResponse
from multisource_data_agent.interfaces.http.models.chat_session import ChatSessionCreate, ChatSessionResponse
from sqlalchemy.orm import Session

from multisource_data_agent.interfaces.http.security import (
    require_user, assert_claimed_user, owned_session, safe_error, public_error_details,
)
from multisource_data_agent.infrastructure.core.uploads import resolve_upload

router = APIRouter()


class ChatRequest(BaseModel):
    """Chat request model"""
    message: str = Field(..., description="User message", min_length=1, max_length=5000)
    session_id: Optional[str] = Field(None, description="Session ID for conversation continuity")
    user_id: Optional[str] = Field(None, description="Deprecated claim; must equal authenticated JWT sub")
    stream: bool = Field(False, description="Compatibility field; use /chat/stream for progress/final SSE")
    file_id: Optional[str] = Field(None, description="ID returned by authenticated upload")
    file_path: Optional[str] = Field(None, description="Deprecated; raw server paths are rejected")
    trace_id: Optional[str] = Field(None, pattern=r"^[A-Za-z0-9_-]{1,128}$", description="Optional request trace id")
    ingest_incremental: Optional[bool] = Field(
        None,
        description="Override whether CSV/Excel dataset ingestion uses incremental mode (defaults to server setting)",
    )


class ChatResponse(BaseModel):
    """Chat response model"""
    message: str
    session_id: str
    message_id: str
    route: Optional[str] = None
    route_logic: Optional[str] = None
    sources: Optional[List[Dict[str, Any]]] = None
    metadata: Optional[Dict[str, Any]] = None
    execution: Optional[ExecutionDTO] = None
    timestamp: datetime = Field(default_factory=datetime.now)


class ChatStreamChunk(BaseModel):
    """Streaming response chunk"""
    type: str = Field(..., description="Event type: progress, final, error, done (not token streaming)")
    content: Optional[str] = None
    metadata: Optional[Dict[str, Any]] = None
    session_id: Optional[str] = None
    route: Optional[str] = None


def get_or_create_session(db: Session, session_id: Optional[str], user_id: str) -> str:
    """Get existing session or create new one"""
    if session_id:
        owned_session(db, session_id, user_id)
        return session_id

    # Create new session
    new_session_id = str(uuid.uuid4())
    session_data = ChatSessionCreate(
        id=new_session_id,
        user_id=user_id,
        title=f"Chat {datetime.now().strftime('%Y-%m-%d %H:%M')}"
    )
    chat_session.create(db, obj_in=session_data)
    return new_session_id


def _serialize_tool_results(value: Any) -> List[Dict[str, Any]]:
    """Serialize canonical ToolResult objects for HTTP metadata consumers."""
    if not isinstance(value, list):
        return []
    serialized: List[Dict[str, Any]] = []
    for item in value:
        model_dump = getattr(item, "model_dump", None)
        if callable(model_dump):
            serialized.append(model_dump(mode="json"))
        elif isinstance(item, dict):
            serialized.append(item)
    return serialized


async def save_message(db: Session, session_id: str, message: str, is_user: bool,
                       route: Optional[str] = None, metadata: Optional[Dict] = None):
    """Save message to database"""
    try:
        last_message = chat_message.get_latest_by_session(db, session_id=session_id)
        next_order_index = (last_message.order_index + 1) if last_message else 1

        message_metadata: Dict[str, Any] = {}
        if isinstance(metadata, dict):
            # Avoid persisting huge/unstable objects (e.g. full agent graph state).
            message_metadata.update({k: v for k, v in metadata.items() if k != "agent_state"})
        elif metadata is not None:
            message_metadata["metadata"] = str(metadata)

        if route:
            message_metadata["route"] = route

        message_data = ChatMessageCreate(
            session_id=session_id,
            message_type="user_query" if is_user else "agent_response",
            content=message,
            message_metadata=message_metadata or None,
            order_index=next_order_index,
        )
        created = chat_message.create(db, obj_in=message_data)

        # Update session activity timestamp so session list ordering stays correct.
        chat_session.update_activity(db, session_id=session_id)
        return str(created.id)
    except Exception as e:
        logger.opt(exception=e).error("Failed to persist chat message")
        raise safe_error(500, "message_persistence_failed", "The message could not be saved", "persistence") from None


async def process_agent_query(message: str, session_id: str,
                            file_path: Optional[str] = None,
                            ingest_incremental: Optional[bool] = None,
                            user_id: Optional[str] = None,
                            trace_id: Optional[str] = None,
                            uploaded_file: Optional[Dict[str, str]] = None) -> Dict[str, Any]:
    """Process query through agent system"""
    incremental_flag = (
        settings.INGEST_INCREMENTAL_DEFAULT if ingest_incremental is None else bool(ingest_incremental)
    )
    eval_trace = create_trace(trace_id=trace_id, session_id=session_id, user_id=user_id)
    trace_event(
        eval_trace,
        "http",
        "request_received",
        question=message,
        metadata={
            "session_id": session_id,
            "file_path_present": bool(file_path),
            "incremental": incremental_flag,
        },
    )
    config = {
        "configurable": {
            "thread_id": session_id,
            "incremental": incremental_flag,
            "trace_id": eval_trace["trace_id"],
            "eval_trace": eval_trace,
        }
    }

    input_state = {
        "messages": [{"type": "human", "content": message}],
        "eval_trace": eval_trace,
        "uploaded_file": uploaded_file,
    }

    try:
        # Invoke agent graph
        trace_event(eval_trace, "agent_graph", "start", question=message)
        with request_trace_scope(eval_trace):
            result = await asyncio.wait_for(
                graph.ainvoke(input_state, config=config),
                timeout=float(settings.AGENT_TIMEOUT),
            )

        # Extract response and metadata
        response_text = ""
        if result.get("messages"):
            response_text = result["messages"][-1].content

        # Extract route information
        router_info = result.get("router", {})
        route = router_info.get("type")
        route_logic = router_info.get("logic")

        # Extract sources if available
        sources_raw = result.get("sources", [])
        if result.get("messages"):
            last_agent_message = result["messages"][-1]
            additional_kwargs = getattr(last_agent_message, "additional_kwargs", {}) or {}
            trace_payload = additional_kwargs.get("eval_trace")
            if isinstance(trace_payload, dict):
                eval_trace = merge_trace(eval_trace, trace_payload)

        # Convert sources to expected format (list of dicts)
        sources = []
        if sources_raw:
            # If sources is a list of strings, convert to list of dicts
            if isinstance(sources_raw[0], str):
                for src in sources_raw:
                    sources.append({"document_id": src, "source": src})
            else:
                # Already in correct format
                sources = sources_raw

        trace_event(
            eval_trace,
            "agent_graph",
            "done",
            success=True,
            route=route,
            metadata={
                "route_logic": route_logic,
                "source_count": len(sources),
                "answer_length": len(response_text),
            },
        )
        finish_trace(eval_trace, route=route)
        canonical_tool_results = normalize_tool_results(result.get("tool_results") or [])
        execution = build_execution_dto(
            tasks=result.get("tasks") or [],
            tool_results=canonical_tool_results,
            citation_ids=result.get("citation_ids") or [],
            execution_summary_value=result.get("execution_summary"),
            trace=eval_trace,
            route=route,
        )

        if execution.status == "error":
            response_text = "未能完成数据查询，请检查请求或稍后重试。"
        return public_error_details({
            "message": response_text,
            "route": route,
            "route_logic": route_logic,
            "sources": sources,
            "metadata": {
                "session_id": session_id,
                "trace_id": eval_trace.get("trace_id"),
                "eval_trace": eval_trace,
            },
            "execution": execution.model_dump(mode="json"),
        })
    except asyncio.TimeoutError as e:
        logger.opt(exception=e).error("Agent query exceeded timeout budget")
        trace_event(
            eval_trace,
            "agent_graph",
            "error",
            success=False,
            error=e,
            error_code="agent_timeout",
            fallback=False,
            metadata={
                "timeout_budget_ms": round(float(settings.AGENT_TIMEOUT) * 1000, 2)
            },
        )
        finish_trace(eval_trace, route="error", error="agent timeout")
        return public_error_details({
            "message": "抱歉，请求处理超时，请稍后重试。",
            "route": "error",
            "route_logic": "Error: agent timeout",
            "sources": [],
            "metadata": {
                "error": "agent timeout",
                "error_code": "agent_timeout",
                "error_category": "timeout",
                "trace_id": eval_trace.get("trace_id"),
                "eval_trace": eval_trace,
            },
            "execution": build_execution_dto(
                trace=eval_trace, route="error"
            ).model_dump(mode="json"),
        })
    except Exception as e:
        logger.opt(exception=e).error("Agent query failed")
        trace_event(eval_trace, "agent_graph", "error", success=False, error=e, error_code="agent_execution_failed", fallback=True)
        finish_trace(eval_trace, route="error", error=str(e))
        return public_error_details({
            "message": "抱歉，处理您的请求时出现了错误。请稍后重试。",
            "route": "error",
            "route_logic": "agent_execution_failed",
            "sources": [],
            "metadata": {"error": str(e), "error_code": "agent_execution_failed", "error_category": "execution", "trace_id": eval_trace.get("trace_id"), "eval_trace": eval_trace},
            "execution": build_execution_dto(trace=eval_trace, route="error").model_dump(mode="json"),
        })


async def stream_agent_response(message: str, session_id: str,
                               file_path: Optional[str] = None,
                               ingest_incremental: Optional[bool] = None,
                               user_id: Optional[str] = None,
                               trace_id: Optional[str] = None,
                               uploaded_file: Optional[Dict[str, str]] = None,
                               on_complete=None) -> AsyncGenerator[str, None]:
    """Progress and final-result SSE. This is not model-token streaming."""
    trace_id = trace_id or str(uuid.uuid4())

    def event(kind, content=None, metadata=None):
        chunk = ChatStreamChunk(type=kind, content=content, metadata=metadata,
                                session_id=session_id)
        return f"event: {kind}\ndata: {chunk.model_dump_json()}\n\n"

    try:
        yield event("progress", metadata={"status": "processing", "trace_id": trace_id})
        result = await process_agent_query(
            message, session_id, ingest_incremental=ingest_incremental,
            user_id=user_id, trace_id=trace_id, uploaded_file=uploaded_file,
        )
        if on_complete is not None:
            await on_complete(result)
        metadata = public_error_details({
            "route": result.get("route"), "sources": result.get("sources", []),
            "execution": result.get("execution"), "trace_id": trace_id,
        })
        if result.get("route") == "error":
            yield event("error", content=result["message"], metadata={
                **metadata, "code": "agent_execution_failed", "category": "execution",
            })
        else:
            yield event("final", content=result["message"], metadata=metadata)
    except (asyncio.CancelledError, GeneratorExit):
        logger.info("SSE client disconnected, trace_id={}", trace_id)
        raise
    except Exception as exc:
        logger.opt(exception=exc).error("SSE request failed, trace_id={}", trace_id)
        yield event("error", "The request could not be completed", {
            "code": "stream_failed", "category": "execution", "trace_id": trace_id,
        })
    yield event("done", metadata={"trace_id": trace_id})


def trusted_upload(request: ChatRequest, db: Session, subject: str):
    assert_claimed_user(request.user_id, subject)
    if request.file_path:
        raise safe_error(400, "file_id_required", "Send file_id from the upload response; server paths are not accepted")
    if not request.file_id:
        return None
    record = resolve_upload(db, request.file_id, subject)
    return {"file_id": record.id, "storage_name": record.storage_name,
            "original_name": record.original_name, "user_id": subject}


@router.post("/", response_model=ChatResponse)
async def chat(
    request: ChatRequest,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
    subject: str = Depends(require_user),
) -> ChatResponse:
    """
    Unified chat endpoint with automatic agent routing

    - Automatically routes queries to appropriate agents
    - Maintains conversation history
    - Supports CSV/XLSX/XLS dataset ingestion
    """
    upload = trusted_upload(request, db, subject)
    session_id = get_or_create_session(db, request.session_id, subject)
    trace_id = request.trace_id or str(uuid.uuid4())

    # Save user message
    await save_message(
        db,
        session_id,
        request.message,
        is_user=True,
        metadata={"trace_id": trace_id},
    )

    # Process through agent
    effective_incremental = (
        request.ingest_incremental
        if request.ingest_incremental is not None
        else settings.INGEST_INCREMENTAL_DEFAULT
    )

    result = await process_agent_query(
        request.message,
        session_id,
        request.file_path,
        effective_incremental,
        user_id=subject,
        trace_id=trace_id,
        uploaded_file=upload,
    )

    # Save assistant message
    message_id = await save_message(
        db,
        session_id,
        result["message"],
        is_user=False,
        route=result["route"],
        metadata=result.get("metadata"),
    )

    return ChatResponse(
        message=result["message"],
        session_id=session_id,
        message_id=message_id or str(uuid.uuid4()),
        route=result["route"],
        route_logic=result["route_logic"],
        sources=result.get("sources"),
        metadata=result.get("metadata"),
        execution=result.get("execution"),
    )


# ---------------------------------------------------------------------------
# Legacy alias routes (backwards compatibility)
#
# Older docs/scripts use `/api/v1/chat/chat` and `/api/v1/chat/chat/stream`.
# Keep them working to reduce migration friction.
# ---------------------------------------------------------------------------


@router.post("/chat", response_model=ChatResponse, include_in_schema=False)
async def chat_legacy(
    request: ChatRequest,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
    subject: str = Depends(require_user),
) -> ChatResponse:
    return await chat(request, background_tasks, db, subject)


@router.post("/stream")
async def chat_stream(
    request: ChatRequest,
    db: Session = Depends(get_db),
    subject: str = Depends(require_user),
) -> StreamingResponse:
    """
    Streaming chat endpoint with automatic agent routing

    Returns responses in Server-Sent Events (SSE) format
    """
    upload = trusted_upload(request, db, subject)
    session_id = get_or_create_session(db, request.session_id, subject)
    trace_id = request.trace_id or str(uuid.uuid4())

    # Save user message
    await save_message(
        db,
        session_id,
        request.message,
        is_user=True,
        metadata={"trace_id": trace_id},
    )

    effective_incremental = (
        request.ingest_incremental
        if request.ingest_incremental is not None
        else settings.INGEST_INCREMENTAL_DEFAULT
    )

    async def persist_final(result):
        with SessionLocal() as stream_db:
            owned_session(stream_db, session_id, subject)
            await save_message(stream_db, session_id, result["message"], is_user=False,
                               route=result.get("route"), metadata=result.get("metadata"))

    return StreamingResponse(
        stream_agent_response(
            request.message,
            session_id,
            request.file_path,
            effective_incremental,
            user_id=subject,
            trace_id=trace_id,
            uploaded_file=upload,
            on_complete=persist_final,
        ),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no"  # Disable nginx buffering
        }
    )


@router.post("/chat/stream", include_in_schema=False)
async def chat_stream_legacy_post(
    request: ChatRequest,
    db: Session = Depends(get_db),
    subject: str = Depends(require_user),
) -> StreamingResponse:
    return await chat_stream(request, db, subject)


@router.get("/chat/stream", include_in_schema=False)
async def chat_stream_legacy_get(
    message: str = Query(..., min_length=1, max_length=5000),
    session_id: Optional[str] = Query(None),
    user_id: Optional[str] = Query(None),
    file_path: Optional[str] = Query(None),
    file_id: Optional[str] = Query(None),
    trace_id: Optional[str] = Query(None),
    ingest_incremental: Optional[bool] = Query(None),
    db: Session = Depends(get_db),
    subject: str = Depends(require_user),
) -> StreamingResponse:
    request = ChatRequest(
        message=message,
        session_id=session_id,
        user_id=user_id,
        stream=True,
        file_path=file_path,
        file_id=file_id,
        trace_id=trace_id,
        ingest_incremental=ingest_incremental,
    )
    return await chat_stream(request, db, subject)


@router.get("/history/{session_id}", response_model=List[ChatMessageResponse])
async def get_chat_history(
    session_id: str,
    db: Session = Depends(get_db),
    limit: int = Query(50, le=100),
    offset: int = Query(0, ge=0),
    subject: str = Depends(require_user),
) -> List[ChatMessageResponse]:
    """
    Get chat history for a session
    """
    # Verify session exists
    session = owned_session(db, session_id, subject)
    if not session:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Session not found"
        )

    # Get messages
    messages = chat_message.get_by_session(
        db,
        session_id=session_id,
        skip=offset,
        limit=limit
    )

    return messages


@router.delete("/session/{session_id}")
async def clear_session(
    session_id: str,
    db: Session = Depends(get_db),
    subject: str = Depends(require_user),
) -> Dict[str, str]:
    """
    Clear all messages in a session
    """
    session = owned_session(db, session_id, subject)
    if not session:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Session not found"
        )

    # Delete all messages in session
    chat_message.delete_by_session(db, session_id=session_id)

    return {"message": "Session cleared successfully", "session_id": session_id}


@router.get("/routes")
async def get_route_info(subject: str = Depends(require_user)) -> Dict[str, Any]:
    """
    Get information about available routes and their purposes
    """
    return {
        "routes": {
            "general-query": {
                "name": "轻量对话",
                "description": "处理问候、感谢和超出数据产品范围的轻量问题",
                "examples": ["你好", "谢谢"]
            },
            "additional-query": {
                "name": "补充信息",
                "description": "当数据分析目标缺少关键对象或约束时补充信息",
                "examples": ["帮我分析一下", "看看这个数据"]
            },
            "data-query": {
                "name": "数据分析",
                "description": "统一进入 Planner，并在 SQL Agent、Knowledge RAG、Graph Tool 之间选择",
                "examples": ["按品类统计销售件数", "电子产品退货政策", "查询商品与供应商关系"]
            },
            "file-query": {
                "name": "数据集导入",
                "description": "导入上传的 CSV/XLSX/XLS 表格数据集",
                "examples": ["导入这个 Excel 数据集", "导入这个 CSV 文件"]
            }
        },
        "auto_routing": "系统会根据您的问题自动选择合适的处理方式"
    }
