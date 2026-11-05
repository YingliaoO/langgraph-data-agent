"""
Legacy aggregate for application models.

This module now proxies to the new `multisource_data_agent.infrastructure.persistence.db.models` (SQLAlchemy) and
`multisource_data_agent.interfaces.http.models` (Pydantic) packages. Prefer importing from those packages
directly to make intent explicit.
"""
from warnings import warn

from multisource_data_agent.interfaces.http.models.chat import ChatRequest, ChatResponse
from multisource_data_agent.interfaces.http.models.chat_message import (
    ChatMessageBase,
    ChatMessageCreate,
    ChatMessageResponse,
    ChatMessageUpdate,
    ChatSessionSnapshotBase,
    ChatSessionSnapshotCreate,
    ChatSessionSnapshotResponse,
)
from multisource_data_agent.interfaces.http.models.chat_session import ChatSessionCreate, ChatSessionResponse, ChatSessionUpdate
from multisource_data_agent.infrastructure.persistence.db.models import (
    ChatMessage,
    ChatSession,
    ChatSessionSnapshot,
    Conversation,
    ConversationHistorySnapshot,
    DialogueType,
    Message,
    User,
)

warn(
    "Importing from `multisource_data_agent.models` is deprecated. "
    "Use `multisource_data_agent.infrastructure.persistence.db.models` for database entities and "
    "`multisource_data_agent.interfaces.http.models` for Pydantic schemas instead.",
    DeprecationWarning,
    stacklevel=2,
)

__all__ = [
    # Pydantic schemas (legacy re-export)
    "ChatRequest",
    "ChatResponse",
    "ChatMessageBase",
    "ChatMessageCreate",
    "ChatMessageUpdate",
    "ChatMessageResponse",
    "ChatSessionSnapshotBase",
    "ChatSessionSnapshotCreate",
    "ChatSessionSnapshotResponse",
    "ChatSessionCreate",
    "ChatSessionUpdate",
    "ChatSessionResponse",
    # SQLAlchemy models
    "ChatMessage",
    "ChatSession",
    "ChatSessionSnapshot",
    "Conversation",
    "ConversationHistorySnapshot",
    "DialogueType",
    "Message",
    "User",
]
