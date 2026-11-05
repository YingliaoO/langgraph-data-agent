import asyncio
import importlib.util
from pathlib import Path
import sys



KB_ROOT = Path(__file__).resolve().parents[1] / "kb_ingest"
if str(KB_ROOT) not in sys.path:
    sys.path.insert(0, str(KB_ROOT))

_IDENTITY_SPEC = importlib.util.spec_from_file_location(
    "kb_document_identity", KB_ROOT / "kb_service" / "services" / "document_identity.py"
)
_IDENTITY_MODULE = importlib.util.module_from_spec(_IDENTITY_SPEC)
assert _IDENTITY_SPEC.loader is not None
_IDENTITY_SPEC.loader.exec_module(_IDENTITY_MODULE)

DEFAULT_ENTERPRISE_DATASET = _IDENTITY_MODULE.DEFAULT_ENTERPRISE_DATASET
infer_legacy_identity = _IDENTITY_MODULE.infer_legacy_identity
resolve_item_identity = _IDENTITY_MODULE.resolve_item_identity
stable_source_id = _IDENTITY_MODULE.stable_source_id
stable_upload_dataset_id = _IDENTITY_MODULE.stable_upload_dataset_id

from multisource_data_agent.application.agents.tool_execution.knowledge_rag import (  # noqa: E402
    KnowledgeRAGTool,
    PgVectorStrategy,
    _build_tool_result,
)

def run(coro):
    return asyncio.run(coro)


def test_legacy_rows_get_explicit_recipe_and_retail_identities():
    assert infer_legacy_identity("Sheet1", {}) == (
        "recipe",
        "recipe_legacy_v1",
    )
    assert infer_legacy_identity(
        "9171d1db-knowledge_base", {"doc_id": "ENT-REFUND-001"}
    ) == ("enterprise_retail", DEFAULT_ENTERPRISE_DATASET)


def test_upload_identity_does_not_depend_on_uuid_prefixed_filename():
    content = b"doc_id,title\nENT-1,Policy\n"
    first = stable_upload_dataset_id(content, filename="11111111-1111-4111-8111-111111111111_knowledge_base.csv")
    second = stable_upload_dataset_id(content, filename="22222222-2222-4222-8222-222222222222_knowledge_base.csv")
    assert first == second == DEFAULT_ENTERPRISE_DATASET

    generic_first = stable_upload_dataset_id(content, filename="11111111-1111-4111-8111-111111111111_upload.csv")
    generic_second = stable_upload_dataset_id(content, filename="22222222-2222-4222-8222-222222222222_upload.csv")
    assert generic_first == generic_second


def test_unknown_upload_identity_is_content_addressed_and_immutable():
    first = stable_upload_dataset_id(b"version-one", filename="upload.csv")
    same = stable_upload_dataset_id(b"version-one", filename="other.csv")
    changed = stable_upload_dataset_id(b"version-two", filename="upload.csv")

    assert first == same
    assert first.startswith("upload_")
    assert changed != first


def test_business_key_source_id_survives_row_reordering():
    assert stable_source_id({"doc_id": "ENT-REFUND-001"}, 0) == "ENT-REFUND-001"
    assert stable_source_id({"name": "no-id"}, 3) == "3"


def test_dedup_key_is_domain_dataset_source_and_content_changes_are_updates():
    source = Path("kb_ingest/kb_service/services/vector_store.py").read_text(encoding="utf-8")
    assert "ON CONFLICT (domain, dataset_id, source_id)" in source
    assert "content_hash" in source

    item = {
        "domain": "enterprise_retail",
        "dataset_id": "enterprise_retail_knowledge_v1",
        "source_table": "knowledge_base",
        "source_id": "ENT-REFUND-001",
        "original_data": {"name": "policy", "value": "7"},
    }
    first = resolve_item_identity(item)[:3]
    changed = dict(item, original_data={"name": "policy", "value": "30"})
    changed_identity = resolve_item_identity(changed)[:3]
    same_text_other_dataset = dict(item, dataset_id="another_dataset")
    other_identity = resolve_item_identity(same_text_other_dataset)[:3]
    assert first == changed_identity
    assert first != other_identity


def test_resolve_identity_adds_domain_dataset_without_changing_source_id():
    domain, dataset, source_id, metadata = resolve_item_identity(
        {
            "domain": "enterprise_retail",
            "dataset_id": "enterprise_retail_knowledge_v1",
            "source_table": "knowledge_base",
            "source_id": "ENT-1",
            "original_data": {"title": "Return policy"},
        }
    )
    assert (domain, dataset, source_id) == (
        "enterprise_retail",
        "enterprise_retail_knowledge_v1",
        "ENT-1",
    )
    assert metadata["domain"] == domain
    assert metadata["dataset_id"] == dataset
    assert metadata["source_id"] == source_id


class _Response:
    status = 200

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False

    async def json(self):
        return {
            "results": [
                {
                    "id": "ENT-1",
                    "source_id": "ENT-1",
                    "content": "Retail return policy",
                    "domain": "enterprise_retail",
                    "dataset_id": "enterprise_retail_knowledge_v1",
                    "similarity": 0.9,
                    "metadata": {},
                }
            ]
        }

    async def text(self):
        return ""


class _Session:
    last_payload = None

    def __init__(self, *args, **kwargs):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False

    def post(self, url, *, json):
        _Session.last_payload = json
        return _Response()


def test_pgvector_payload_propagates_domain_and_dataset_filter(monkeypatch):
    monkeypatch.setattr(
        "multisource_data_agent.application.agents.tool_execution.knowledge_rag.aiohttp.ClientSession",
        _Session,
    )
    strategy = PgVectorStrategy(search_url="http://kb-ingest.test/search")
    result = run(
        strategy.search(
            "return policy",
            top_k=3,
            context={"domain": "enterprise_retail", "dataset_id": "enterprise_retail_knowledge_v1"},
        )
    )
    assert _Session.last_payload["domain"] == "enterprise_retail"
    assert _Session.last_payload["dataset_id"] == "enterprise_retail_knowledge_v1"
    assert result[0]["metadata"]["domain"] == "enterprise_retail"


def test_old_pgvector_call_without_filter_remains_compatible(monkeypatch):
    monkeypatch.setattr(
        "multisource_data_agent.application.agents.tool_execution.knowledge_rag.aiohttp.ClientSession",
        _Session,
    )
    strategy = PgVectorStrategy(search_url="http://kb-ingest.test/search")
    run(strategy.search("legacy query", top_k=3, context={}))
    assert "domain" not in _Session.last_payload
    assert "dataset_id" not in _Session.last_payload


def test_search_path_does_not_run_identity_migration():
    source = Path("kb_ingest/kb_service/services/search.py").read_text(encoding="utf-8")

    assert "ensure_document_identity_schema" not in source
    assert "ALTER TABLE" not in source
    assert "DELETE FROM searchable_documents" not in source


def test_identity_migration_is_startup_owned_and_write_path_is_read_only_assertion():
    main_source = Path("kb_ingest/kb_service/main.py").read_text(encoding="utf-8")
    writer_source = Path("kb_ingest/kb_service/services/vector_store.py").read_text(encoding="utf-8")

    assert "@app.on_event(\"startup\")" in main_source
    assert "ensure_document_identity_schema(conn, logger)" in main_source
    assert "assert_document_identity_schema(conn)" in writer_source
    assert "ensure_document_identity_schema(conn" not in writer_source


def test_knowledge_tool_defaults_to_product_domain_but_allows_recipe_regression():
    class FakeStrategy:
        backend_name = "pgvector"

        def __init__(self):
            self.contexts = []

        async def search(self, query, *, top_k, context):
            self.contexts.append(dict(context))
            return []

    strategy = FakeStrategy()
    tool = KnowledgeRAGTool(pgvector_strategy=strategy)
    run(tool.execute("retail question"))
    run(tool.execute("recipe question", {"domain": "recipe"}))
    assert strategy.contexts[0]["domain"] == "enterprise_retail"
    assert strategy.contexts[1]["domain"] == "recipe"


def test_evidence_contract_preserves_source_id_and_domain_metadata():
    result = _build_tool_result(
        question="policy",
        documents=[
            {
                "id": "ENT-1",
                "source_id": "ENT-1",
                "source": "Retail Policy Center",
                "content": "Return within 7 days.",
                "similarity": 0.91,
                "domain": "enterprise_retail",
                "dataset_id": "enterprise_retail_knowledge_v1",
                "metadata": {"title": "Electronics Return Policy"},
            }
        ],
        status="success",
        backend="pgvector",
        attempts=[{"backend": "pgvector", "status": "success", "result_count": 1}],
        top_k=5,
    )
    evidence = result.evidence[0]
    assert evidence.source == "Retail Policy Center"
    assert evidence.source_id == "ENT-1"
    assert evidence.metadata["domain"] == "enterprise_retail"
    assert evidence.metadata["dataset_id"] == "enterprise_retail_knowledge_v1"
    assert evidence.citation == evidence.source
