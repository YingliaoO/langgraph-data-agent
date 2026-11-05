"""Independent lexical recall, shared filters and conservative acceptance."""

import asyncio
import logging
from pathlib import Path
import sys
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "kb_ingest"))
from kb_service.services import search
from kb_service.services.documents import lexical_query, lexical_text
from multisource_data_agent.application.agents.tool_execution.knowledge_rag import PgVectorStrategy
from multisource_data_agent.config import settings


def document(identifier, score=0.9):
    return {"id": identifier, "source_id": identifier, "similarity": score, "metadata": {}}


def test_rrf_uses_ranks_and_deduplicates_without_mixing_raw_scores():
    dense = [document("a", 0.99), document("b", 0.01)]
    lexical = [document("b", 1000), document("c", 1)]
    result = search.reciprocal_rank_fusion(dense, lexical, 2)
    assert [r["id"] for r in result] == ["b", "a"]
    assert result[0]["dense_rank"] == 2 and result[0]["lexical_rank"] == 1
    dense[0]["similarity"], lexical[0]["similarity"] = -1000, 0
    assert [r["id"] for r in search.reciprocal_rank_fusion(dense, lexical, 2)] == ["b", "a"]


def test_mixed_language_tokenization_is_shared_and_has_no_query_operators():
    text = lexical_text("KB-42 退货期限 2026.10")
    assert {"kb", "42", "kb42", "退货", "期限", "202610"} <= set(text.split())
    query = lexical_query("KB-42 退货'; DROP TABLE data --")
    assert '"kb42"' in query and ";" not in query and "--" not in query
    assert len(lexical_query(" ".join(f"word{n}" for n in range(100))).split(" OR ")) <= 64


def test_hybrid_recalls_document_missing_from_dense_and_keeps_all_scope_filters(monkeypatch):
    queries = []
    class Cursor:
        def __enter__(self): return self
        def __exit__(self, *args): return False
        def execute(self, sql, params=None):
            self.sql = sql
            if params is not None:
                queries.append((sql, params))
        def fetchall(self):
            if "search_vector @@" in self.sql:
                return [(2, "policies", "target", {}, "Original target clause", "retail", "v1", 0.2, 0.8,
                         {"content_origin": "original", "document_version": "2"}, 0.9)]
            return [(1, "policies", "other", {}, "Other clause", "retail", "v1", 0.9, 0.1, {})]
    class Ranker:
        calls = 0
        def rerank(self, query, candidates):
            self.calls += 1
            assert {c["id"] for c in candidates} == {"1", "2"}
            return [{"id": "2", "score": 0.99}, {"id": "1", "score": 0.2}]
    instance = search.VectorSearcher.__new__(search.VectorSearcher)
    instance.config = SimpleNamespace(db_config={}, rerank_enabled=True, rerank_max_candidates=20, rerank_score_fusion_alpha=None)
    instance.embedding_client = SimpleNamespace(embed_texts=lambda texts: [[0.1, 0.2]])
    instance.rerank_client = Ranker()
    instance.logger = logging.getLogger("hybrid-test")
    monkeypatch.setattr(search.psycopg2, "connect", lambda **kwargs: SimpleNamespace(cursor=lambda: Cursor(), close=lambda: None))
    monkeypatch.setattr(search, "register_vector", lambda conn: None)
    stats = {}
    result = instance.search_similar("KB-42 退货", top_k=1, candidate_k=2, threshold=0.5,
        retrieval_mode="hybrid", domain="retail", dataset_id="v1", source_tables=["policies"],
        as_of="2026-10-15", diagnostics=stats)
    assert result[0]["source_id"] == "target"
    assert result[0]["metadata"]["document_version"] == "2"
    assert instance.rerank_client.calls == 1
    assert stats["dense_candidate_source_ids"] == ["other"] and stats["lexical_candidate_source_ids"] == ["target"]
    assert stats["lexical_applied"] is True
    assert len(queries) == 2
    for sql, params in queries:
        assert "AND domain = %s" in sql and "AND dataset_id = %s" in sql and "source_table = ANY(%s)" in sql
        assert "effective_from" in sql and "effective_to" in sql
        assert "retail" in params and "v1" in params and params.count("2026-10-15") == 2
    assert ">= %s::float8" in queries[0][0] and ">= %s::float8" not in queries[1][0]


@pytest.mark.parametrize("mode,rerank_score,accepted", [("dense",0.99,False),("hybrid",0.99,True),
                                                       ("hybrid",None,False),("hybrid",0.1,False)])
def test_low_cosine_lexical_hit_requires_real_qualified_rerank_score(monkeypatch, mode, rerank_score, accepted):
    class Response:
        status = 200
        async def __aenter__(self): return self
        async def __aexit__(self, *args): return False
        async def json(self):
            return {"results": [{"source_id": "POL-1", "content": "Exact policy", "similarity": 0.2,
                                "rerank_score": rerank_score, "metadata": {}}]}
    class Session:
        payload = None
        def __init__(self, *args, **kwargs): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *args): return False
        def post(self, url, json, headers):
            assert "X-Knowledge-Service-Key" in headers
            Session.payload = json
            return Response()
    monkeypatch.setattr("multisource_data_agent.application.agents.tool_execution.knowledge_rag.aiohttp.ClientSession", Session)
    monkeypatch.setattr(settings, "KB_POSTGRES_RERANK_THRESHOLD", 0.8)
    strategy = PgVectorStrategy(search_url="http://test/search")
    docs = asyncio.run(strategy.search("policy", top_k=3, context={"retrieval_mode":mode,
        "domain":"retail", "dataset_id":"v1", "as_of":"2026-10-15", "postgres_similarity_threshold":0.5}))
    assert bool(docs) is accepted
    assert Session.payload["as_of"] == "2026-10-15" and Session.payload["retrieval_mode"] == mode
