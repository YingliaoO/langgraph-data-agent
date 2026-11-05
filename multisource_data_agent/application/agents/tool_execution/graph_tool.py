"""Canonical Neo4j graph-tool boundary for the Data Agent.

Phase 1 converges the two historical graph execution paths -- predefined
Cypher templates and dynamic Text2Cypher -- behind one application-level tool.
The existing planner/tool-selection node names remain in place as compatibility
routing labels until Phase 2, but both now execute the same ``GraphTool`` and
produce the shared ``ToolResult`` contract.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
import re
from threading import Lock
import time
from typing import Any, Awaitable, Callable, Dict, List, Literal, Optional, Protocol

from multisource_data_agent.application.agents.observability import elapsed_ms, execution_success, trace_event
from multisource_data_agent.application.agents.tool_contracts import Evidence, Statement, ToolResult
from multisource_data_agent.config import settings
from multisource_data_agent.infrastructure.core.logger import get_logger

logger = get_logger(service="graph-tool")

GraphStrategyName = Literal["predefined", "text2cypher", "auto"]
_EMPTY_GRAPH_DISPLAY = [{"error": "在数据库中找不到任何相关信息。"}]


def _error_text(exc: BaseException) -> str:
    return str(exc).strip() or repr(exc)


def _clean_cypher(statement: Any) -> str:
    text = str(statement or "").strip()
    if text.startswith("```"):
        text = text.strip("`").strip()
        if text.lower().startswith("cypher"):
            text = text[6:].strip()
    return text


def _record_count(data: Any) -> int:
    return len(data) if isinstance(data, list) else 0


def _errors_from_result(result: ToolResult) -> List[str]:
    errors = result.metadata.get("errors") if isinstance(result.metadata, dict) else None
    if isinstance(errors, list):
        return [str(item) for item in errors if item]
    error = result.metadata.get("error") if isinstance(result.metadata, dict) else None
    return [str(error)] if error else []


class GraphExecutionStrategy(Protocol):
    """Internal GraphTool strategy contract."""

    strategy_name: str

    async def execute(
        self,
        task: str,
        *,
        context: Dict[str, Any],
    ) -> ToolResult:
        ...


class PredefinedCypherStrategy:
    """Fast graph path backed by the project's predefined Cypher templates."""

    strategy_name = "predefined"

    def __init__(
        self,
        *,
        graph: Any = None,
        graph_factory: Optional[Callable[[], Any]] = None,
        predefined_cypher_dict: Optional[Dict[str, str]] = None,
        matcher: Any = None,
        llm: Any = None,
    ) -> None:
        self._graph = graph
        self._initialization_lock = Lock()
        self._graph_factory = graph_factory or self._default_graph_factory
        self._predefined_cypher_dict = predefined_cypher_dict
        self._matcher = matcher
        self._llm = llm

    @staticmethod
    def _default_graph_factory() -> Any:
        from multisource_data_agent.application.agents.kg_sub_graph.kg_neo4j_conn import get_neo4j_graph

        return get_neo4j_graph()

    def _get_graph(self) -> Any:
        if self._graph is None:
            with self._initialization_lock:
                if self._graph is None:
                    self._graph = self._graph_factory()
        return self._graph

    def _templates(self) -> Dict[str, str]:
        if self._predefined_cypher_dict is None:
            from multisource_data_agent.application.agents.kg_sub_graph.agentic_rag_agents.components.predefined_cypher.cypher_dict import (
                predefined_cypher_dict,
            )

            self._predefined_cypher_dict = predefined_cypher_dict
        return self._predefined_cypher_dict

    def _get_matcher(self) -> Any:
        if self._matcher is not None:
            return self._matcher
        with self._initialization_lock:
            if self._matcher is not None:
                return self._matcher
            from multisource_data_agent.application.agents.kg_sub_graph.agentic_rag_agents.components.predefined_cypher.descriptions import (
                QUERY_DESCRIPTIONS,
            )
            from multisource_data_agent.application.agents.kg_sub_graph.agentic_rag_agents.components.predefined_cypher.utils import (
                create_vector_query_matcher,
            )

            self._matcher = create_vector_query_matcher(self._templates(), QUERY_DESCRIPTIONS)
        return self._matcher

    def _get_llm(self) -> Any:
        if self._llm is not None:
            return self._llm
        with self._initialization_lock:
            if self._llm is not None:
                return self._llm
            from langchain_openai import ChatOpenAI

            kwargs: Dict[str, Any] = {
                "model": getattr(settings, "OPENAI_MODEL", "gpt-4o-mini"),
                "temperature": 0,
            }
            api_key = getattr(settings, "OPENAI_API_KEY", None)
            if api_key:
                kwargs["openai_api_key"] = api_key
            api_base = getattr(settings, "OPENAI_API_BASE", None)
            if api_base:
                kwargs["openai_api_base"] = api_base
            from multisource_data_agent.application.agents.llm_observability import LLMObservationCallback
            self._llm = ChatOpenAI(callbacks=[LLMObservationCallback()], **kwargs)
        return self._llm

    async def execute(
        self,
        task: str,
        *,
        context: Dict[str, Any],
    ) -> ToolResult:
        templates = self._templates()
        matcher = await asyncio.to_thread(self._get_matcher)

        nested = context.get("query_parameters")
        incoming: Dict[str, Any] = dict(nested) if isinstance(nested, dict) else dict(context)
        candidate_name = incoming.get("query") or context.get("query") or context.get("query_name")
        query_name = str(candidate_name or "").strip()
        if query_name not in templates:
            query_name = ""

        match_score: Optional[float] = None
        if not query_name:
            matches = await asyncio.to_thread(matcher.match_query, task, top_k=1)
            if matches:
                query_name = str(matches[0].get("query_name") or "")
                try:
                    match_score = float(matches[0].get("similarity"))
                except (TypeError, ValueError):
                    match_score = None

        if not query_name:
            return ToolResult(
                tool_name="graph_tool",
                tool_kind="graph",
                status="error",
                task=task,
                metadata={
                    "strategy": self.strategy_name,
                    "errors": ["无法为当前问题匹配到预定义查询。"],
                    "reason": "no_predefined_match",
                },
            )

        statement = _clean_cypher(templates.get(query_name))
        if not statement:
            return ToolResult(
                tool_name="graph_tool",
                tool_kind="graph",
                status="error",
                task=task,
                metadata={
                    "strategy": self.strategy_name,
                    "query_name": query_name,
                    "errors": [f"未找到对应的 Cypher 模板：{query_name}"],
                    "reason": "missing_template",
                },
            )

        required_params = {name for name in re.findall(r"\$(\w+)", statement)}
        parameters = incoming.get("parameters")
        parameters = dict(parameters) if isinstance(parameters, dict) else {}
        if required_params and not parameters:
            llm = await asyncio.to_thread(self._get_llm)
            parameters = await asyncio.to_thread(
                matcher.extract_parameters, task, query_name, llm=llm
            ) or {}
        parameters = {str(key): value for key, value in parameters.items()}

        missing = [name for name in sorted(required_params) if not parameters.get(name)]
        if missing:
            return ToolResult(
                tool_name="graph_tool",
                tool_kind="graph",
                status="error",
                task=task,
                statement=Statement(language="cypher", text=statement, parameters=parameters),
                metadata={
                    "strategy": self.strategy_name,
                    "query_name": query_name,
                    "match_score": match_score,
                    "errors": [f"缺少查询参数: {', '.join(missing)}"],
                    "reason": "missing_parameters",
                },
            )

        try:
            # Keep separate await boundaries: cancellation during initialization
            # or parameter extraction must not start a subsequent database query.
            # Cancelling an await cannot stop synchronous I/O already in a worker.
            graph = await asyncio.to_thread(self._get_graph)
            records = await asyncio.to_thread(
                graph.query, query=statement, params=parameters
            ) or []
        except Exception as exc:
            logger.exception("Predefined Cypher query failed: {}", exc)
            error = _error_text(exc)
            return ToolResult(
                tool_name="graph_tool",
                tool_kind="graph",
                status="error",
                task=task,
                statement=Statement(language="cypher", text=statement, parameters=parameters),
                metadata={
                    "strategy": self.strategy_name,
                    "query_name": query_name,
                    "match_score": match_score,
                    "errors": [error],
                    "error": error,
                    "error_type": type(exc).__name__,
                },
            )

        records = records if isinstance(records, list) else []
        status = "success" if records else "empty"
        return ToolResult(
            tool_name="graph_tool",
            tool_kind="graph",
            status=status,
            task=task,
            statement=Statement(language="cypher", text=statement, parameters=parameters),
            data=records,
            evidence=[
                Evidence(
                    kind="graph_record",
                    content=record,
                    source="neo4j",
                    source_id=f"neo4j:{query_name or self.strategy_name}:{index + 1}",
                    rank=index + 1,
                    citation=f"Neo4j record {index + 1}",
                    metadata={
                        "row_index": index,
                        "strategy": self.strategy_name,
                        "query_name": query_name,
                    },
                )
                for index, record in enumerate(records)
            ],
            metadata={
                "strategy": self.strategy_name,
                "query_name": query_name,
                "match_score": match_score,
                "errors": [],
            },
        )


class Text2CypherStrategy:
    """Dynamic graph path using generation, validation and Neo4j execution."""

    strategy_name = "text2cypher"

    def __init__(
        self,
        *,
        graph: Any = None,
        graph_factory: Optional[Callable[[], Any]] = None,
        llm: Any = None,
        cypher_example_retriever: Any = None,
        llm_cypher_validation: bool = True,
    ) -> None:
        self._graph = graph
        self._initialization_lock = Lock()
        self._graph_factory = graph_factory or self._default_graph_factory
        self._llm = llm
        self._cypher_example_retriever = cypher_example_retriever
        self.llm_cypher_validation = llm_cypher_validation

    @staticmethod
    def _default_graph_factory() -> Any:
        from multisource_data_agent.application.agents.kg_sub_graph.kg_neo4j_conn import get_neo4j_graph

        return get_neo4j_graph()

    def _get_graph(self) -> Any:
        if self._graph is None:
            with self._initialization_lock:
                if self._graph is None:
                    self._graph = self._graph_factory()
        return self._graph

    def _get_llm(self) -> Any:
        if self._llm is not None:
            return self._llm
        with self._initialization_lock:
            if self._llm is not None:
                return self._llm
            from langchain_openai import ChatOpenAI

            kwargs: Dict[str, Any] = {
                "model": getattr(settings, "OPENAI_MODEL", "gpt-4o-mini"),
                "temperature": 0.7,
                "tags": ["graph_tool"],
            }
            api_key = getattr(settings, "OPENAI_API_KEY", None)
            if api_key:
                kwargs["openai_api_key"] = api_key
            api_base = getattr(settings, "OPENAI_API_BASE", None)
            if api_base:
                kwargs["openai_api_base"] = api_base
            from multisource_data_agent.application.agents.llm_observability import LLMObservationCallback
            self._llm = ChatOpenAI(callbacks=[LLMObservationCallback()], **kwargs)
        return self._llm

    def _get_retriever(self) -> Any:
        if self._cypher_example_retriever is None:
            from multisource_data_agent.application.agents.kg_sub_graph.agentic_rag_agents.retrievers.cypher_examples.retail_retriever import (
                RetailCypherExampleRetriever,
            )

            self._cypher_example_retriever = RetailCypherExampleRetriever()
        return self._cypher_example_retriever

    async def execute(
        self,
        task: str,
        *,
        context: Dict[str, Any],
    ) -> ToolResult:
        if not task.strip():
            return ToolResult(
                tool_name="graph_tool",
                tool_kind="graph",
                status="error",
                task=task,
                metadata={
                    "strategy": self.strategy_name,
                    "errors": ["未提供查询文本"],
                },
            )

        from multisource_data_agent.application.agents.kg_sub_graph.agentic_rag_agents.components.cypher_tools.utils import (
            create_text2cypher_execution_node,
            create_text2cypher_generation_node,
            create_text2cypher_validation_node,
            validate_no_writes_in_cypher_query,
        )

        graph = await asyncio.to_thread(self._get_graph)
        llm = await asyncio.to_thread(self._get_llm)
        state: Dict[str, Any] = {
            "task": task,
            "steps": list(context.get("steps") or []),
        }

        generation = create_text2cypher_generation_node(
            llm=llm,
            graph=graph,
            cypher_example_retriever=self._get_retriever(),
        )
        generated = await generation(state)
        statement = _clean_cypher(
            generated.get("statement") if isinstance(generated, dict) else generated
        )
        if isinstance(generated, dict):
            state["steps"].extend(
                step for step in generated.get("steps", []) if step not in state["steps"]
            )

        if not statement:
            return ToolResult(
                tool_name="graph_tool",
                tool_kind="graph",
                status="error",
                task=task,
                metadata={
                    "strategy": self.strategy_name,
                    "errors": ["failed to generate Cypher query"],
                    "reason": "generation_empty",
                    "steps": state["steps"],
                },
            )

        state["statement"] = statement
        validation = create_text2cypher_validation_node(
            llm=llm,
            graph=graph,
            llm_validation=bool(context.get("llm_cypher_validation", self.llm_cypher_validation)),
        )
        validation_result = await validation(state)
        executed_statement = _clean_cypher(
            validation_result.get("statement") if isinstance(validation_result, dict) else statement
        ) or statement
        validation_errors = (
            list(validation_result.get("errors") or [])
            if isinstance(validation_result, dict)
            else []
        )

        # The legacy validator can ask the LLM to correct a rejected query.
        # Enforce read-only policy again on the final corrected statement before
        # it reaches Neo4j so a correction can never introduce a write clause.
        final_write_errors = validate_no_writes_in_cypher_query(executed_statement)
        if final_write_errors:
            errors = [str(item) for item in final_write_errors]
            return ToolResult(
                tool_name="graph_tool",
                tool_kind="graph",
                status="error",
                task=task,
                statement=Statement(language="cypher", text=executed_statement),
                metadata={
                    "strategy": self.strategy_name,
                    "errors": errors,
                    "error": "; ".join(errors),
                    "reason": "write_clause_rejected",
                    "generated_statement": statement,
                    "steps": state["steps"],
                },
            )

        execution = create_text2cypher_execution_node(
            graph=graph,
            cypher=validation_result,
        )
        final_result = await execution(state)
        cyphers = (final_result.get("cyphers") or []) if isinstance(final_result, dict) else []
        first = cyphers[0] if cyphers else {}
        if hasattr(first, "model_dump"):
            first = first.model_dump()
        if not isinstance(first, dict):
            first = {}

        records_payload = first.get("records") or []
        result_errors = list(first.get("errors") or [])
        records = [] if records_payload == _EMPTY_GRAPH_DISPLAY else records_payload
        records = records if isinstance(records, list) else []

        # The legacy execution node carries pre-execution validation errors into
        # CypherOutputState even after a corrected/read-only statement executes
        # successfully. Those errors are stale at this point: execution exceptions
        # propagate, while a returned ``cyphers`` payload proves Neo4j accepted the
        # final statement. Preserve them as diagnostics instead of turning valid
        # graph data into ``status=error``.
        validation_warnings = [
            str(item) for item in (result_errors or validation_errors) if item
        ]
        status = "success" if records else "empty" if cyphers else "error"
        executed_statement = _clean_cypher(first.get("statement")) or executed_statement

        return ToolResult(
            tool_name="graph_tool",
            tool_kind="graph",
            status=status,
            task=task,
            statement=Statement(language="cypher", text=executed_statement),
            data=records,
            evidence=[
                Evidence(
                    kind="graph_record",
                    content=record,
                    source="neo4j",
                    source_id=f"neo4j:{self.strategy_name}:{index + 1}",
                    rank=index + 1,
                    citation=f"Neo4j record {index + 1}",
                    metadata={"row_index": index, "strategy": self.strategy_name},
                )
                for index, record in enumerate(records)
            ] if status == "success" else [],
            metadata={
                "strategy": self.strategy_name,
                "errors": [],
                "validation_warnings": validation_warnings,
                "steps": first.get("steps") or final_result.get("steps") or state.get("steps", []),
                "generated_statement": statement,
            },
        )


@dataclass
class GraphToolExecution:
    """Detailed result retained for compatibility adapters and observability."""

    result: ToolResult
    strategy: Optional[str]
    attempts: List[Dict[str, Any]]


class GraphTool:
    """Single graph tool with predefined and dynamic Text2Cypher strategies."""

    tool_name = "graph_tool"
    tool_kind = "graph"

    def __init__(
        self,
        *,
        graph: Any = None,
        llm: Any = None,
        predefined_cypher_dict: Optional[Dict[str, str]] = None,
        cypher_example_retriever: Any = None,
        llm_cypher_validation: bool = True,
        predefined_strategy: Optional[GraphExecutionStrategy] = None,
        text2cypher_strategy: Optional[GraphExecutionStrategy] = None,
    ) -> None:
        self.predefined = predefined_strategy or PredefinedCypherStrategy(
            graph=graph,
            predefined_cypher_dict=predefined_cypher_dict,
            llm=llm,
        )
        self.text2cypher = text2cypher_strategy or Text2CypherStrategy(
            graph=graph,
            llm=llm,
            cypher_example_retriever=cypher_example_retriever,
            llm_cypher_validation=llm_cypher_validation,
        )

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
    ) -> GraphToolExecution:
        context = dict(context or {})
        requested = str(context.get("strategy") or "auto").strip().lower()
        aliases = {
            "dynamic": "text2cypher",
            "cypher_query": "text2cypher",
            "predefined_cypher": "predefined",
        }
        requested = aliases.get(requested, requested)
        if requested not in {"predefined", "text2cypher", "auto"}:
            result = ToolResult(
                tool_name=self.tool_name,
                tool_kind="graph",
                status="error",
                task=task,
                metadata={"error": f"unsupported graph strategy: {requested}"},
            )
            return GraphToolExecution(result=result, strategy=None, attempts=[])

        started_at = time.perf_counter()
        trace_event(
            trace,
            "tool_execution",
            "start",
            tool=self.tool_name,
            question=task,
            metadata={"requested_strategy": requested},
        )

        if not task.strip():
            result = ToolResult(
                tool_name=self.tool_name,
                tool_kind="graph",
                status="error",
                task=task,
                metadata={"error": "graph task is empty", "requested_strategy": requested},
            )
            self._trace_done(trace, task, result, started_at)
            return GraphToolExecution(result=result, strategy=None, attempts=[])

        strategies: List[GraphExecutionStrategy]
        if requested == "predefined":
            strategies = [self.predefined]
        elif requested == "text2cypher":
            strategies = [self.text2cypher]
        else:
            strategies = [self.predefined, self.text2cypher]

        attempts: List[Dict[str, Any]] = []
        final_result: Optional[ToolResult] = None
        final_strategy: Optional[str] = None

        for index, strategy in enumerate(strategies):
            strategy_name = strategy.strategy_name
            final_strategy = strategy_name
            strategy_started = time.perf_counter()
            trace_event(
                trace,
                "graph_strategy",
                "start",
                tool=self.tool_name,
                question=task,
                fallback=index > 0,
                metadata={"strategy": strategy_name},
            )
            try:
                candidate = await strategy.execute(task, context=context)
                candidate = self._normalize_candidate(candidate, strategy_name)
                attempt = {
                    "strategy": strategy_name,
                    "status": candidate.status,
                    "result_count": _record_count(candidate.data),
                }
                errors = _errors_from_result(candidate)
                if errors:
                    attempt["error"] = "; ".join(errors)
                attempts.append(attempt)
                trace_event(
                    trace,
                    "graph_strategy",
                    "done" if candidate.status != "error" else "error",
                    success=execution_success(candidate.status),
                    tool=self.tool_name,
                    question=task,
                    latency_ms=elapsed_ms(strategy_started),
                    error=attempt.get("error"),
                    fallback=index > 0,
                    metadata={
                        "strategy": strategy_name,
                        "status": candidate.status,
                        "result_count": _record_count(candidate.data),
                    },
                )
                final_result = candidate
                final_strategy = strategy_name
                if candidate.status in {"success", "empty"}:
                    break
            except Exception as exc:  # pragma: no cover - defensive boundary
                error = _error_text(exc)
                logger.exception("Graph strategy {} failed: {}", strategy_name, exc)
                attempts.append(
                    {
                        "strategy": strategy_name,
                        "status": "error",
                        "result_count": 0,
                        "error": error,
                        "error_type": type(exc).__name__,
                    }
                )
                trace_event(
                    trace,
                    "graph_strategy",
                    "error",
                    success=False,
                    tool=self.tool_name,
                    question=task,
                    latency_ms=elapsed_ms(strategy_started),
                    error=error,
                    fallback=index + 1 < len(strategies),
                    metadata={"strategy": strategy_name, "status": "error"},
                )

        if final_result is None or final_result.status == "error":
            errors = [str(item.get("error")) for item in attempts if item.get("error")]
            final_result = final_result or ToolResult(
                tool_name=self.tool_name,
                tool_kind="graph",
                status="error",
                task=task,
                metadata={"error": "; ".join(errors) or "graph execution failed"},
            )

        fallback_used = len(attempts) > 1
        metadata = dict(final_result.metadata or {})
        metadata.update(
            {
                "strategy": final_strategy or metadata.get("strategy"),
                "requested_strategy": requested,
                "fallback_used": fallback_used,
                "attempts": attempts,
            }
        )
        if final_result.status == "error" and "error" not in metadata:
            errors = [str(item.get("error")) for item in attempts if item.get("error")]
            if errors:
                metadata["error"] = "; ".join(errors)

        final_result = final_result.model_copy(
            update={
                "tool_name": self.tool_name,
                "tool_kind": "graph",
                "metadata": metadata,
            }
        )
        self._trace_done(trace, task, final_result, started_at)
        return GraphToolExecution(
            result=final_result,
            strategy=final_strategy,
            attempts=attempts,
        )

    def _normalize_candidate(self, result: ToolResult, strategy_name: str) -> ToolResult:
        metadata = dict(result.metadata or {})
        metadata.setdefault("strategy", strategy_name)
        return result.model_copy(
            update={
                "tool_name": self.tool_name,
                "tool_kind": "graph",
                "metadata": metadata,
            }
        )

    def _trace_done(
        self,
        trace: Any,
        task: str,
        result: ToolResult,
        started_at: float,
    ) -> None:
        trace_event(
            trace,
            "tool_execution",
            "done",
            success=execution_success(result.status),
            tool=self.tool_name,
            question=task,
            latency_ms=elapsed_ms(started_at),
            error=result.metadata.get("error") if isinstance(result.metadata, dict) else None,
            fallback=bool(result.metadata.get("fallback_used")) if isinstance(result.metadata, dict) else False,
            metadata={
                "execution_success": execution_success(result.status),
                "status": result.status,
                "strategy": result.metadata.get("strategy") if isinstance(result.metadata, dict) else None,
                "requested_strategy": result.metadata.get("requested_strategy") if isinstance(result.metadata, dict) else None,
                "fallback_used": result.metadata.get("fallback_used", False) if isinstance(result.metadata, dict) else False,
                "result_count": _record_count(result.data),
                "statement": result.statement.text if result.statement else None,
                "attempts": result.metadata.get("attempts", []) if isinstance(result.metadata, dict) else [],
            },
        )


def create_graph_tool_node(
    *,
    tool: Optional[GraphTool] = None,
    graph: Any = None,
    llm: Any = None,
    predefined_cypher_dict: Optional[Dict[str, str]] = None,
    cypher_example_retriever: Any = None,
    llm_cypher_validation: bool = True,
    strategy: GraphStrategyName = "auto",
    legacy_node_name: Optional[str] = None,
) -> Callable[[Dict[str, Any]], Awaitable[Dict[str, Any]]]:
    """Compatibility LangGraph node backed by the canonical ``GraphTool``.

    ``legacy_node_name`` preserves the old state/step labels used by the current
    workflow.  The semantic result and trace always identify one tool:
    ``graph_tool``.
    """

    graph_tool = tool or GraphTool(
        graph=graph,
        llm=llm,
        predefined_cypher_dict=predefined_cypher_dict,
        cypher_example_retriever=cypher_example_retriever,
        llm_cypher_validation=llm_cypher_validation,
    )

    async def graph_tool_node(state: Dict[str, Any]) -> Dict[str, Any]:
        task = state.get("task") or state.get("question") or ""
        trace = state.get("eval_trace")
        query_parameters = state.get("query_parameters")
        context: Dict[str, Any] = (
            dict(query_parameters) if isinstance(query_parameters, dict) else {}
        )
        context["query_parameters"] = dict(query_parameters) if isinstance(query_parameters, dict) else {}
        context["query_name"] = state.get("query_name") or context.get("query_name")
        context["strategy"] = strategy
        context["steps"] = list(state.get("steps") or [])
        context["llm_cypher_validation"] = llm_cypher_validation

        result = await graph_tool.execute(task, context, trace=trace)
        legacy_name = legacy_node_name or (
            "predefined_cypher" if strategy == "predefined" else "cypher_query"
        )
        step = (
            "execute_predefined_cypher"
            if legacy_name == "predefined_cypher"
            else "execute_cypher_query"
        )
        statement = result.statement.text if result.statement else ""
        parameters = result.statement.parameters if result.statement else {}
        errors = _errors_from_result(result)
        records = result.data or []

        if legacy_name == "cypher_query":
            legacy_records: Any = {
                "result": records if records else _EMPTY_GRAPH_DISPLAY,
            }
        else:
            legacy_records = records if records else _EMPTY_GRAPH_DISPLAY

        return {
            "cyphers": [
                {
                    "task": task,
                    "query": statement if legacy_name == "cypher_query" else "",
                    "statement": statement,
                    "parameters": parameters,
                    "errors": errors,
                    "records": legacy_records,
                    "steps": [step],
                }
            ],
            "tool_results": [result],
            "steps": [step],
        }

    graph_tool_node.__name__ = legacy_node_name or "graph_tool"
    return graph_tool_node


__all__ = [
    "GraphExecutionStrategy",
    "GraphTool",
    "GraphToolExecution",
    "PredefinedCypherStrategy",
    "Text2CypherStrategy",
    "create_graph_tool_node",
]
