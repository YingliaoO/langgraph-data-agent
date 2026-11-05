"""
SQLAlchemy ORM models used across the application.

These were previously imported from the `multisource_data_agent.models` package. That alias is
still available for backward compatibility, but new code should import from
`multisource_data_agent.infrastructure.persistence.db.models` to make the intent (database layer) explicit.
"""

from .chat_history import ConversationHistorySnapshot
from .chat_message import ChatMessage, ChatSessionSnapshot
from .chat_session import ChatSession
from .conversation import Conversation, DialogueType
from .message import Message
from .user import User

__all__ = [
    "ConversationHistorySnapshot",
    "ChatMessage",
    "ChatSessionSnapshot",
    "ChatSession",
    "Conversation",
    "DialogueType",
    "Message",
    "User",
]
