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
from multisource_data_agent.infrastructure.core.database import get_db
from multisource_data_agent.infrastructure.persistence.crud import chat_message, chat_session
from multisource_data_agent.interfaces.http.models.chat_message import ChatMessageCreate, ChatMessageResponse
from multisource_data_agent.interfaces.http.models.chat_session import ChatSessionCreate, ChatSessionResponse
from sqlalchemy.orm import Session

router = APIRouter()


class ChatRequest(BaseModel):
    """Chat request model"""
    message: str = Field(..., description="User message", min_length=1, max_length=5000)
    session_id: Optional[str] = Field(None, description="Session ID for conversation continuity")
    user_id: Optional[str] = Field("default_user", description="User identifier")
    stream: bool = Field(False, description="Enable streaming response")
    file_path: Optional[str] = Field(None, description="Path to uploaded file")
    trace_id: Optional[str] = Field(None, description="Optional request trace id")
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
    type: str = Field(..., description="Chunk type: 'message', 'metadata', 'error', 'done'")
    content: Optional[str] = None
    metadata: Optional[Dict[str, Any]] = None
    session_id: Optional[str] = None
    route: Optional[str] = None


def get_or_create_session(db: Session, session_id: Optional[str], user_id: str) -> str:
    """Get existing session or create new one"""
    if session_id:
        session = chat_session.get(db, id=session_id)
        if session:
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
        logger.error(f"Failed to save message: {e}")
        return None


async def process_agent_query(message: str, session_id: str,
                            file_path: Optional[str] = None,
                            ingest_incremental: Optional[bool] = None,
                            user_id: Optional[str] = None,
                            trace_id: Optional[str] = None) -> Dict[str, Any]:
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
            "file_path": file_path,
            "incremental": incremental_flag,
            "trace_id": eval_trace["trace_id"],
            "eval_trace": eval_trace,
        }
    }

    input_state = {
        "messages": [{"type": "human", "content": message}],
        "eval_trace": eval_trace,
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

        return {
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
        }
    except asyncio.TimeoutError as e:
        logger.error("Agent query exceeded timeout budget", exc_info=True)
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
        return {
            "message": "抱歉，请求处理超时，请稍后重试。",
            "route": "error",
            "route_logic": "Error: agent timeout",
            "sources": [],
            "metadata": {
                "error": "agent timeout",
                "trace_id": eval_trace.get("trace_id"),
                "eval_trace": eval_trace,
            },
            "execution": build_execution_dto(
                trace=eval_trace, route="error"
            ).model_dump(mode="json"),
        }
    except Exception as e:
        logger.error("Agent query failed: {}", e, exc_info=True)
        trace_event(eval_trace, "agent_graph", "error", success=False, error=e, fallback=True)
        finish_trace(eval_trace, route="error", error=str(e))
        return {
            "message": "抱歉，处理您的请求时出现了错误。请稍后重试。",
            "route": "error",
            "route_logic": f"Error: {str(e)}",
            "sources": [],
            "metadata": {"error": str(e), "trace_id": eval_trace.get("trace_id"), "eval_trace": eval_trace},
            "execution": build_execution_dto(trace=eval_trace, route="error").model_dump(mode="json"),
        }


async def stream_agent_response(message: str, session_id: str,
                               file_path: Optional[str] = None,
                               ingest_incremental: Optional[bool] = None,
                               user_id: Optional[str] = None,
                               trace_id: Optional[str] = None) -> AsyncGenerator[str, None]:
    """Stream agent response"""
    trace_id = trace_id or str(uuid.uuid4())
    # Send initial metadata
    metadata_chunk = ChatStreamChunk(
        type="metadata",
        metadata={"status": "processing", "trace_id": trace_id},
        session_id=session_id
    )
    yield f"data: {metadata_chunk.model_dump_json()}\n\n"

    try:
        # Process the query
        result = await process_agent_query(
            message,
            session_id,
            file_path,
            ingest_incremental,
            user_id=user_id,
            trace_id=trace_id,
        )

        # Send route information
        route_chunk = ChatStreamChunk(
            type="metadata",
            metadata={
                "route": result["route"],
                "logic": result["route_logic"],
                "trace_id": result.get("metadata", {}).get("trace_id"),
            },
            session_id=session_id,
            route=result["route"]
        )
        yield f"data: {route_chunk.model_dump_json()}\n\n"

        # Stream the response message (simulated - in real implementation, you'd stream from LLM)
        response_text = result["message"]
        words = response_text.split()
        current_text = ""

        for word in words:
            current_text += word + " "
            message_chunk = ChatStreamChunk(
                type="message",
                content=word + " ",
                session_id=session_id
            )
            yield f"data: {message_chunk.model_dump_json()}\n\n"
            await asyncio.sleep(0.05)  # Simulate streaming delay

        # Send done signal
        done_chunk = ChatStreamChunk(
            type="done",
            metadata={
                "sources": result.get("sources", []),
                "trace_id": result.get("metadata", {}).get("trace_id"),
                "eval_trace": result.get("metadata", {}).get("eval_trace"),
                "execution": result.get("execution"),
            },
            session_id=session_id
        )
        yield f"data: {done_chunk.model_dump_json()}\n\n"

    except Exception as e:
        # Send error
        error_chunk = ChatStreamChunk(
            type="error",
            content=f"处理请求时出错: {str(e)}",
            session_id=session_id
        )
        yield f"data: {error_chunk.model_dump_json()}\n\n"


@router.post("/", response_model=ChatResponse)
async def chat(
    request: ChatRequest,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db)
) -> ChatResponse:
    """
    Unified chat endpoint with automatic agent routing

    - Automatically routes queries to appropriate agents
    - Maintains conversation history
    - Supports CSV/XLSX/XLS dataset ingestion
    """
    # Get or create session
    session_id = get_or_create_session(db, request.session_id, request.user_id)
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
        user_id=request.user_id,
        trace_id=trace_id,
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
) -> ChatResponse:
    return await chat(request, background_tasks, db)


@router.post("/stream")
async def chat_stream(
    request: ChatRequest,
    db: Session = Depends(get_db)
) -> StreamingResponse:
    """
    Streaming chat endpoint with automatic agent routing

    Returns responses in Server-Sent Events (SSE) format
    """
    # Get or create session
    session_id = get_or_create_session(db, request.session_id, request.user_id)
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

    # Return streaming response
    return StreamingResponse(
        stream_agent_response(
            request.message,
            session_id,
            request.file_path,
            effective_incremental,
            user_id=request.user_id,
            trace_id=trace_id,
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
) -> StreamingResponse:
    return await chat_stream(request, db)


@router.get("/chat/stream", include_in_schema=False)
async def chat_stream_legacy_get(
    message: str = Query(..., min_length=1, max_length=5000),
    session_id: Optional[str] = Query(None),
    user_id: Optional[str] = Query("default_user"),
    file_path: Optional[str] = Query(None),
    trace_id: Optional[str] = Query(None),
    ingest_incremental: Optional[bool] = Query(None),
    db: Session = Depends(get_db),
) -> StreamingResponse:
    request = ChatRequest(
        message=message,
        session_id=session_id,
        user_id=user_id,
        stream=True,
        file_path=file_path,
        trace_id=trace_id,
        ingest_incremental=ingest_incremental,
    )
    return await chat_stream(request, db)


@router.get("/history/{session_id}", response_model=List[ChatMessageResponse])
async def get_chat_history(
    session_id: str,
    db: Session = Depends(get_db),
    limit: int = Query(50, le=100),
    offset: int = Query(0, ge=0)
) -> List[ChatMessageResponse]:
    """
    Get chat history for a session
    """
    # Verify session exists
    session = chat_session.get(db, id=session_id)
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
    db: Session = Depends(get_db)
) -> Dict[str, str]:
    """
    Clear all messages in a session
    """
    session = chat_session.get(db, id=session_id)
    if not session:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Session not found"
        )

    # Delete all messages in session
    chat_message.delete_by_session(db, session_id=session_id)

    return {"message": "Session cleared successfully", "session_id": session_id}


@router.get("/routes")
async def get_route_info() -> Dict[str, Any]:
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
                "examples": ["按菜系统计菜谱数量", "宫保鸡丁的历史", "查询菜品和食材关系"]
            },
            "file-query": {
                "name": "数据集导入",
                "description": "导入上传的 CSV/XLSX/XLS 表格数据集",
                "examples": ["导入这个 Excel 数据集", "导入这个 CSV 文件"]
            }
        },
        "auto_routing": "系统会根据您的问题自动选择合适的处理方式"
    }
