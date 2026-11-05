"""Canonical Knowledge RAG tool boundary for the Data Agent.

The core Data Agent uses one retrieval backend: PostgreSQL + pgvector served
by ``kb_ingest``.  Reranking stays inside ``kb_ingest`` so the application
does not rerank a second time.  ``MilvusStrategy`` remains in this module only
as a legacy adapter for historical callers; ``KnowledgeRAGTool`` does not
instantiate or fall back to it.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Dict, List, Optional, Protocol
import time

import aiohttp

from multisource_data_agent.application.agents.observability import elapsed_ms, execution_success, trace_event
from multisource_data_agent.application.agents.tool_contracts import Evidence, ToolResult
from multisource_data_agent.config import settings
from multisource_data_agent.infrastructure.core.logger import get_logger

DEFAULT_KNOWLEDGE_DOMAIN = "enterprise_retail"

logger = get_logger(service="knowledge-rag-tool")


class KnowledgeRetrievalStrategy(Protocol):
    """Internal retrieval backend used by ``KnowledgeRAGTool``."""

    backend_name: str

    async def search(
        self,
        query: str,
        *,
        top_k: int,
        context: Dict[str, Any],
    ) -> List[Dict[str, Any]]:
        ...


class PgVectorStrategy:
    """Primary RAG backend served by the kb_ingest PostgreSQL/pgvector API.

    Reranking, when enabled, belongs to the kb_ingest service.  This strategy
    intentionally does not call the main application's Milvus reranker a
    second time.
    """

    backend_name = "pgvector"

    def __init__(
        self,
        *,
        search_url: Optional[str] = None,
        timeout: Optional[float] = None,
    ) -> None:
        ingest_base = settings.INGEST_SERVICE_URL.rstrip("/") if settings.INGEST_SERVICE_URL else None
        self.search_url = search_url or (
            f"{ingest_base}/api/v1/knowledge/search" if ingest_base else None
        )
        # PGVector is an internal KB dependency and may itself wait for the
        # configured reranker.  Do not reuse the shorter external-search
        # timeout here, otherwise a healthy kb_ingest request can be cancelled
        # before its own reranker timeout expires.
        self.timeout = timeout if timeout is not None else settings.KB_PGVECTOR_REQUEST_TIMEOUT

    async def search(
        self,
        query: str,
        *,
        top_k: int,
        context: Dict[str, Any],
    ) -> List[Dict[str, Any]]:
        if not self.search_url:
            raise RuntimeError("INGEST_SERVICE_URL is not configured for pgvector retrieval")

        similarity_threshold = context.get("postgres_similarity_threshold")
        if similarity_threshold is None:
            similarity_threshold = settings.KB_POSTGRES_SIMILARITY_THRESHOLD

        payload: Dict[str, Any] = {
            "query": query,
            "top_k": top_k,
        }
        if context.get("candidate_k") is not None:
            payload["candidate_k"] = context["candidate_k"]
        retrieval_mode = context.get("retrieval_mode", "dense")
        payload["retrieval_mode"] = retrieval_mode
        if context.get("as_of") is not None:
            payload["as_of"] = str(context["as_of"])
        if similarity_threshold is not None:
            payload["threshold"] = similarity_threshold
        if context.get("domain"):
            payload["domain"] = str(context["domain"])
        if context.get("dataset_id"):
            payload["dataset_id"] = str(context["dataset_id"])

        timeout_cfg = aiohttp.ClientTimeout(total=self.timeout)
        async with aiohttp.ClientSession(timeout=timeout_cfg) as session:
            async with session.post(self.search_url, json=payload) as response:
                if response.status != 200:
                    error_text = await response.text()
                    raise RuntimeError(f"pgvector HTTP {response.status}: {error_text[:500]}")
                body = await response.json()

        raw_results = body.get("results") or []
        if not isinstance(raw_results, list):
            raise RuntimeError("invalid pgvector response: 'results' is not a list")

        results: List[Dict[str, Any]] = []
        for index, item in enumerate(raw_results):
            if not isinstance(item, dict):
                continue
            doc = dict(item)
            metadata = doc.get("metadata")
            doc["metadata"] = dict(metadata) if isinstance(metadata, dict) else {}
            if doc.get("domain"):
                doc["metadata"].setdefault("domain", doc["domain"])
            if doc.get("dataset_id"):
                doc["metadata"].setdefault("dataset_id", doc["dataset_id"])
            if doc.get("source_id"):
                doc["metadata"].setdefault("source_id", doc["source_id"])
            doc["backend"] = self.backend_name
            doc["tool"] = self.backend_name
            doc["id"] = str(
                doc.get("id")
                or doc.get("document_id")
                or doc.get("source_id")
                or f"pgvector_{index}"
            )

            similarity_value = doc.get("similarity")
            if similarity_value is None:
                similarity_value = doc.get("score")
            similarity = _float_or_none(similarity_value)
            if similarity is not None:
                doc["similarity"] = similarity

            rerank_score = _float_or_none(doc.get("rerank_score"))
            if rerank_score is not None:
                doc["rerank_score"] = rerank_score

            if similarity_threshold is not None and similarity is not None:
                # A lexical hit may have low cosine similarity. In hybrid mode
                # only a real rerank score can qualify it independently; raw
                # lexical/RRF scores are never treated as semantic confidence.
                if similarity < float(similarity_threshold) and not (retrieval_mode == "hybrid" and rerank_score is not None):
                    continue
            if rerank_score is not None and rerank_score < settings.KB_POSTGRES_RERANK_THRESHOLD:
                continue

            results.append(doc)

        return results[:top_k]


class MilvusStrategy:
    """Legacy-only Milvus adapter.

    The canonical ``KnowledgeRAGTool`` no longer instantiates or falls back to
    this strategy.  It remains importable until product-scope cleanup removes
    historical file/KB paths that still reference Milvus.
    """

    backend_name = "milvus"

    def __init__(
        self,
        *,
        knowledge_service: Optional[Any] = None,
        service_factory: Optional[Callable[[], Any]] = None,
    ) -> None:
        self._knowledge_service = knowledge_service
        self._service_factory = service_factory or self._default_service_factory

    @staticmethod
    def _default_service_factory() -> Any:
        # Import lazily so the primary pgvector path does not require Milvus at
        # module import / workflow construction time.
        from multisource_data_agent.infrastructure.knowledge import KnowledgeService

        return KnowledgeService()

    def _service(self) -> Any:
        if self._knowledge_service is None:
            self._knowledge_service = self._service_factory()
        return self._knowledge_service

    async def search(
        self,
        query: str,
        *,
        top_k: int,
        context: Dict[str, Any],
    ) -> List[Dict[str, Any]]:
        service = self._service()
        similarity_threshold = context.get("similarity_threshold")
        if similarity_threshold is None:
            similarity_threshold = settings.KB_SIMILARITY_THRESHOLD
        filter_expr = context.get("filter_expr")

        docs = await service.search(
            query=query,
            top_k=top_k,
            similarity_threshold=similarity_threshold,
            filter_expr=filter_expr,
            filter_by_similarity=not service.reranker.enabled,
        )

        results: List[Dict[str, Any]] = []
        for index, item in enumerate(docs or []):
            if not isinstance(item, dict):
                continue
            doc = dict(item)
            metadata = doc.get("metadata")
            doc["metadata"] = dict(metadata) if isinstance(metadata, dict) else {}
            doc["backend"] = self.backend_name
            doc["tool"] = self.backend_name
            doc["id"] = str(
                doc.get("id")
                or doc.get("document_id")
                or doc.get("source_id")
                or f"milvus_{index}"
            )
            results.append(doc)
        return results[:top_k]


@dataclass
class KnowledgeRAGExecution:
    """Internal compatibility payload around the public ``ToolResult``."""

    result: ToolResult
    documents: List[Dict[str, Any]]
    backend: Optional[str]
    attempts: List[Dict[str, Any]]


class KnowledgeRAGTool:
    """Single canonical RAG tool backed only by PostgreSQL + pgvector."""

    tool_name = "knowledge_rag"
    tool_kind = "rag"

    def __init__(
        self,
        *,
        pgvector_strategy: Optional[KnowledgeRetrievalStrategy] = None,
        top_k: Optional[int] = None,
    ) -> None:
        self.pgvector = pgvector_strategy or PgVectorStrategy()
        self.top_k = top_k or settings.KB_TOP_K

    async def execute(
        self,
        task: str,
        context: Optional[Dict[str, Any]] = None,
        *,
        trace: Any = None,
    ) -> ToolResult:
        return (await self.execute_with_details(task, context, trace=trace)).result

    async def execute_with_details(
        self,
        task: str,
        context: Optional[Dict[str, Any]] = None,
        *,
        trace: Any = None,
    ) -> KnowledgeRAGExecution:
        context = dict(context or {})
        # The product context is Enterprise Retail.  Callers that run a
        # historical Recipe regression can explicitly pass ``domain=recipe``.
        context.setdefault("domain", DEFAULT_KNOWLEDGE_DOMAIN)
        question = task or ""
        top_k = int(context.get("top_k") or self.top_k)
        started_at = time.perf_counter()
        trace_event(
            trace,
            "tool_execution",
            "start",
            tool=self.tool_name,
            question=question,
            metadata={"top_k": top_k},
        )

        if not question.strip():
            result = ToolResult(
                tool_name=self.tool_name,
                tool_kind="rag",
                status="empty",
                task=question,
                data=[],
                evidence=[],
                metadata={
                    "backend": None,
                    "fallback_used": False,
                    "attempts": [],
                    "reason": "empty_question",
                },
            )
            self._trace_done(trace, question, result, started_at)
            return KnowledgeRAGExecution(result=result, documents=[], backend=None, attempts=[])

        attempts: List[Dict[str, Any]] = []
        documents: List[Dict[str, Any]] = []
        backend: Optional[str] = None
        backend_name = str(getattr(self.pgvector, "backend_name", "pgvector"))
        backend_started = time.perf_counter()

        trace_event(
            trace,
            "knowledge_retrieval_backend",
            "start",
            tool=self.tool_name,
            question=question,
            metadata={"backend": backend_name, "fallback": False},
        )

        try:
            found = await self.pgvector.search(question, top_k=top_k, context=context)
            documents = list(found or [])
            attempt_status = "success" if documents else "empty"
            attempts.append(
                {
                    "backend": backend_name,
                    "status": attempt_status,
                    "result_count": len(documents),
                }
            )
            trace_event(
                trace,
                "knowledge_retrieval_backend",
                "done",
                success=True,
                tool=self.tool_name,
                question=question,
                latency_ms=elapsed_ms(backend_started),
                fallback=False,
                metadata={
                    "backend": backend_name,
                    "status": attempt_status,
                    "result_count": len(documents),
                },
            )
            if documents:
                backend = backend_name
                status = "success"
            else:
                status = "empty"
        except Exception as exc:  # pragma: no cover - defensive runtime boundary
            error_text = str(exc).strip() or repr(exc)
            logger.warning(
                "Knowledge backend %s failed (%s): %s",
                backend_name,
                type(exc).__name__,
                error_text,
            )
            attempts.append(
                {
                    "backend": backend_name,
                    "status": "error",
                    "result_count": 0,
                    "error": error_text,
                    "error_type": type(exc).__name__,
                }
            )
            trace_event(
                trace,
                "knowledge_retrieval_backend",
                "error",
                success=False,
                tool=self.tool_name,
                question=question,
                latency_ms=elapsed_ms(backend_started),
                error=exc,
                fallback=False,
                metadata={"backend": backend_name, "status": "error"},
            )
            status = "error"

        result = _build_tool_result(
            question=question,
            documents=documents,
            status=status,
            backend=backend,
            attempts=attempts,
            top_k=top_k,
        )
        self._trace_done(trace, question, result, started_at)
        return KnowledgeRAGExecution(
            result=result,
            documents=documents,
            backend=backend,
            attempts=attempts,
        )

    def _trace_done(
        self,
        trace: Any,
        question: str,
        result: ToolResult,
        started_at: float,
    ) -> None:
        trace_event(
            trace,
            "tool_execution",
            "done",
            success=execution_success(result.status),
            tool=self.tool_name,
            question=question,
            latency_ms=elapsed_ms(started_at),
            error=result.metadata.get("error"),
            fallback=False,
            metadata={
                "execution_success": execution_success(result.status),
                "status": result.status,
                "backend": result.metadata.get("backend"),
                "fallback_used": False,
                "result_count": len(result.data or []),
                "attempts": result.metadata.get("attempts", []),
            },
        )

def create_knowledge_rag_node(
    *,
    tool: Optional[KnowledgeRAGTool] = None,
    knowledge_service: Optional[Any] = None,
    top_k: Optional[int] = None,
    pgvector_search_url: Optional[str] = None,
    pgvector_timeout: Optional[float] = None,
) -> Callable[[Dict[str, Any]], Awaitable[Dict[str, Any]]]:
    """Compatibility LangGraph node backed by the canonical RAG tool."""

    # ``knowledge_service`` is intentionally retained in the compatibility
    # signature until Phase 5 removes legacy KB/file callers.  The canonical
    # node is PGVector-only and never initializes Milvus.
    _ = knowledge_service
    tool = tool or KnowledgeRAGTool(
        pgvector_strategy=PgVectorStrategy(
            search_url=pgvector_search_url,
            timeout=pgvector_timeout,
        ),
        top_k=top_k,
    )

    async def knowledge_rag_node(state: Dict[str, Any]) -> Dict[str, Any]:
        question = state.get("task") or state.get("question") or ""
        trace = state.get("eval_trace")
        context: Dict[str, Any] = {
            "top_k": state.get("top_k") or top_k or settings.KB_TOP_K,
            "similarity_threshold": state.get("similarity_threshold"),
            "postgres_similarity_threshold": state.get("postgres_similarity_threshold"),
            "filter_expr": state.get("filter_expr"),
            "domain": state.get("domain") or state.get("knowledge_domain"),
            "dataset_id": state.get("dataset_id") or state.get("knowledge_dataset_id"),
        }
        execution = await tool.execute_with_details(question, context, trace=trace)
        backend = execution.backend
        postgres_results = execution.documents if backend == "pgvector" else []
        milvus_results: List[Dict[str, Any]] = []
        return {
            "postgres_results": postgres_results,
            "milvus_results": milvus_results,
            "local_results": execution.documents,
            "tool_results": [execution.result],
            "steps": ["knowledge_rag"],
            "eval_trace": trace,
        }

    return knowledge_rag_node


def _build_tool_result(
    *,
    question: str,
    documents: List[Dict[str, Any]],
    status: str,
    backend: Optional[str],
    attempts: List[Dict[str, Any]],
    top_k: int,
) -> ToolResult:
    compact_data: List[Dict[str, Any]] = []
    evidence: List[Evidence] = []

    for rank, document in enumerate(documents[:top_k], start=1):
        metadata = document.get("metadata")
        metadata = dict(metadata) if isinstance(metadata, dict) else {}
        source = _document_source(document, metadata)
        source_id = (
            document.get("source_id")
            or metadata.get("source_id")
            or document.get("document_id")
            or document.get("id")
            or source
        )
        retrieval_score = _float_or_none(document.get("similarity"))
        rerank_score = _float_or_none(document.get("rerank_score"))
        score = _document_score(document)
        document_id = source_id
        compact_data.append(
            {
                "id": str(document_id) if document_id is not None else None,
                "source": source,
                "score": score,
                "backend": backend,
            }
        )
        if metadata.get("domain"):
            compact_data[-1]["domain"] = metadata["domain"]
        if metadata.get("dataset_id"):
            compact_data[-1]["dataset_id"] = metadata["dataset_id"]

        content = document.get("content") or document.get("document") or document.get("summary")
        # Evidence is for synthesis, not a UI preview. Paragraph chunks are
        # bounded at ingestion; the HTTP DTO owns its separate preview limits.
        snippet = str(content) if content is not None else ""
        if snippet.strip():
            evidence_metadata = dict(metadata)
            if document.get("domain"):
                evidence_metadata.setdefault("domain", document["domain"])
            if document.get("dataset_id"):
                evidence_metadata.setdefault("dataset_id", document["dataset_id"])
            if source_id is not None:
                evidence_metadata.setdefault("source_id", str(source_id))
            if backend:
                evidence_metadata.setdefault("backend", backend)
            evidence.append(
                Evidence(
                    kind="document",
                    content=snippet,
                    source=source,
                    source_id=str(source_id) if source_id is not None else source,
                    rank=rank,
                    score=score,
                    retrieval_score=retrieval_score,
                    rerank_score=rerank_score,
                    citation=source,
                    metadata=evidence_metadata,
                )
            )

    errors = [item.get("error") for item in attempts if item.get("error")]
    resolved_domain = next(
        (doc.get("domain") or (doc.get("metadata") or {}).get("domain") for doc in documents),
        None,
    )
    resolved_dataset = next(
        (doc.get("dataset_id") or (doc.get("metadata") or {}).get("dataset_id") for doc in documents),
        None,
    )
    return ToolResult(
        tool_name="knowledge_rag",
        tool_kind="rag",
        status=status,  # type: ignore[arg-type]
        task=question,
        data=compact_data,
        evidence=evidence,
        metadata={
            "backend": backend,
            "fallback_used": False,
            "attempts": attempts,
            "top_k": top_k,
            "result_count": len(documents),
            "domain": resolved_domain,
            "dataset_id": resolved_dataset,
            **({"error": "; ".join(str(item) for item in errors)} if status == "error" and errors else {}),
        },
    )


def _document_source(document: Dict[str, Any], metadata: Dict[str, Any]) -> Optional[str]:
    source_table = document.get("source_table") or metadata.get("source_table")
    source_id = document.get("source_id") or metadata.get("source_id")
    source = (
        document.get("source")
        or metadata.get("source")
        or metadata.get("url")
        or (f"{source_table}/{source_id}" if source_table and source_id else None)
        or document.get("document_id")
        or source_id
        or metadata.get("recipe_id")
        or metadata.get("title")
        or document.get("id")
    )
    return str(source) if source is not None else None


def _document_score(document: Dict[str, Any]) -> Optional[float]:
    for field in ("rerank_score", "similarity", "score"):
        value = _float_or_none(document.get(field))
        if value is not None:
            return value
    return None


def _float_or_none(value: Any) -> Optional[float]:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


__all__ = [
    "KnowledgeRAGExecution",
    "KnowledgeRAGTool",
    "KnowledgeRetrievalStrategy",
    "MilvusStrategy",
    "PgVectorStrategy",
    "create_knowledge_rag_node",
]
