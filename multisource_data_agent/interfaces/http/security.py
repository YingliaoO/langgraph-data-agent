"""Bearer authentication and safe, traceable public errors. No anonymous fallback."""

from contextvars import ContextVar
from uuid import uuid4

from fastapi import Depends, HTTPException
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from jose import JWTError, jwt

from multisource_data_agent.config import settings

request_trace_id: ContextVar[str] = ContextVar("request_trace_id", default="")
bearer = HTTPBearer(auto_error=False)


def safe_error(status: int, code: str, message: str, category: str = "request_error"):
    return HTTPException(
        status_code=status,
        detail={
            "code": code,
            "category": category,
            "message": message,
            "trace_id": request_trace_id.get() or str(uuid4()),
        },
        headers={"WWW-Authenticate": "Bearer"} if status == 401 else None,
    )


def require_user(credentials: HTTPAuthorizationCredentials | None = Depends(bearer)) -> str:
    if credentials is None or credentials.scheme.lower() != "bearer":
        raise safe_error(
            401, "authentication_required", "A Bearer token is required", "authentication"
        )
    if not settings.AUTH_JWT_SECRET or len(settings.AUTH_JWT_SECRET.encode()) < 32:
        raise safe_error(
            503, "authentication_unconfigured", "Authentication is not configured", "configuration"
        )
    try:
        claims = jwt.decode(
            credentials.credentials,
            settings.AUTH_JWT_SECRET,
            algorithms=["HS256"],
            issuer=settings.AUTH_JWT_ISSUER,
            audience=settings.AUTH_JWT_AUDIENCE,
            options={"require_exp": True, "require_sub": True, "require_iat": True},
        )
        subject = claims["sub"]
        if not isinstance(subject, str) or not subject.strip() or len(subject) > 255:
            raise JWTError("Invalid subject")
        if any(ord(c) < 32 for c in subject):
            raise JWTError("Invalid subject")
    except (JWTError, ValueError, TypeError, KeyError):
        raise safe_error(
            401, "invalid_token", "The token is invalid or expired", "authentication"
        ) from None
    return subject


def assert_claimed_user(claimed: str | None, subject: str) -> None:
    if claimed is not None and claimed != subject:
        raise safe_error(
            403, "identity_mismatch", "User identity cannot be overridden", "authorization"
        )


def owned_session(db, session_id: str, subject: str, *, include_inactive: bool = False):
    from multisource_data_agent.infrastructure.persistence.crud import chat_session

    session = chat_session.get_owned(
        db, session_id=session_id, user_id=subject, active_only=not include_inactive
    )
    if session is None or session.user_id != subject:
        raise safe_error(404, "session_not_found", "Session not found", "authorization")
    return session


def public_error_details(value):
    """Keep statuses/codes, removing exception text from public diagnostics."""
    if isinstance(value, dict):
        return {
            key: (
                (
                    "internal_error"
                    if isinstance(item, str)
                    else [] if isinstance(item, list) else None
                )
                if key.lower()
                in {
                    "error",
                    "errors",
                    "exception",
                    "traceback",
                    "stack",
                    "stacktrace",
                    "execution_error",
                    "validation_errors",
                }
                else public_error_details(item)
            )
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [public_error_details(item) for item in value]
    return value
