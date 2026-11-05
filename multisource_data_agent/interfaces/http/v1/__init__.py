"""
API v1 router aggregation.

Session operations require a verified JWT subject.
"""
from fastapi import APIRouter

from multisource_data_agent.interfaces.http.v1 import sessions, chat, upload

api_router = APIRouter()

# Include routers (authentication is enforced by endpoint dependencies)
api_router.include_router(sessions.router, prefix="/sessions", tags=["Chat Sessions"])
api_router.include_router(chat.router, prefix="/chat", tags=["Unified Chat"])
api_router.include_router(upload.router, prefix="/upload", tags=["File Upload"])
