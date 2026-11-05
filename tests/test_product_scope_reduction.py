from pathlib import Path

import pytest
import yaml

from multisource_data_agent.application.agents.routing import CANONICAL_ROUTE_TYPES, normalize_route_type
from multisource_data_agent.application.agents.tool_execution.interface import create_default_tool_registry


def _read_repo_source_or_skip(path: str, *, encoding: str = "utf-8") -> str:
    source_path = Path(path)
    if not source_path.exists():
        pytest.skip(f"repository source is not mounted in this test runtime: {path}")
    return source_path.read_text(encoding=encoding)


class _FakeTool:
    def __init__(self, name: str, kind: str) -> None:
        self.tool_name = name
        self.tool_kind = kind

    async def execute(self, task, context=None, *, trace=None):  # pragma: no cover
        raise AssertionError("not executed in registry-surface test")


def test_product_router_surface_excludes_image() -> None:
    assert CANONICAL_ROUTE_TYPES == (
        "general-query",
        "additional-query",
        "data-query",
        "file-query",
    )
    assert normalize_route_type("image-query") == "general-query"

    source = Path("multisource_data_agent/application/agents/lg_builder.py").read_text(encoding="utf-8-sig")
    registry_block = source.split("# 定义状态图", 1)[1]
    assert "builder.add_node(create_image_query)" not in registry_block
    assert "builder.add_node(create_kb_query)" not in registry_block
    assert "async def create_image_query(" not in source
    assert "async def create_kb_query(" not in source


def test_public_chat_and_upload_surface_no_longer_exposes_image_mode() -> None:
    chat_source = Path("multisource_data_agent/interfaces/http/v1/chat.py").read_text(encoding="utf-8")
    upload_source = Path("multisource_data_agent/interfaces/http/v1/upload.py").read_text(encoding="utf-8")

    request_block = chat_source.split("class ChatRequest", 1)[1].split("class ChatResponse", 1)[0]
    routes_block = chat_source.split("async def get_route_info", 1)[1]
    assert "image_path" not in request_block
    assert '"image-query"' not in routes_block
    assert '@router.post("/image")' not in upload_source
    assert '@router.get("/images/{filename}")' not in upload_source


def test_legacy_knowledge_and_lightrag_routers_are_not_mounted() -> None:
    source = Path("multisource_data_agent/main.py").read_text(encoding="utf-8")
    assert "knowledge_router" not in source
    assert "lightrag_router" not in source
    assert "get_lightrag_service" not in source
    assert "get_neo4j_qa_service" not in source
    assert "application.include_router(api_v1_router" in source


def test_default_compose_contains_only_core_services() -> None:
    raw = _read_repo_source_or_skip("docker-compose.yml", encoding="utf-8-sig")
    compose = yaml.safe_load(raw)
    assert "version" not in compose
    assert set(compose["services"]) == {
        "backend",
        "neo4j",
        "mysql",
        "kb_postgres",
        "kb_ingest",
    }
    assert set(compose.get("volumes", {})) == {
        "neo4j_data",
        "neo4j_logs",
        "mysql_data",
        "kb_pgdata",
    }
    backend = compose["services"]["backend"]
    assert set(backend["depends_on"]) == {"neo4j", "mysql", "kb_ingest"}
    backend_env = "\n".join(backend.get("environment", []))
    for legacy in ("REDIS_", "MILVUS_", "LIGHTRAG_", "VISION_", "IMAGE_GENERATION_"):
        assert legacy not in backend_env


def test_backend_bootstrap_no_longer_waits_for_legacy_stores() -> None:
    dockerfile = _read_repo_source_or_skip("Dockerfile", encoding="utf-8-sig")
    entrypoint = _read_repo_source_or_skip("scripts/backend_entrypoint.sh")
    assert "wait_for_mysql" in entrypoint
    assert "wait_for_milvus" not in entrypoint
    assert "run_milvus_ingest" not in entrypoint
    assert "bootstrap_lightrag" not in entrypoint
    assert "init_lightrag.py" not in dockerfile
    assert "INIT_LIGHTRAG_ON_BUILD" not in dockerfile


def test_milvus_alias_is_not_part_of_core_tool_registry() -> None:
    registry = create_default_tool_registry(
        sql_agent=_FakeTool("sql_agent", "sql"),
        knowledge_rag=_FakeTool("knowledge_rag", "rag"),
        graph_tool=_FakeTool("graph_tool", "graph"),
    )
    assert "milvus" not in registry.aliases()
    assert registry.canonical_name("pgvector") == "knowledge_rag"


def test_core_imports_do_not_eagerly_import_legacy_milvus_service() -> None:
    kb_source = Path("multisource_data_agent/application/agents/tool_execution/knowledge_rag.py").read_text(encoding="utf-8")
    canonical_source = kb_source.split("class KnowledgeRAGTool", 1)[1]
    multi_source = Path(
        "multisource_data_agent/application/agents/kg_sub_graph/agentic_rag_agents/"
        "workflows/multi_agent/multi_tool.py"
    ).read_text(encoding="utf-8")
    assert "KnowledgeService(" not in canonical_source
    assert "from multisource_data_agent.infrastructure.knowledge import KnowledgeService" not in multi_source


def test_env_example_documents_only_core_stack() -> None:
    source = _read_repo_source_or_skip(".env.example", encoding="utf-8-sig")
    for legacy in (
        "MILVUS_HOST",
        "REDIS_HOST",
        "LIGHTRAG_WORKING_DIR",
        "VISION_API_KEY",
        "IMAGE_GENERATION_API_KEY",
        "AUTO_BOOTSTRAP_LIGHTRAG",
    ):
        assert legacy not in source
    assert "BACKEND_INGEST_SERVICE_URL=http://kb_ingest:8000" in source
    assert "BUSINESS_DATABASE_URL=mysql+pymysql://retail_reader:local_business_demo_only@mysql:3306/retail_demo" in source
    assert "APP_DATABASE_URL=mysql+pymysql://agent_app:local_app_demo_only@mysql:3306/agent_app" in source


def test_debug_is_opt_in_and_legacy_sqlite_path_is_preserved(monkeypatch) -> None:
    from multisource_data_agent.config.settings import Settings

    monkeypatch.delenv("DEBUG", raising=False)
    monkeypatch.delenv("DATABASE_URL", raising=False)
    settings = Settings(_env_file=None)
    assert settings.DEBUG is False
    assert settings.DATABASE_URL == "sqlite:///./data/gustobot.db"
    monkeypatch.setenv("DEBUG", "true")
    assert Settings(_env_file=None).DEBUG is True


def test_production_override_removes_demo_exposure_and_requires_credentials() -> None:
    import re

    doc = Path("docs/DEPLOYMENT.md").read_text(encoding="utf-8")
    template = doc.split("<!-- production-compose-override:start -->", 1)[1].split(
        "<!-- production-compose-override:end -->", 1
    )[0]
    template = template.split("```yaml\n", 1)[1].split("```", 1)[0]
    # Parsing is for assertions only: preserve the Compose merge tags in the actual template.
    parsed = yaml.safe_load(re.sub(r" !(?:override|reset)", "", template))
    services = parsed["services"]
    assert services["backend"]["environment"]["DEBUG"] == "false"
    assert "--reload" not in services["backend"]["command"]
    assert services["backend"]["environment"]["MYSQL_ROOT_PASSWORD"] == ""
    assert services["backend"]["ports"] == ["127.0.0.1:8000:8000"]
    assert services["kb_ingest"]["env_file"] == []
    for name in ("mysql", "neo4j", "kb_postgres", "kb_ingest"):
        assert services[name]["ports"] == []
    neo = services["neo4j"]["environment"]
    assert neo["NEO4J_dbms_security_auth__enabled"] == "true"
    assert neo["NEO4J_AUTH"].startswith("neo4j/${PROD_NEO4J_PASSWORD:?")
    assert "*" not in neo["NEO4J_dbms_security_procedures_allowlist"]
    assert "*" not in neo["NEO4J_dbms_security_procedures_unrestricted"]
    assert "local_demo_only" not in template
    for variable in ("PROD_APP_DATABASE_URL", "PROD_BUSINESS_DATABASE_URL", "PROD_APP_DB_PASSWORD", "PROD_BUSINESS_DB_PASSWORD", "PROD_MYSQL_PASSWORD", "PROD_MYSQL_ROOT_PASSWORD", "PROD_NEO4J_PASSWORD", "PROD_PG_PASSWORD"):
        assert "${" + variable + ":?" in template
