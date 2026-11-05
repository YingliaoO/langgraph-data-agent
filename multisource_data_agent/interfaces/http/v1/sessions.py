"""
Chat session management API endpoints.

Every operation is scoped to the authenticated JWT subject.
"""
from typing import Any, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.orm import Session

from multisource_data_agent.infrastructure.core.database import get_db
from multisource_data_agent.infrastructure.persistence.crud import chat_message, chat_session, chat_session_snapshot
from multisource_data_agent.interfaces.http.models.chat_message import (
    ChatMessageCreate,
    ChatMessageResponse,
    ChatSessionSnapshotCreate,
    ChatSessionSnapshotResponse,
)
from multisource_data_agent.interfaces.http.models.chat_session import (
    ChatSessionCreate,
    ChatSessionResponse,
    ChatSessionUpdate,
)

router = APIRouter()

from multisource_data_agent.interfaces.http.security import (
    assert_claimed_user, owned_session, require_user, safe_error,
)


@router.get("/", response_model=List[ChatSessionResponse])
def get_sessions(
    *,
    db: Session = Depends(get_db),
    subject: str = Depends(require_user),
    user_id: Optional[str] = Query(None, description="Filter by user ID (device ID, UUID, etc.)"),
    skip: int = 0,
    limit: int = 50,
    active_only: bool = True,
) -> Any:
    """List only the authenticated subject's sessions; an absent filter never means all users."""
    assert_claimed_user(user_id, subject)
    if subject:
        # Get sessions for specific user
        sessions = chat_session.get_by_user(
            db,
            user_id=subject,
            skip=skip,
            limit=limit,
            active_only=active_only
        )

    return sessions


@router.post("/", response_model=ChatSessionResponse, status_code=status.HTTP_201_CREATED)
def create_session(
    *,
    db: Session = Depends(get_db),
    session_in: ChatSessionCreate,
    subject: str = Depends(require_user),
) -> Any:
    """
    Create a new chat session.

    Frontend should provide:
    - id: Session UUID
    - user_id: User identifier (device ID, UUID, etc.) - optional
    - title: Session title
    """
    assert_claimed_user(session_in.user_id, subject)
    if chat_session.get(db, id=session_in.id):
        raise safe_error(409, "session_id_unavailable", "Session ID is unavailable")
    session = chat_session.create(db, obj_in=session_in.model_copy(update={"user_id": subject}))
    return session


@router.get("/{session_id}", response_model=ChatSessionResponse)
def get_session(
    *,
    db: Session = Depends(get_db),
    session_id: str,
    subject: str = Depends(require_user),
) -> Any:
    """
    Get a specific chat session by ID.
    """
    session = owned_session(db, session_id, subject)

    if not session:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Session not found"
        )

    return session


@router.patch("/{session_id}", response_model=ChatSessionResponse)
def update_session(
    *,
    db: Session = Depends(get_db),
    session_id: str,
    session_update: ChatSessionUpdate,
    subject: str = Depends(require_user),
) -> Any:
    """
    Update a chat session.

    Can update title and is_active; ownership is immutable.
    """
    session = owned_session(db, session_id, subject, include_inactive=True)
    assert_claimed_user(session_update.user_id, subject)

    if not session:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Session not found"
        )

    session = chat_session.update(
        db, db_obj=session, obj_in=session_update.model_dump(exclude_unset=True, exclude={"user_id"})
    )
    return session


@router.delete("/{session_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_session(
    *,
    db: Session = Depends(get_db),
    session_id: str,
    subject: str = Depends(require_user),
) -> None:
    """
    Soft delete a chat session.
    """
    session = owned_session(db, session_id, subject)

    if not session:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Session not found"
        )

    chat_session.soft_delete(db, session_id=session_id)


@router.post(
    "/{session_id}/messages",
    response_model=ChatMessageResponse,
    status_code=status.HTTP_201_CREATED,
)
def add_message_to_session(
    *,
    db: Session = Depends(get_db),
    session_id: str,
    message_in: ChatMessageCreate,
    subject: str = Depends(require_user),
) -> Any:
    """
    Add a message to a chat session.
    """
    session = owned_session(db, session_id, subject)

    if not session:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Session not found"
        )

    # Ensure session_id matches
    message_in.session_id = session_id

    # Create message
    message = chat_message.create(db, obj_in=message_in)

    # Update session activity
    chat_session.update_activity(db, session_id=session_id)

    return message


@router.post(
    "/{session_id}/snapshot",
    response_model=ChatSessionSnapshotResponse,
    status_code=status.HTTP_201_CREATED,
)
def create_session_snapshot(
    *,
    db: Session = Depends(get_db),
    session_id: str,
    snapshot_in: ChatSessionSnapshotCreate,
    subject: str = Depends(require_user),
) -> Any:
    """
    Create a snapshot of a conversation turn for quick restoration.
    """
    session = owned_session(db, session_id, subject)

    if not session:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Session not found"
        )

    # Ensure session_id matches
    snapshot_in.session_id = session_id

    # Create snapshot
    snapshot = chat_session_snapshot.create(db, obj_in=snapshot_in)

    return snapshot


@router.get("/user/{user_id}/count", response_model=dict)
def get_user_session_count(
    *,
    db: Session = Depends(get_db),
    user_id: str,
    subject: str = Depends(require_user),
) -> Any:
    """
    Get the total number of sessions for a user.

    Useful for analytics and user interface.
    """
    assert_claimed_user(user_id, subject)
    count = chat_session.count_by_user(db, user_id=subject)

    return {
        "user_id": user_id,
        "session_count": count
    }
