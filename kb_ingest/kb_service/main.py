
import logging

from fastapi import FastAPI
import psycopg2

from kb_service.api.routes import router as api_router
from kb_service.core.config import load_config
from kb_service.services.document_identity import ensure_document_identity_schema
from kb_service.services.document_storage import ensure_document_search_schema

logger = logging.getLogger(__name__)


def _run_identity_migration_at_startup() -> None:
    """Run the idempotent document identity migration once per service start."""

    config = load_config()
    conn = psycopg2.connect(**config.db_config)
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT to_regclass('public.searchable_documents');")
            table_exists = cur.fetchone()[0] is not None
        if table_exists:
            ensure_document_identity_schema(conn, logger)
            ensure_document_search_schema(conn)
            logger.info("searchable_documents identity migration completed before readiness")
        else:
            logger.info(
                "searchable_documents does not exist at startup; "
                "the ingestion writer will create the current identity schema"
            )
    finally:
        conn.close()


def create_app() -> FastAPI:
    app = FastAPI(
        title="Knowledge Ingestion Service",
        version="0.1.0",
        description="Rewrite tabular data with LLMs, embed via pgvector, and expose retrieval APIs.",
    )
    # expose both legacy /api and the expected /api/v1/knowledge paths
    app.include_router(api_router, prefix="/api")
    app.include_router(api_router, prefix="/api/v1/knowledge")

    @app.get("/health", tags=["system"])
    def health_check():
        return {"status": "ok"}

    @app.on_event("startup")
    def startup_identity_migration() -> None:
        _run_identity_migration_at_startup()

    logger.info("FastAPI application initialised")
    return app


app = create_app()
