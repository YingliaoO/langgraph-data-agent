"""B-stage Evidence contract and normalization tests."""

from __future__ import annotations

import asyncio

from multisource_data_agent.application.agents.kg_sub_graph.agentic_rag_agents.components.summarize.evidence_context import (
    build_evidence_context,
)
from multisource_data_agent.application.agents.tool_contracts import Evidence, ToolResult
from multisource_data_agent.application.agents.tool_execution.knowledge_rag import KnowledgeRAGTool


class FakePgVector:
    backend_name = "pgvector"

    async def search(self, query, *, top_k, context):
        return [
            {
                "id": "doc-1",
                "content": "grounded document fact",
                "source": "kb/table/1",
                "similarity": 0.73,
                "rerank_score": 0.94,
                "metadata": {"title": "test"},
            }
        ]


def test_evidence_fields_round_trip_without_breaking_legacy_fields() -> None:
    evidence = Evidence(
        kind="document",
        content="fact",
        source="kb/table/1",
        source_id="doc-1",
        rank=1,
        score=0.94,
        retrieval_score=0.73,
        rerank_score=0.94,
        citation="kb/table/1",
    )

    dumped = evidence.model_dump(mode="json")

    assert dumped["source"] == "kb/table/1"
    assert dumped["source_id"] == "doc-1"
    assert dumped["rank"] == 1
    assert dumped["retrieval_score"] == 0.73
    assert dumped["rerank_score"] == 0.94
    assert dumped["citation"] == "kb/table/1"


def test_pgvector_evidence_has_stable_identity_rank_and_score_layers() -> None:
    tool = KnowledgeRAGTool(pgvector_strategy=FakePgVector(), top_k=5)
    result = asyncio.run(tool.execute("find the fact"))

    evidence = result.evidence[0]
    assert evidence.source == "kb/table/1"
    assert evidence.source_id == "doc-1"
    assert evidence.rank == 1
    assert evidence.retrieval_score == 0.73
    assert evidence.rerank_score == 0.94
    assert evidence.score == 0.94


def test_evidence_context_retains_source_and_ranking_metadata() -> None:
    result = ToolResult(
        tool_name="knowledge_rag",
        tool_kind="rag",
        status="success",
        task="find the fact",
        evidence=[
            Evidence(
                kind="document",
                content="grounded document fact",
                source="kb/table/1",
                source_id="doc-1",
                rank=1,
                score=0.94,
                retrieval_score=0.73,
                rerank_score=0.94,
                citation="kb/table/1",
            )
        ],
    )

    context = build_evidence_context([result])
    metadata = context.metadata["evidence"][0]

    assert metadata["source"] == "kb/table/1"
    assert metadata["source_id"] == "doc-1"
    assert metadata["rank"] == 1
    assert metadata["retrieval_score"] == 0.73
    assert metadata["rerank_score"] == 0.94
    assert metadata["citation"] == "kb/table/1"
