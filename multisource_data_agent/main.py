"""
Multi-Source Data Agent FastAPI application entry point.
"""

import asyncio
import os
from urllib.parse import urlsplit

import uvicorn
from fastapi import FastAPI, Request, HTTPException
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from loguru import logger

from multisource_data_agent.config import settings
from multisource_data_agent.infrastructure.core import configure_logging
from multisource_data_agent.infrastructure.core.database import Base, engine
from multisource_data_agent.interfaces.http.v1 import api_router as api_v1_router
from multisource_data_agent.interfaces.http.security import request_trace_id
from uuid import uuid4
from multisource_data_agent.interfaces.http.body_limit import RequestBodyLimit
from starlette.exceptions import HTTPException as StarletteHTTPException

# Configure logging before app creation
configure_logging(debug=settings.DEBUG)

# Application instance --------------------------------------------------------
application = FastAPI(
    title=settings.APP_NAME,
    version=settings.APP_VERSION,
    description="Multi-source Data Agent for SQL, knowledge retrieval, and graph queries",
    docs_url="/docs",
    redoc_url="/redoc",
)

# Global middleware -----------------------------------------------------------
application.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins_list,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Routers --------------------------------------------------------------------
application.add_middleware(RequestBodyLimit, limit_bytes=lambda: settings.FILE_UPLOAD_MAX_MB * 1024 * 1024)
application.include_router(api_v1_router, prefix=settings.API_V1_PREFIX)

# Uploads are served exclusively by the authenticated upload router.


@application.middleware("http")
async def trace_http_request(request: Request, call_next):
    trace_id = str(uuid4())
    request.state.trace_id = trace_id
    token = request_trace_id.set(trace_id)
    try:
        response = await call_next(request)
        response.headers["X-Trace-ID"] = trace_id
        return response
    finally:
        request_trace_id.reset(token)

@application.get("/")
async def root_redirect():
    """Redirect to API docs.

    The web UI is a separate Vite app under `web/` and is typically run via `npm run dev`.
    """
    from fastapi.responses import RedirectResponse
    return RedirectResponse(url="/docs")


@application.get("/favicon.ico")
async def favicon():
    """Return an empty favicon to avoid noisy 404s in dev."""
    from fastapi.responses import Response
    return Response(content=b"", media_type="image/x-icon")


@application.get("/api")
async def root() -> dict:
    return {
        "name": settings.APP_NAME,
        "version": settings.APP_VERSION,
        "status": "running",
        "docs": "/docs",
    }


@application.get("/health")
async def health_check() -> dict:
    """Liveness probe: the API process is running."""
    return {"status": "healthy", "version": settings.APP_VERSION}


_READINESS_TIMEOUT_SECONDS = 2.0


def _readiness_targets() -> dict[str, tuple[str, int]]:
    """Core internal dependencies required by the default product runtime."""
    return {
        "database": (str(settings.APP_DATABASE_URL or ""), 3306),
        "business_database": (str(settings.BUSINESS_DATABASE_URL or ""), 3306),
        "neo4j": (str(settings.NEO4J_URI or ""), 7687),
        "knowledge_service": (str(settings.INGEST_SERVICE_URL or ""), 8000),
    }


async def _probe_tcp_url(url: str, default_port: int) -> bool:
    """Probe only internal transport availability; never expose credentials."""
    if not url:
        return False
    parsed = urlsplit(url)
    if parsed.scheme.startswith("sqlite"):
        # SQLite is process-local; startup already validates schema creation.
        return True
    host = parsed.hostname
    if not host:
        return False
    port = parsed.port or default_port
    writer = None
    try:
        _reader, writer = await asyncio.wait_for(
            asyncio.open_connection(host, port),
            timeout=_READINESS_TIMEOUT_SECONDS,
        )
        return True
    except (OSError, asyncio.TimeoutError):
        return False
    finally:
        if writer is not None:
            writer.close()
            try:
                await writer.wait_closed()
            except Exception:
                pass


async def _readiness_snapshot() -> tuple[bool, dict[str, str]]:
    targets = _readiness_targets()
    names = list(targets)
    probe_results = await asyncio.gather(
        *(_probe_tcp_url(*targets[name]) for name in names)
    )
    checks = {
        name: "ok" if probe_ok else "unreachable"
        for name, probe_ok in zip(names, probe_results)
    }
    checks["llm_config"] = "ok" if settings.OPENAI_API_KEY else "missing"
    checks["auth_config"] = "ok" if settings.AUTH_JWT_SECRET and len(settings.AUTH_JWT_SECRET.encode()) >= 32 else "missing"
    return all(value == "ok" for value in checks.values()), checks


@application.get("/ready")
async def readiness_check() -> JSONResponse:
    """Readiness probe for the core Multi-source Data Agent runtime."""
    ready, checks = await _readiness_snapshot()
    return JSONResponse(
        status_code=200 if ready else 503,
        content={
            "status": "ready" if ready else "not_ready",
            "version": settings.APP_VERSION,
            "checks": checks,
        },
    )


@application.on_event("startup")
async def startup_event() -> None:
    logger.info("Starting {} v{}", settings.APP_NAME, settings.APP_VERSION)
    logger.info("Debug mode: {}", settings.DEBUG)
    logger.info("API docs available at http://{}:{}/docs", settings.HOST, settings.PORT)

    # Auto-create database tables on startup
    import multisource_data_agent.infrastructure.persistence.db.models  # noqa: F401 - Import all models to register them with Base
    logger.info("Creating database tables if not exist...")
    if engine.dialect.name == "sqlite" and engine.url.database not in (None, "", ":memory:"):
        from pathlib import Path
        Path(engine.url.database).parent.mkdir(parents=True, exist_ok=True)
    Base.metadata.create_all(bind=engine)
    logger.info("Database tables ready")


@application.on_event("shutdown")
async def shutdown_event() -> None:
    logger.info("Shutting down {}", settings.APP_NAME)

    # Core tool clients are created lazily and do not require legacy API cleanup hooks.


@application.exception_handler(Exception)
async def global_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    trace_id = getattr(request.state, "trace_id", str(uuid4()))
    logger.opt(exception=exc).error("Unhandled exception on {} {}, trace_id={}", request.method, request.url.path, trace_id)
    return JSONResponse(
        status_code=500,
        content={
            "code": "internal_error", "category": "internal_error",
            "message": "An unexpected error occurred", "trace_id": trace_id,
        },
    )


@application.exception_handler(HTTPException)
@application.exception_handler(StarletteHTTPException)
async def safe_http_error(request: Request, exc: HTTPException) -> JSONResponse:
    trace_id = getattr(request.state, "trace_id", str(uuid4()))
    detail = exc.detail if isinstance(exc.detail, dict) and "code" in exc.detail else {
        "code": "request_failed", "category": "request_error",
        "message": "The request could not be completed",
    }
    if exc.status_code >= 500 and not isinstance(exc.detail, dict):
        logger.opt(exception=exc).error("HTTP failure, trace_id={}", trace_id)
    return JSONResponse(status_code=exc.status_code, content={"detail": {**detail, "trace_id": trace_id}}, headers=exc.headers)


@application.exception_handler(RequestValidationError)
async def safe_validation_error(request: Request, exc: RequestValidationError) -> JSONResponse:
    return JSONResponse(status_code=422, content={"detail": {
        "code": "invalid_request", "category": "validation", "message": "Request validation failed",
        "trace_id": getattr(request.state, "trace_id", str(uuid4())),
        "fields": [{"loc": list(item["loc"]), "type": item["type"]} for item in exc.errors()],
    }})


if __name__ == "__main__":
    uvicorn.run(
        "multisource_data_agent.main:application",
        host=settings.HOST,
        port=settings.PORT,
        reload=settings.DEBUG,
        log_level="info",
    )

# Backwards-compatible alias for tooling expecting `app` symbol
app = application
