"""Fail-closed boundary for the read-only business database."""

import re

import sqlglot
from sqlglot import exp
from sqlalchemy.engine import make_url
from sqlglot.optimizer.scope import traverse_scope

from multisource_data_agent.config import settings

INTERNAL_TABLES = frozenset(
    {
        "users",
        "user",
        "chat_sessions",
        "chat_messages",
        "chat_session_snapshots",
        "conversations",
        "messages",
        "conversation_history_snapshots",
        "chat_history_snapshots",
        "uploaded_files",
    }
)


def business_scope() -> tuple[set[str], set[str]]:
    schemas = {s.strip().lower() for s in settings.BUSINESS_ALLOWED_SCHEMAS.split(",") if s.strip()}
    tables = {t.strip().lower() for t in settings.BUSINESS_ALLOWED_TABLES.split(",") if t.strip()}
    if not schemas or not tables or tables & INTERNAL_TABLES or "*" in schemas | tables:
        raise ValueError("Explicit business schema/table allowlists are required")
    return schemas, tables


def business_database_url(override: str | None = None) -> str:
    configured = settings.BUSINESS_DATABASE_URL
    if not configured:
        raise ValueError("BUSINESS_DATABASE_URL is required; application fallback is forbidden")
    if override and make_url(override) != make_url(configured):
        raise ValueError("Only the configured business connection is permitted")
    url = make_url(configured)
    app = make_url(settings.APP_DATABASE_URL)
    schemas, _ = business_scope()
    if url.get_backend_name() != "mysql" or (url.database or "").lower() not in schemas:
        raise ValueError("A MySQL connection in the allowed business schema is required")
    if not url.username or url.username.lower() == "root":
        raise ValueError("A dedicated read-only business account is required")
    if (url.host, url.port or 3306, url.username) == (app.host, app.port or 3306, app.username):
        raise ValueError("Application and business database accounts must differ")
    return configured


def assert_readonly_account(connection) -> None:
    """Reject write/admin privileges and ambiguous role grants before any business SQL."""
    grants = connection.exec_driver_sql("SHOW GRANTS FOR CURRENT_USER").fetchall()
    if not grants:
        raise ValueError("Cannot verify the business account privileges")
    for row in grants:
        grant = str(row[0]).upper()
        match = re.match(r"GRANT (.+?) ON ", grant)
        if not match or "WITH GRANT OPTION" in grant:
            raise ValueError("Business account must have explicit read-only grants")
        if not {p.strip() for p in match.group(1).split(",")} <= {"USAGE", "SELECT", "SHOW VIEW"}:
            raise ValueError("Business account has non-read-only privileges")
        if match.group(1) != "USAGE":
            target = grant.split(" ON ", 1)[1].split(" TO ", 1)[0].replace("`", "").lower()
            schemas, tables = business_scope()
            if "." not in target:
                raise ValueError("Business grants must name an allowed table")
            schema, table = target.split(".", 1)
            if schema not in schemas or table not in tables:
                raise ValueError("Business grants exceed the configured scope")


def assert_business_query(sql: str) -> None:
    """Check physical sources by SQL scope, including CTEs and qualified references."""
    from multisource_data_agent.application.agents.text2sql.components.sql_execution.node import (
        _is_read_only_query,
    )

    if not _is_read_only_query(sql):
        raise ValueError("Only one read-only business query is allowed")
    statements = [s for s in sqlglot.parse(sql, read="mysql") if s is not None]
    if len(statements) != 1 or not isinstance(statements[0], exp.Query):
        raise ValueError("Only SELECT queries are allowed")
    schemas, tables = business_scope()
    for scope in traverse_scope(statements[0]):
        for source in scope.sources.values():
            if not isinstance(source, exp.Table):
                continue  # A derived scope/CTE is checked in its own scope.
            if source.catalog or source.name.lower() not in tables:
                raise ValueError("SQL source is outside the business table allowlist")
            if source.db and source.db.lower() not in schemas:
                raise ValueError("SQL source is outside the business schema allowlist")
