"""Knowledge-service upload/security checks with no database or model calls."""

import asyncio
from pathlib import Path
import sys
from types import SimpleNamespace

import httpx
import pytest
import pytest_asyncio

KB_ROOT = Path(__file__).resolve().parents[1] / "kb_ingest"
if str(KB_ROOT) not in sys.path:
    sys.path.insert(0, str(KB_ROOT))

from kb_service.api import routes
from kb_service.api.deps import get_config
from kb_service.core.config import Config
from kb_service.main import create_app

SERVICE_KEY = "fixture-internal-service-token-not-for-deployment-123456"


@pytest_asyncio.fixture
async def knowledge_api(monkeypatch, tmp_path):
    monkeypatch.setenv("KNOWLEDGE_SERVICE_TOKEN", SERVICE_KEY)
    monkeypatch.setenv("FILE_UPLOAD_MAX_MB", "1")
    app = create_app()
    app.dependency_overrides[get_config] = lambda: Config(use_llm=False)
    processed = []

    class Processor:
        def __init__(self, config, **kwargs):
            self.config = config

        def process_csv(self, path):
            processed.append((Path(path).name, Path(path).read_bytes()))

        def process_excel(self):
            processed.append((Path(self.config.excel_file_path).name, b"workbook"))

    monkeypatch.setattr(routes, "DataProcessor", Processor)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app, raise_app_exceptions=False),
        base_url="http://knowledge",
    ) as client:
        yield client, processed


@pytest.mark.asyncio
@pytest.mark.parametrize("prefix", ["/api", "/api/v1/knowledge"])
async def test_both_knowledge_aliases_require_service_auth(knowledge_api, prefix):
    response = await knowledge_api[0].post(prefix + "/search", json={"query": "policy"})
    assert response.status_code == 401
    assert "trace_id" in response.json()["detail"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "filename,content,status",
    [
        ("data.csv", b"id,name\n1,test\n", 202),
        ("../data.csv", b"id\n1", 400),
        ("data.pdf", b"pdf", 400),
        ("data.csv", b"binary\0content", 400),
        ("data.xlsx", b"not an excel file", 400),
        ("large.csv", b"a\n" + b"x" * 1024 * 1024, 413),
    ],
    ids=[
        "valid-csv",
        "path-traversal",
        "unsupported-type",
        "binary-csv",
        "invalid-xlsx",
        "over-limit",
    ],
)
async def test_knowledge_upload_limits_format_and_generated_filename(
    knowledge_api, filename, content, status
):
    client, processed = knowledge_api
    response = await client.post(
        "/api/v1/knowledge/ingest/dataset/upload",
        headers={"X-Knowledge-Service-Key": SERVICE_KEY},
        files={"file": (filename, content)},
    )
    assert response.status_code == status, response.text
    if status == 202:
        assert len(processed) == 1
        assert processed[0][0] != filename
        assert processed[0][1] == content
    else:
        assert processed == []


@pytest.mark.asyncio
async def test_legacy_excel_upload_has_same_limit(knowledge_api):
    response = await knowledge_api[0].post(
        "/api/ingest/excel/upload",
        headers={"X-Knowledge-Service-Key": SERVICE_KEY},
        files={"file": ("large.xls", b"x" * (1024 * 1024 + 1))},
    )
    assert response.status_code == 413
    assert knowledge_api[1] == []


@pytest.mark.asyncio
async def test_legacy_ingest_cannot_read_arbitrary_server_path(knowledge_api):
    response = await knowledge_api[0].post(
        "/api/ingest/excel",
        headers={"X-Knowledge-Service-Key": SERVICE_KEY},
        json={"excel_path": "/private/server/secret.xlsx"},
    )
    assert response.status_code == 400
    assert "/private/server" not in response.text


@pytest.mark.asyncio
@pytest.mark.parametrize("service", ["agent", "knowledge"])
async def test_raw_stream_body_is_bounded_even_without_content_length(service, monkeypatch):
    from multisource_data_agent.interfaces.http.body_limit import RequestBodyLimit as AgentLimit
    from kb_service.api.upload_security import RequestBodyLimit as KnowledgeLimit

    monkeypatch.setenv("FILE_UPLOAD_MAX_MB", "1")
    received = 0

    async def receive():
        nonlocal received
        received += 1
        return {"type": "http.request", "body": b"x" * (512 * 1024), "more_body": received < 4}

    async def app(scope, bounded_receive, send):
        while (await bounded_receive())["more_body"]:
            pass

    limiter = AgentLimit(app, lambda: 1024 * 1024) if service == "agent" else KnowledgeLimit(app)
    with pytest.raises(Exception) as exc:
        await limiter({"type": "http", "headers": []}, receive, None)
    assert exc.value.status_code == 413
    assert received == 3  # Stop before consuming the entire body.
