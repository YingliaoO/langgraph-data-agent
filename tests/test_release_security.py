"""Public security boundaries: real JWT verification and isolated application persistence."""

import asyncio
from datetime import datetime, timedelta, timezone
import io
import json
from pathlib import Path
from uuid import uuid4

import httpx
from jose import jwt
import pytest
import pytest_asyncio
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from multisource_data_agent.config import settings
from multisource_data_agent.config.settings import Settings
from multisource_data_agent.infrastructure.core.database import Base, get_db
from multisource_data_agent.infrastructure.core.business_database import (
    assert_business_query,
    assert_readonly_account,
    business_database_url,
)
from multisource_data_agent.infrastructure.core.uploads import controlled_path
from multisource_data_agent.interfaces.http.v1 import chat as chat_module
from multisource_data_agent.main import application

TEST_SECRET = "unit-test-signing-key-not-for-deployment-123456789"


def bearer(subject="alice", *, secret=TEST_SECRET, expired=False, audience=None):
    now = datetime.now(timezone.utc)
    token = jwt.encode(
        {
            "sub": subject,
            "iat": now,
            "exp": now + timedelta(seconds=-10 if expired else 600),
            "iss": settings.AUTH_JWT_ISSUER,
            "aud": audience or settings.AUTH_JWT_AUDIENCE,
        },
        secret,
        algorithm="HS256",
    )
    return {"Authorization": "Bearer " + token}


@pytest_asyncio.fixture
async def secured_api(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "AUTH_JWT_SECRET", TEST_SECRET)
    monkeypatch.setattr(settings, "UPLOAD_DIR", str(tmp_path / "uploads"))
    monkeypatch.setattr(settings, "FILE_UPLOAD_MAX_MB", 1)
    db_engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(db_engine)
    factory = sessionmaker(bind=db_engine, expire_on_commit=False)

    def db_dependency():
        with factory() as session:
            yield session

    previous = application.dependency_overrides.copy()
    application.dependency_overrides[get_db] = db_dependency
    monkeypatch.setattr(chat_module, "SessionLocal", factory)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=application, raise_app_exceptions=False),
        base_url="http://testserver",
    ) as client:
        yield client, factory
    application.dependency_overrides.clear()
    application.dependency_overrides.update(previous)
    db_engine.dispose()


async def create_session(client, owner="alice"):
    response = await client.post(
        "/api/v1/sessions/",
        json={"id": owner + "-session", "title": "Private", "user_id": owner},
        headers=bearer(owner),
    )
    assert response.status_code == 201, response.text
    return response.json()["id"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "method,path,payload",
    [
        ("GET", "/api/v1/sessions/", None),
        ("POST", "/api/v1/chat/", {"message": "hello"}),
        ("POST", "/api/v1/chat/stream", {"message": "hello"}),
        ("GET", "/api/v1/chat/history/other", None),
        ("DELETE", "/api/v1/chat/session/other", None),
        ("GET", "/api/v1/upload/files/other.csv", None),
        ("DELETE", "/api/v1/upload/other", None),
    ],
)
async def test_no_anonymous_protected_api(secured_api, method, path, payload):
    response = await secured_api[0].request(method, path, json=payload)
    assert response.status_code == 401
    assert response.json()["detail"]["code"] == "authentication_required"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "headers",
    [
        bearer(expired=True),
        bearer(secret="wrong-key-that-is-long-enough-1234567890"),
        bearer(audience="another-app"),
        {"Authorization": "Bearer invalid"},
    ],
)
async def test_invalid_jwt_rejected(secured_api, headers):
    assert (await secured_api[0].get("/api/v1/sessions/", headers=headers)).status_code == 401


@pytest.mark.asyncio
async def test_no_secret_is_not_an_auth_bypass(secured_api, monkeypatch):
    monkeypatch.setattr(settings, "AUTH_JWT_SECRET", None)
    assert (await secured_api[0].get("/api/v1/sessions/", headers=bearer())).status_code == 503


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "method,path,payload",
    [
        ("GET", "/api/v1/sessions/alice-session", None),
        ("PATCH", "/api/v1/sessions/alice-session", {"title": "stolen"}),
        ("DELETE", "/api/v1/sessions/alice-session", None),
        ("GET", "/api/v1/chat/history/alice-session", None),
        ("DELETE", "/api/v1/chat/session/alice-session", None),
        ("POST", "/api/v1/chat/", {"message": "read it", "session_id": "alice-session"}),
        (
            "POST",
            "/api/v1/sessions/alice-session/messages",
            {
                "session_id": "alice-session",
                "message_type": "user_query",
                "content": "injected",
                "order_index": 1,
            },
        ),
    ],
)
async def test_cross_user_session_operations_denied(secured_api, method, path, payload):
    client, _ = secured_api
    await create_session(client)
    response = await client.request(method, path, json=payload, headers=bearer("bob"))
    assert response.status_code == 404, response.text


@pytest.mark.asyncio
async def test_session_list_owner_and_immutable_identity(secured_api):
    client, _ = secured_api
    await create_session(client)
    await create_session(client, "bob")
    own = await client.get("/api/v1/sessions/", headers=bearer())
    assert [r["id"] for r in own.json()] == ["alice-session"]
    assert (await client.get("/api/v1/sessions/", headers=bearer("ALICE"))).json() == []
    assert (
        await client.get("/api/v1/sessions/alice-session", headers=bearer("ALICE"))
    ).status_code == 404
    assert (await client.get("/api/v1/sessions/?user_id=bob", headers=bearer())).status_code == 403
    assert (
        await client.post(
            "/api/v1/sessions/",
            headers=bearer(),
            json={"id": "spoof", "title": "x", "user_id": "bob"},
        )
    ).status_code == 403
    assert (
        await client.patch(
            "/api/v1/sessions/alice-session", headers=bearer(), json={"user_id": "bob"}
        )
    ).status_code == 403
    assert (
        await client.post(
            "/api/v1/chat/", headers=bearer(), json={"message": "hello", "user_id": "bob"}
        )
    ).status_code == 403
    assert (
        await client.get("/api/v1/sessions/user/bob/count", headers=bearer())
    ).status_code == 403


@pytest.mark.asyncio
async def test_owner_can_restore_own_session_but_other_users_cannot(secured_api):
    client, _ = secured_api
    await create_session(client)
    assert (
        await client.delete("/api/v1/sessions/alice-session", headers=bearer())
    ).status_code == 204
    assert (await client.get("/api/v1/sessions/alice-session", headers=bearer())).status_code == 404
    assert (
        await client.patch(
            "/api/v1/sessions/alice-session", headers=bearer("bob"), json={"is_active": True}
        )
    ).status_code == 404
    restored = await client.patch(
        "/api/v1/sessions/alice-session", headers=bearer(), json={"is_active": True}
    )
    assert restored.status_code == 200
    assert restored.json()["user_id"] == "alice"


@pytest.mark.asyncio
async def test_normal_upload_is_owned_and_consumed_by_id(secured_api, monkeypatch):
    client, _ = secured_api
    response = await client.post(
        "/api/v1/upload/file",
        files={"file": ("orders.csv", b"id,name\n1,synthetic\n", "text/csv")},
        headers=bearer(),
    )
    assert response.status_code == 200, response.text
    upload = response.json()
    assert upload["filename"] == upload["file_id"] + ".csv"
    assert "orders" not in upload["filename"]
    path = "/api/v1/upload/files/" + upload["filename"]
    assert (await client.get(path, headers=bearer())).status_code == 200
    assert (await client.get(path, headers=bearer("bob"))).status_code == 404
    assert (await client.get(path, headers=bearer("ALICE"))).status_code == 404
    assert (
        await client.delete("/api/v1/upload/" + upload["file_id"], headers=bearer("bob"))
    ).status_code == 404
    assert (
        await client.post(
            "/api/v1/chat/",
            json={"message": "ingest", "file_id": upload["file_id"]},
            headers=bearer("bob"),
        )
    ).status_code == 404
    assert (
        await client.post(
            "/api/v1/chat/",
            json={"message": "ingest", "file_path": upload["file_path"]},
            headers=bearer(),
        )
    ).status_code == 400

    class Response:
        status = 202

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

        async def text(self):
            return "{}"

    class IngestSession:
        def __init__(self, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

        def post(self, url, **kwargs):
            assert url.endswith("/ingest/dataset/upload")
            return Response()

    from multisource_data_agent.application.agents import lg_builder

    monkeypatch.setattr(lg_builder.aiohttp, "ClientSession", IngestSession)
    monkeypatch.setattr(settings, "INGEST_SERVICE_URL", "http://knowledge.invalid")
    monkeypatch.setattr(
        settings, "LLM_API_KEY", None
    )  # A real upload must bypass LLM routing entirely.
    result = await client.post(
        "/api/v1/chat/", json={"message": "hello", "file_id": upload["file_id"]}, headers=bearer()
    )
    assert result.status_code == 200, result.text
    assert result.json()["route"] == "file-query", result.text
    assert "已提交" in result.json()["message"]
    assert (
        await client.delete("/api/v1/upload/" + upload["file_id"], headers=bearer())
    ).status_code == 200


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "filename,content",
    [
        ("../escape.csv", b"a\n1"),
        ("C:\\private\\x.csv", b"a\n1"),
        ("x.exe", b"MZ"),
        ("x.csv", b"MZ\0binary"),
        ("x.xlsx", b"not a workbook"),
    ],
)
async def test_bad_upload_does_not_create_files(secured_api, filename, content):
    if "\\" in filename:
        # Multipart parsers may normalize Windows browser filenames before the handler sees them.
        # Exercise the raw-name guard directly rather than claiming a normalized name is traversal.
        from fastapi import UploadFile, HTTPException
        from multisource_data_agent.interfaces.http.v1.upload import upload_file

        with secured_api[1]() as db:
            with pytest.raises(HTTPException) as exc:
                await upload_file(UploadFile(io.BytesIO(content), filename=filename), db, "alice")
            assert exc.value.status_code == 400
        return
    response = await secured_api[0].post(
        "/api/v1/upload/file", headers=bearer(), files={"file": (filename, content)}
    )
    assert response.status_code == 400, response.text
    assert not list(Path(settings.UPLOAD_DIR).glob("*"))


@pytest.mark.asyncio
async def test_oversized_upload_is_stopped_and_cleaned(secured_api):
    response = await secured_api[0].post(
        "/api/v1/upload/file",
        headers=bearer(),
        files={"file": ("large.csv", b"a\n" + b"x" * 1024 * 1024)},
    )
    assert response.status_code == 413
    assert not list(Path(settings.UPLOAD_DIR).glob("*"))


@pytest.mark.parametrize(
    "name", ["../x.csv", "C:\\secret.csv", "/etc/passwd", "a.csv", "../" + str(uuid4()) + ".csv"]
)
def test_controlled_path_rejects_traversal(name):
    with pytest.raises(Exception) as exc:
        controlled_path(name)
    assert exc.value.status_code == 400


@pytest.fixture
def business_config(monkeypatch):
    url = "mysql+pymysql://retail_reader:fixture-only@mysql/retail_demo"
    monkeypatch.setattr(settings, "BUSINESS_DATABASE_URL", url)
    monkeypatch.setattr(
        settings, "APP_DATABASE_URL", "mysql+pymysql://agent_app:fixture-only@mysql/agent_app"
    )
    return url


def test_database_urls_are_separate_and_missing_business_fails_closed(business_config, monkeypatch):
    assert business_database_url() == business_config
    with pytest.raises(ValueError):
        business_database_url(settings.APP_DATABASE_URL)
    monkeypatch.setattr(settings, "BUSINESS_DATABASE_URL", None)
    with pytest.raises(ValueError):
        business_database_url()


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT * FROM users",
        "SELECT * FROM chat_sessions",
        "SELECT * FROM agent_app.users",
        "SELECT * FROM agent_app.retail_orders",
        "SELECT * FROM information_schema.tables",
        "WITH retail_orders AS (SELECT * FROM users) SELECT * FROM retail_orders",
        "SELECT * FROM retail_orders; DELETE FROM retail_orders",
        "DELETE FROM retail_orders",
        "SELECT * FROM mysql.user",
    ],
)
def test_business_query_rejects_internal_cross_schema_and_writes(business_config, sql):
    with pytest.raises(ValueError):
        assert_business_query(sql)


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT COUNT(*) FROM retail_orders",
        "SELECT * FROM retail_demo.retail_orders",
        "WITH c AS (SELECT * FROM retail_orders) SELECT * FROM c",
    ],
)
def test_allowed_business_queries(business_config, sql):
    assert_business_query(sql)


@pytest.mark.asyncio
async def test_schema_discovery_never_exposes_internal_tables(business_config, monkeypatch):
    from sqlalchemy.engine import make_url
    from multisource_data_agent.application.agents.text2sql.components.schema_retrieval import node

    column_requests = []

    class Result:
        def __init__(self, rows):
            self.rows = rows

        def fetchall(self):
            return self.rows

    class Connection:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def exec_driver_sql(self, sql):
            return Result([("GRANT SELECT ON `retail_demo`.`retail_orders` TO 'reader'@'%'",)])

        def execute(self, statement, params=None):
            text = str(statement)
            if "INFORMATION_SCHEMA.TABLES" in text:
                return Result(
                    [
                        ("retail_orders", "Orders"),
                        ("users", "Private"),
                        ("chat_sessions", "Private"),
                    ]
                )
            if "INFORMATION_SCHEMA.COLUMNS" in text:
                column_requests.append(params["table_name"])
                return Result([("order_id", "varchar", "", "PRI")])
            return Result([("retail_orders", "order_id", "users", "id")])

    class Engine:
        url = make_url(business_config)

        def connect(self):
            return Connection()

        def dispose(self):
            pass

    monkeypatch.setattr(node, "create_engine", lambda *a, **kw: Engine())
    schema = await node._retrieve_schema_from_mysql("count orders")
    assert [t["table_name"] for t in schema["tables"]] == ["retail_orders"]
    assert column_requests == ["retail_orders"]
    assert not schema["relationships"]


@pytest.mark.parametrize(
    "grant",
    [
        "GRANT ALL PRIVILEGES ON `retail_demo`.* TO 'reader'@'%'",
        "GRANT SELECT ON *.* TO 'reader'@'%'",
        "GRANT SELECT ON `agent_app`.`users` TO 'reader'@'%'",
        "GRANT INSERT, SELECT ON `retail_demo`.`retail_orders` TO 'reader'@'%'",
        "GRANT SELECT ON `retail_demo`.`retail_orders` TO 'reader'@'%' WITH GRANT OPTION",
    ],
)
def test_business_account_rejects_excess_grants(business_config, grant):
    class Connection:
        def exec_driver_sql(self, sql):
            assert sql == "SHOW GRANTS FOR CURRENT_USER"
            return self

        def fetchall(self):
            return [(grant,)]

    with pytest.raises(ValueError):
        assert_readonly_account(Connection())


@pytest.mark.parametrize(
    "value",
    ["*", "https://example.com,*", "https://user:pass@example.com", "https://example.com/path"],
)
def test_unsafe_cors_configuration_is_rejected(value):
    with pytest.raises(ValueError):
        Settings(_env_file=None, CORS_ORIGINS=value)


def test_cors_secure_default_and_explicit_origins(monkeypatch):
    monkeypatch.delenv("CORS_ORIGINS", raising=False)
    assert Settings(_env_file=None).cors_origins_list == []
    assert Settings(
        _env_file=None, CORS_ORIGINS="https://console.example.com,http://localhost:5173"
    ).cors_origins_list == ["https://console.example.com", "http://localhost:5173"]
    middleware = next(m for m in application.user_middleware if m.cls.__name__ == "CORSMiddleware")
    assert "*" not in middleware.kwargs["allow_origins"]


def test_compose_does_not_share_application_credentials_with_knowledge_service():
    import yaml

    compose = yaml.safe_load(Path("docker-compose.yml").read_text(encoding="utf-8"))
    knowledge = compose["services"]["kb_ingest"]
    assert not knowledge.get("env_file")
    assert not {
        "AUTH_JWT_SECRET",
        "APP_DATABASE_URL",
        "APP_DB_PASSWORD",
        "MYSQL_ROOT_PASSWORD",
    } & set(knowledge["environment"])
    assert not any(".env:" in str(mount) for mount in compose["services"]["backend"]["volumes"])


@pytest.mark.asyncio
async def test_untrusted_origin_cannot_read_credentialed_api(secured_api):
    response = await secured_api[0].options(
        "/api/v1/sessions/",
        headers={"Origin": "https://attacker.invalid", "Access-Control-Request-Method": "GET"},
    )
    assert response.status_code == 400
    assert "access-control-allow-origin" not in response.headers


@pytest.mark.asyncio
async def test_stream_saves_one_complete_assistant_message(secured_api, monkeypatch):
    async def fake_process(*args, **kwargs):
        return {
            "message": "完整 answer without splitting",
            "route": "general-query",
            "metadata": {},
            "execution": None,
        }

    monkeypatch.setattr(chat_module, "process_agent_query", fake_process)
    response = await secured_api[0].post(
        "/api/v1/chat/stream", json={"message": "hello"}, headers=bearer()
    )
    assert response.status_code == 200
    events = [
        json.loads(line[6:]) for line in response.text.splitlines() if line.startswith("data: ")
    ]
    assert [e["type"] for e in events] == ["progress", "final", "done"]
    assert events[1]["content"] == "完整 answer without splitting"
    history = await secured_api[0].get(
        "/api/v1/chat/history/" + events[0]["session_id"], headers=bearer()
    )
    assert [m["content"] for m in history.json()] == ["hello", "完整 answer without splitting"]


@pytest.mark.asyncio
async def test_stream_errors_are_safe_and_terminated(monkeypatch):
    async def broken(*args, **kwargs):
        raise RuntimeError("mysql://secret-password@private.internal")

    monkeypatch.setattr(chat_module, "process_agent_query", broken)
    chunks = [chunk async for chunk in chat_module.stream_agent_response("q", "session")]
    assert "secret-password" not in "".join(chunks)
    assert "event: error" in chunks[-2] and "event: done" in chunks[-1]


@pytest.mark.asyncio
async def test_stream_disconnect_cancels_work_without_false_final(monkeypatch):
    started = asyncio.Event()
    cancelled = asyncio.Event()

    async def pending(*args, **kwargs):
        started.set()
        try:
            await asyncio.Future()
        finally:
            cancelled.set()

    monkeypatch.setattr(chat_module, "process_agent_query", pending)
    stream = chat_module.stream_agent_response("q", "session")
    assert "progress" in await stream.__anext__()
    task = asyncio.create_task(stream.__anext__())
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert cancelled.is_set()
    await stream.aclose()


@pytest.mark.asyncio
async def test_global_exception_is_safe_even_with_debug_enabled(secured_api, monkeypatch):
    monkeypatch.setattr(settings, "DEBUG", True)

    def broken_db():
        raise RuntimeError(
            "database-driver mysql://private-user:password-value@internal-host/private-db"
        )

    previous = application.dependency_overrides[get_db]
    application.dependency_overrides[get_db] = broken_db
    try:
        response = await secured_api[0].get("/api/v1/sessions/", headers=bearer())
    finally:
        application.dependency_overrides[get_db] = previous
    assert response.status_code == 500
    body = response.json()
    assert set(body) == {"code", "category", "message", "trace_id"}
    assert body["code"] == "internal_error" and body["trace_id"]
    for private in [
        "database-driver",
        "private-user",
        "password-value",
        "internal-host",
        "private-db",
    ]:
        assert private not in response.text


def test_partial_answer_fallback_cannot_echo_internal_tool_exceptions():
    from multisource_data_agent.application.agents.tool_contracts import ToolResult, Evidence
    from multisource_data_agent.application.agents.kg_sub_graph.agentic_rag_agents.components.summarize.evidence_context import (
        build_evidence_context,
    )
    from multisource_data_agent.application.agents.kg_sub_graph.agentic_rag_agents.components.summarize.answer_generator import (
        render_sections,
    )

    secret = "DB driver mysql://private-user:private-password@internal-database/schema"
    context = build_evidence_context(
        [
            ToolResult(
                tool_name="sql_agent",
                tool_kind="sql",
                status="success",
                task="count orders",
                data=[{"total": 12}],
                evidence=[Evidence(kind="sql_row", source="mysql", content={"total": 12})],
            ),
            ToolResult(
                tool_name="knowledge_rag",
                tool_kind="rag",
                status="error",
                task="policy",
                answer=secret,
                metadata={"errors": [secret]},
            ),
        ]
    )
    rendered = render_sections(*context.summary_sections())
    assert "12" in rendered
    assert context.metadata["errors"]
    for value in [secret, "private-password", "internal-database", "DB driver"]:
        assert value not in rendered
        assert value not in json.dumps(context.metadata)


def test_owner_filter_is_exact_even_with_case_insensitive_database_collation():
    from sqlalchemy import MetaData, Table, Column, String, insert, select
    from sqlalchemy.dialects import mysql
    from multisource_data_agent.infrastructure.core.database import owner_filter

    engine = create_engine("sqlite://")
    table = Table("case_test", MetaData(), Column("owner", String(collation="NOCASE")))
    table.metadata.create_all(engine)
    with engine.begin() as connection:
        connection.execute(insert(table), [{"owner": "alice"}, {"owner": "ALICE"}])
        assert len(connection.execute(select(table).where(table.c.owner == "alice")).all()) == 2
        assert connection.execute(
            select(table).where(owner_filter(table.c.owner, "alice"))
        ).all() == [("alice",)]
        assert connection.execute(
            select(table).where(owner_filter(table.c.owner, "ALICE"))
        ).all() == [("ALICE",)]
    assert "AS BINARY" in str(
        select(table).where(owner_filter(table.c.owner, "alice")).compile(dialect=mysql.dialect())
    )
    engine.dispose()
