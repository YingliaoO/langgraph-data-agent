import asyncio
from pathlib import Path
from typing import Any, Dict, List

from multisource_data_agent.application.agents.tool_execution.knowledge_rag import (
    KnowledgeRAGTool,
    PgVectorStrategy,
    create_knowledge_rag_node,
)
from multisource_data_agent.config import settings


class FakeStrategy:
    def __init__(self, backend_name: str, *, documents=None, error: Exception | None = None):
        self.backend_name = backend_name
        self.documents = list(documents or [])
        self.error = error
        self.calls = 0

    async def search(self, query: str, *, top_k: int, context: Dict[str, Any]) -> List[Dict[str, Any]]:
        self.calls += 1
        if self.error is not None:
            raise self.error
        return self.documents[:top_k]


def run(coro):
    return asyncio.run(coro)


def sample_doc(doc_id: str, *, content: str = "宫保鸡丁的历史记载", source: str = "kb/1", score: float = 0.91):
    return {
        "id": doc_id,
        "content": content,
        "source": source,
        "similarity": score,
        "metadata": {"title": "宫保鸡丁"},
    }


def test_pgvector_is_the_only_canonical_backend_on_hit():
    pgvector = FakeStrategy("pgvector", documents=[sample_doc("pg-1")])
    tool = KnowledgeRAGTool(pgvector_strategy=pgvector, top_k=5)

    result = run(tool.execute("宫保鸡丁的历史"))

    assert result.tool_name == "knowledge_rag"
    assert result.tool_kind == "rag"
    assert result.status == "success"
    assert result.metadata["backend"] == "pgvector"
    assert result.metadata["fallback_used"] is False
    assert result.metadata["attempts"] == [
        {"backend": "pgvector", "status": "success", "result_count": 1}
    ]
    assert pgvector.calls == 1
    assert not hasattr(tool, "milvus")
    assert result.data == [{"id": "pg-1", "source": "kb/1", "score": 0.91, "backend": "pgvector"}]
    assert len(result.evidence) == 1
    assert result.evidence[0].source == "kb/1"


def test_pgvector_empty_returns_empty_without_fallback():
    pgvector = FakeStrategy("pgvector", documents=[])
    tool = KnowledgeRAGTool(pgvector_strategy=pgvector)

    execution = run(tool.execute_with_details("不存在的知识"))

    assert execution.result.status == "empty"
    assert execution.backend is None
    assert execution.result.data == []
    assert execution.result.evidence == []
    assert execution.result.metadata["fallback_used"] is False
    assert execution.attempts == [
        {"backend": "pgvector", "status": "empty", "result_count": 0}
    ]
    assert pgvector.calls == 1


def test_pgvector_error_returns_error_without_fallback():
    pgvector = FakeStrategy("pgvector", error=RuntimeError("pg unavailable"))
    tool = KnowledgeRAGTool(pgvector_strategy=pgvector)

    execution = run(tool.execute_with_details("川菜发展史"))

    assert execution.result.status == "error"
    assert execution.backend is None
    assert execution.result.metadata["fallback_used"] is False
    assert execution.attempts[0]["backend"] == "pgvector"
    assert execution.attempts[0]["status"] == "error"
    assert execution.attempts[0]["error"] == "pg unavailable"
    assert execution.attempts[0]["error_type"] == "RuntimeError"
    assert "pg unavailable" in execution.result.metadata["error"]


def test_empty_question_skips_pgvector_call():
    pgvector = FakeStrategy("pgvector", documents=[sample_doc("unused")])
    tool = KnowledgeRAGTool(pgvector_strategy=pgvector)

    result = run(tool.execute("   "))

    assert result.status == "empty"
    assert result.metadata["reason"] == "empty_question"
    assert result.metadata["attempts"] == []
    assert result.metadata["fallback_used"] is False
    assert pgvector.calls == 0


def test_compatibility_node_preserves_legacy_result_buckets_without_milvus_results():
    pgvector = FakeStrategy("pgvector", documents=[sample_doc("pg-2")])
    tool = KnowledgeRAGTool(pgvector_strategy=pgvector)
    node = create_knowledge_rag_node(tool=tool, top_k=5)

    state = run(node({"question": "宫保鸡丁历史", "eval_trace": None}))

    assert len(state["postgres_results"]) == 1
    assert state["milvus_results"] == []
    assert len(state["local_results"]) == 1
    assert len(state["tool_results"]) == 1
    assert state["tool_results"][0].tool_name == "knowledge_rag"
    assert state["tool_results"][0].metadata["backend"] == "pgvector"
    assert state["tool_results"][0].metadata["fallback_used"] is False


def test_canonical_node_does_not_construct_milvus_from_legacy_service_argument():
    class LegacyServiceBomb:
        @property
        def reranker(self):
            raise AssertionError("legacy Milvus service must not be touched")

        async def search(self, *args, **kwargs):
            raise AssertionError("legacy Milvus service must not be called")

    source = Path(
        "multisource_data_agent/application/agents/tool_execution/knowledge_rag.py"
    ).read_text(encoding="utf-8")

    assert "self.milvus =" not in source
    assert "for index, strategy in enumerate((self.pgvector, self.milvus))" not in source
    assert "milvus_strategy=MilvusStrategy" not in source

    # Signature compatibility is retained, but the value is intentionally ignored.
    node = create_knowledge_rag_node(
        knowledge_service=LegacyServiceBomb(),
        pgvector_search_url="http://kb-ingest.test/search",
    )
    assert callable(node)


def test_legacy_kb_workflow_no_longer_performs_main_app_pgvector_rerank():
    source = Path(
        "multisource_data_agent/application/agents/kg_sub_graph/agentic_rag_agents/workflows/multi_agent/multi_tool.py"
    ).read_text(encoding="utf-8")

    assert "knowledge_service.reranker.rerank" not in source
    assert "knowledge_service = knowledge_service or KnowledgeService()" not in source
    assert "create_knowledge_rag_node" in source


def test_pgvector_uses_dedicated_internal_service_timeout():
    strategy = PgVectorStrategy(search_url="http://kb-ingest.test/search")
    assert strategy.timeout == settings.KB_PGVECTOR_REQUEST_TIMEOUT


def test_backend_error_metadata_is_non_empty_for_empty_string_exceptions():
    pgvector = FakeStrategy("pgvector", error=TimeoutError())
    tool = KnowledgeRAGTool(pgvector_strategy=pgvector)

    execution = run(tool.execute_with_details("测试超时"))

    assert execution.result.status == "error"
    assert execution.attempts[0]["status"] == "error"
    assert execution.attempts[0]["error"]
    assert execution.attempts[0]["error_type"] == "TimeoutError"
    assert execution.result.metadata["fallback_used"] is False


def test_rerank_score_is_preferred_for_evidence_score():
    document = sample_doc("pg-3", score=0.72)
    document["rerank_score"] = 0.96
    pgvector = FakeStrategy("pgvector", documents=[document])
    tool = KnowledgeRAGTool(pgvector_strategy=pgvector)

    result = run(tool.execute("宫保鸡丁历史"))

    assert result.status == "success"
    assert result.data[0]["score"] == 0.96
    assert result.evidence[0].score == 0.96
