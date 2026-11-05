"""Real search orchestration with deterministic DB/embedding/reranker doubles."""

import logging
import sys
from pathlib import Path
from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest

KB_ROOT = Path(__file__).resolve().parents[1] / "kb_ingest"
if str(KB_ROOT) not in sys.path:
    sys.path.insert(0, str(KB_ROOT))

from kb_service.services import search as search_module
from kb_service.schemas.search import SearchRequest


class Cursor:
    def __init__(self):
        self.query = ""
        self.params = []

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def execute(self, query, params=None):
        if params:
            self.query, self.params = query, params

    def mogrify(self, *_):
        return b"query (redacted test fixture)"

    def fetchall(self):
        return [(n, "policies", f"doc-{n}", {}, f"Policy {n}", "enterprise_retail", "test",
                 1 - n / 100, n / 100) for n in range(1, min(7, self.params[-1] + 1))]


class Reranker:
    calls = 0

    def rerank(self, query, candidates, top_n=None):
        self.calls += 1
        self.candidates = candidates
        return [{"id": "6", "score": 0.99}, {"id": "2", "score": 0.9}]


@pytest.fixture
def searcher(monkeypatch):
    cursor = Cursor()
    instance = search_module.VectorSearcher.__new__(search_module.VectorSearcher)
    instance.config = SimpleNamespace(rerank_enabled=True, rerank_max_candidates=20,
                                      rerank_score_fusion_alpha=None, db_config={})
    instance.embedding_client = SimpleNamespace(embed_texts=lambda texts: [[0.1, 0.2]])
    instance.rerank_client = Reranker()
    instance.logger = logging.getLogger("test-rag-budget")
    conn = SimpleNamespace(cursor=lambda: cursor, close=lambda: None)
    monkeypatch.setattr(search_module.psycopg2, "connect", lambda **kwargs: conn)
    monkeypatch.setattr(search_module, "register_vector", lambda conn: None)
    return instance, cursor


def test_relevant_sixth_candidate_can_enter_final_top_two(searcher):
    instance, cursor = searcher
    stats = {}
    result = instance.search_similar("policy", top_k=2, domain="enterprise_retail", dataset_id="test", diagnostics=stats)
    assert cursor.params[-1] == 20
    assert "AND domain = %s" in cursor.query and "AND dataset_id = %s" in cursor.query
    assert [r["source_id"] for r in result] == ["doc-6", "doc-2"]
    assert len(instance.rerank_client.candidates) == 6
    assert stats["candidate_count"] == 6 and stats["returned_count"] == 2
    assert stats["rerank_applied"] is True
    assert stats["candidate_source_ids"][-1] == "doc-6"
    assert all(stats[key] >= 0 for key in ("embedding_ms", "vector_ms", "rerank_ms", "total_ms"))


def test_dense_comparison_explicitly_bypasses_reranker(searcher):
    instance, cursor = searcher
    result = instance.search_similar("policy", top_k=2, candidate_k=6, rerank=False)
    assert [r["source_id"] for r in result] == ["doc-1", "doc-2"]
    assert cursor.params[-1] == 6
    assert instance.rerank_client.calls == 0


@pytest.mark.parametrize("reply", [None, [{"id": "unknown", "score": 1.0}]])
def test_reranker_failure_or_foreign_ids_is_observable_dense_fallback(searcher, reply):
    instance, _ = searcher
    instance.rerank_client.rerank = lambda *args, **kwargs: reply
    stats = {}
    result = instance.search_similar("policy", top_k=2, diagnostics=stats)
    assert [r["source_id"] for r in result] == ["doc-1", "doc-2"]
    assert stats["rerank_requested"] is True and stats["rerank_applied"] is False


def test_no_reranker_keeps_legacy_dense_budget(searcher):
    instance, cursor = searcher
    instance.rerank_client = None
    instance.config.rerank_enabled = False
    assert len(instance.search_similar("policy", top_k=3)) == 3
    assert cursor.params[-1] == 3


def test_score_fusion_does_not_drop_candidates_omitted_by_reranker(searcher):
    instance, _ = searcher
    instance.config.rerank_score_fusion_alpha = 0.5
    instance.rerank_client.zscore_normalize = lambda values: values
    result = instance.search_similar("policy", top_k=6)
    assert len(result) == 6
    assert {row["source_id"] for row in result} == {f"doc-{n}" for n in range(1, 7)}


def test_compatibility_hybrid_endpoint_reranks_only_once(searcher):
    instance, _ = searcher
    result = instance.hybrid_search("policy", vector_top_k=6, rerank_top_k=2)
    assert instance.rerank_client.calls == 1
    assert [r["source_id"] for r in result["results"]] == ["doc-6", "doc-2"]


def test_search_api_legacy_and_explicit_budgets_remain_compatible(searcher, monkeypatch):
    from kb_service.api import routes
    from kb_service.api.deps import get_config

    instance, cursor = searcher
    monkeypatch.setattr(routes, "VectorSearcher", lambda config: instance)
    app = FastAPI()
    app.include_router(routes.router, prefix="/api/v1/knowledge")
    app.dependency_overrides[get_config] = lambda: instance.config
    with TestClient(app) as client:
        response = client.post("/api/v1/knowledge/search", json={"query": "policy", "top_k": 2})
        assert response.status_code == 200 and response.json()["count"] == 2
        assert response.json()["retrieval"]["candidate_k"] == 20
        response = client.post("/api/v1/knowledge/search", json={"query": "policy", "top_k": 2,
                               "candidate_k": 6, "rerank": False})
        assert response.status_code == 200 and cursor.params[-1] == 6
        assert response.json()["retrieval"]["rerank_applied"] is False
        assert client.post("/api/v1/knowledge/search", json={"query": "q", "candidate_k": 101}).status_code == 422


def test_schema_default_does_not_require_new_fields():
    payload = SearchRequest(query="legacy")
    assert payload.candidate_k is None and payload.rerank is None
