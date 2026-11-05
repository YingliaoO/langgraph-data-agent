"""
Multi-Source Data Agent FastAPI application entry point.
"""

import asyncio
import os
from urllib.parse import urlsplit

import uvicorn
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from loguru import logger

from multisource_data_agent.config import settings
from multisource_data_agent.infrastructure.core import configure_logging
from multisource_data_agent.infrastructure.core.database import Base, engine
from multisource_data_agent.interfaces.http.v1 import api_router as api_v1_router

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
    allow_origins=["*"],  # 允许所有来源，方便开发和测试
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Routers --------------------------------------------------------------------
application.include_router(api_v1_router, prefix=settings.API_V1_PREFIX)

# Static files ---------------------------------------------------------------
# 创建上传目录
uploads_dir = os.path.join(os.path.dirname(__file__), "..", "..", "uploads")
os.makedirs(uploads_dir, exist_ok=True)

# 挂载静态文件
application.mount("/uploads", StaticFiles(directory=uploads_dir), name="uploads")

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
        "database": (str(settings.DATABASE_URL or ""), 3306),
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
    Base.metadata.create_all(bind=engine)
    logger.info("Database tables ready")


@application.on_event("shutdown")
async def shutdown_event() -> None:
    logger.info("Shutting down {}", settings.APP_NAME)

    # Core tool clients are created lazily and do not require legacy API cleanup hooks.


@application.exception_handler(Exception)
async def global_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    logger.error("Unhandled exception on {} {}: {}", request.method, request.url, exc)
    return JSONResponse(
        status_code=500,
        content={
            "error": "Internal Server Error",
            "message": str(exc) if settings.DEBUG else "An unexpected error occurred",
        },
    )


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
