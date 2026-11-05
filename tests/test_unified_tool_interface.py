"""Phase 1 Step 4 tests for the unified core-tool execution interface."""

import asyncio
from typing import Any, Dict, Optional

from multisource_data_agent.application.agents.tool_contracts import Evidence, ToolResult
from multisource_data_agent.application.agents.tool_execution.interface import (
    CORE_TOOL_NAMES,
    DataTool,
    DuplicateToolAliasError,
    ToolBinding,
    ToolDescriptor,
    ToolRegistry,
    UnifiedToolExecutor,
    create_default_tool_executor,
    create_default_tool_registry,
    normalize_tool_name,
)


def run(coro):
    return asyncio.run(coro)


class FakeTool:
    def __init__(self, name: str, kind: str, *, native_name: Optional[str] = None) -> None:
        self.tool_name = name
        self.tool_kind = kind
        self.native_name = native_name or name
        self.calls = []

    async def execute(
        self,
        task: str,
        context: Optional[Dict[str, Any]] = None,
        *,
        trace: Any = None,
    ) -> ToolResult:
        self.calls.append((task, context, trace))
        return ToolResult(
            tool_name=self.native_name,
            tool_kind=self.tool_kind,
            status="success",
            task=task,
            data=[{"value": self.tool_kind}],
            evidence=[Evidence(kind=f"{self.tool_kind}_evidence", content=task)],
            metadata={"native": True},
        )


class RaisingTool(FakeTool):
    async def execute(self, task, context=None, *, trace=None):
        raise RuntimeError("boom")


class InvalidResultTool(FakeTool):
    async def execute(self, task, context=None, *, trace=None):
        return {"not": "a ToolResult"}


class SlowTool(FakeTool):
    async def execute(self, task, context=None, *, trace=None):
        self.calls.append((task, context, trace))
        await asyncio.sleep(0.05)
        return ToolResult(
            tool_name=self.tool_name,
            tool_kind=self.tool_kind,
            status="success",
            task=task,
        )


class SequenceTool(FakeTool):
    def __init__(self, name: str, kind: str, outcomes) -> None:
        super().__init__(name, kind)
        self.outcomes = list(outcomes)

    async def execute(self, task, context=None, *, trace=None):
        self.calls.append((task, context, trace))
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


def _result(name: str, kind: str, *, status: str, metadata=None) -> ToolResult:
    return ToolResult(
        tool_name=name,
        tool_kind=kind,
        status=status,
        task="test",
        data=[{"ok": True}] if status == "success" else [],
        metadata=dict(metadata or {}),
    )


def make_executor():
    sql = FakeTool("sql_agent", "sql", native_name="text2sql_query")
    rag = FakeTool("knowledge_rag", "rag")
    graph = FakeTool("graph_tool", "graph")
    executor = create_default_tool_executor(
        sql_agent=sql,
        knowledge_rag=rag,
        graph_tool=graph,
    )
    return executor, sql, rag, graph


def test_core_tool_surface_is_exactly_three_first_class_tools() -> None:
    executor, *_ = make_executor()

    assert CORE_TOOL_NAMES == ("sql_agent", "knowledge_rag", "graph_tool")
    assert executor.registry.names() == CORE_TOOL_NAMES
    assert [item.kind for item in executor.registry.descriptors()] == [
        "sql",
        "rag",
        "graph",
    ]


def test_default_registry_maps_legacy_aliases_without_exposing_legacy_tools() -> None:
    registry = create_default_tool_registry(
        sql_agent=FakeTool("sql_agent", "sql"),
        knowledge_rag=FakeTool("knowledge_rag", "rag"),
        graph_tool=FakeTool("graph_tool", "graph"),
    )

    assert registry.canonical_name("text2sql_query") == "sql_agent"
    assert registry.canonical_name("postgres") == "knowledge_rag"
    assert "milvus" not in registry.aliases()
    assert registry.canonical_name("cypher_query") == "graph_tool"
    assert registry.canonical_name("predefined-cypher") == "graph_tool"
    assert "customer_tools" not in registry.aliases()
    assert "microsoft_graphrag_query" not in registry.aliases()


def test_executor_normalizes_sql_native_identity_and_preserves_payload() -> None:
    executor, sql, *_ = make_executor()
    trace = {"trace_id": "test"}
    context = {"max_rows": 25}

    result = run(
        executor.execute(
            "text2sql_query",
            "统计订单数",
            context,
            trace=trace,
        )
    )

    assert result.tool_name == "sql_agent"
    assert result.tool_kind == "sql"
    assert result.status == "success"
    assert result.data == [{"value": "sql"}]
    assert result.evidence[0].content == "统计订单数"
    assert result.metadata["native"] is True
    assert result.metadata["native_tool_name"] == "text2sql_query"
    assert result.metadata["canonical_tool_name"] == "sql_agent"
    assert result.metadata["requested_tool_name"] == "text2sql_query"
    assert sql.calls == [("统计订单数", context, trace)]


def test_executor_routes_rag_and_graph_aliases_to_one_tool_each() -> None:
    executor, _, rag, graph = make_executor()

    rag_result = run(executor.execute("pgvector", "历史资料", {"top_k": 3}))
    graph_result = run(
        executor.execute(
            "predefined_cypher",
            "查询关系",
        )
    )

    assert rag_result.tool_name == "knowledge_rag"
    assert rag_result.tool_kind == "rag"
    assert rag_result.metadata["requested_tool_name"] == "pgvector"
    assert len(rag.calls) == 1

    milvus_result = run(executor.execute("milvus", "历史资料"))
    assert milvus_result.status == "error"
    assert milvus_result.metadata["error_code"] == "unknown_tool"

    assert graph_result.tool_name == "graph_tool"
    assert graph_result.tool_kind == "graph"
    assert graph_result.metadata["requested_tool_name"] == "predefined_cypher"
    assert len(graph.calls) == 1
    assert graph.calls[0][1] == {"strategy": "predefined"}


def test_unknown_tool_returns_contract_error_instead_of_raising() -> None:
    executor, *_ = make_executor()

    result = run(executor.execute("customer_tools", "描述菜品"))

    assert result.status == "error"
    assert result.tool_name == "customer_tools"
    assert result.tool_kind is None
    assert result.metadata["error_code"] == "unknown_tool"
    assert result.metadata["error_category"] == "tool_error"
    assert result.metadata["available_tools"] == list(CORE_TOOL_NAMES)


def test_executor_contains_tool_exceptions() -> None:
    registry = ToolRegistry(
        (
            ToolBinding(
                descriptor=ToolDescriptor(
                    name="sql_agent",
                    kind="sql",
                    description="test",
                ),
                tool=RaisingTool("sql_agent", "sql"),
            ),
        )
    )
    executor = UnifiedToolExecutor(registry)

    result = run(executor.execute("sql_agent", "统计"))

    assert result.tool_name == "sql_agent"
    assert result.tool_kind == "sql"
    assert result.status == "error"
    assert result.metadata["error_code"] == "tool_execution_exception"
    assert result.metadata["error_type"] == "RuntimeError"
    assert result.metadata["error_category"] == "tool_error"


def test_executor_rejects_non_tool_result_payload() -> None:
    registry = ToolRegistry(
        (
            ToolBinding(
                descriptor=ToolDescriptor(
                    name="graph_tool",
                    kind="graph",
                    description="test",
                ),
                tool=InvalidResultTool("graph_tool", "graph"),
            ),
        )
    )

    result = run(UnifiedToolExecutor(registry).execute("graph_tool", "查询"))

    assert result.status == "error"
    assert result.tool_name == "graph_tool"
    assert result.tool_kind == "graph"
    assert result.metadata["error_code"] == "invalid_tool_result"
    assert result.metadata["error_type"] == "dict"
    assert result.metadata["error_category"] == "tool_error"


def test_duplicate_aliases_are_rejected() -> None:
    registry = ToolRegistry()
    registry.register(
        ToolBinding(
            descriptor=ToolDescriptor(
                name="sql_agent",
                kind="sql",
                description="sql",
                aliases=("shared",),
            ),
            tool=FakeTool("sql_agent", "sql"),
        )
    )

    try:
        registry.register(
            ToolBinding(
                descriptor=ToolDescriptor(
                    name="graph_tool",
                    kind="graph",
                    description="graph",
                    aliases=("shared",),
                ),
                tool=FakeTool("graph_tool", "graph"),
            )
        )
    except DuplicateToolAliasError:
        pass
    else:  # pragma: no cover - assertion branch
        raise AssertionError("duplicate alias should fail")


def test_real_boundaries_expose_common_protocol_metadata() -> None:
    from multisource_data_agent.application.agents.tool_execution.graph_tool import GraphTool
    from multisource_data_agent.application.agents.tool_execution.knowledge_rag import KnowledgeRAGTool
    from multisource_data_agent.application.agents.tool_execution.sql_agent import SQLAgent

    assert SQLAgent.tool_name == "sql_agent"
    assert SQLAgent.tool_kind == "sql"
    assert KnowledgeRAGTool.tool_name == "knowledge_rag"
    assert KnowledgeRAGTool.tool_kind == "rag"
    assert GraphTool.tool_name == "graph_tool"
    assert GraphTool.tool_kind == "graph"

    # Runtime protocol check does not construct external connections.
    assert isinstance(SQLAgent(), DataTool)
    assert isinstance(KnowledgeRAGTool(), DataTool)
    assert isinstance(GraphTool(), DataTool)


def test_registry_rejects_tool_metadata_mismatch() -> None:
    registry = ToolRegistry()
    try:
        registry.register(
            ToolBinding(
                descriptor=ToolDescriptor(
                    name="sql_agent",
                    kind="sql",
                    description="sql",
                ),
                tool=FakeTool("graph_tool", "graph"),
            )
        )
    except ValueError as exc:
        assert "does not match" in str(exc)
    else:  # pragma: no cover - assertion branch
        raise AssertionError("tool metadata mismatch should fail")


def test_name_normalization_is_small_and_deterministic() -> None:
    assert normalize_tool_name("  PREDEFINED-CYPHER ") == "predefined_cypher"
    assert normalize_tool_name("Knowledge_RAG") == "knowledge_rag"


def test_executor_retries_one_transient_503_and_traces_attempt() -> None:
    tool = SequenceTool(
        "knowledge_rag",
        "rag",
        [
            _result(
                "knowledge_rag",
                "rag",
                status="error",
                metadata={"http_status": 503, "error": "service unavailable"},
            ),
            _result("knowledge_rag", "rag", status="success"),
        ],
    )
    registry = ToolRegistry(
        (
            ToolBinding(
                descriptor=ToolDescriptor(
                    name="knowledge_rag", kind="rag", description="test"
                ),
                tool=tool,
            ),
        )
    )
    trace = {
        "trace_id": "retry-test",
        "events": [],
        "selected_tools": [],
        "fallback": False,
    }

    result = run(
        UnifiedToolExecutor(
            registry, max_transient_retries=1, retry_backoff_seconds=0
        ).execute("knowledge_rag", "test", trace=trace)
    )

    assert result.status == "success"
    assert len(tool.calls) == 2
    assert result.metadata["retry_count"] == 1
    assert result.metadata["attempts"] == 2
    retry_events = [event for event in trace["events"] if event.get("action") == "retry"]
    assert len(retry_events) == 1
    assert retry_events[0]["attempt"] == 2
    assert retry_events[0]["metadata"]["retry_reason"] == "http_503"
    assert trace["fallback"] is False


def test_executor_retries_connection_exception_once() -> None:
    tool = SequenceTool(
        "graph_tool",
        "graph",
        [
            ConnectionError("connection reset by peer"),
            _result("graph_tool", "graph", status="success"),
        ],
    )
    registry = ToolRegistry(
        (
            ToolBinding(
                descriptor=ToolDescriptor(
                    name="graph_tool", kind="graph", description="test"
                ),
                tool=tool,
            ),
        )
    )

    result = run(
        UnifiedToolExecutor(
            registry, max_transient_retries=1, retry_backoff_seconds=0
        ).execute("graph_tool", "test")
    )

    assert result.status == "success"
    assert len(tool.calls) == 2
    assert result.metadata["retry_count"] == 1


def test_executor_does_not_retry_policy_timeout_or_billing_failures() -> None:
    terminal_cases = [
        {"error_code": "READ_ONLY_POLICY_VIOLATION", "error": "policy rejection"},
        {"error_type": "TimeoutError", "error": "timed out"},
        {
            "http_status": 429,
            "error_code": "insufficient_quota",
            "error": "insufficient balance",
        },
        {"http_status": 400, "error": "invalid argument"},
    ]

    for metadata in terminal_cases:
        tool = SequenceTool(
            "sql_agent",
            "sql",
            [_result("sql_agent", "sql", status="error", metadata=metadata)],
        )
        registry = ToolRegistry(
            (
                ToolBinding(
                    descriptor=ToolDescriptor(
                        name="sql_agent", kind="sql", description="test"
                    ),
                    tool=tool,
                ),
            )
        )
        result = run(
            UnifiedToolExecutor(
                registry, max_transient_retries=1, retry_backoff_seconds=0
            ).execute("sql_agent", "test")
        )

        assert result.status == "error"
        assert len(tool.calls) == 1
        assert result.metadata["retry_count"] == 0


def test_executor_retry_is_bounded_to_one_additional_attempt() -> None:
    tool = SequenceTool(
        "knowledge_rag",
        "rag",
        [
            _result(
                "knowledge_rag",
                "rag",
                status="error",
                metadata={"http_status": 502, "error": "bad gateway"},
            ),
            _result(
                "knowledge_rag",
                "rag",
                status="error",
                metadata={"http_status": 503, "error": "service unavailable"},
            ),
        ],
    )
    registry = ToolRegistry(
        (
            ToolBinding(
                descriptor=ToolDescriptor(
                    name="knowledge_rag", kind="rag", description="test"
                ),
                tool=tool,
            ),
        )
    )

    result = run(
        UnifiedToolExecutor(
            registry, max_transient_retries=1, retry_backoff_seconds=0
        ).execute("knowledge_rag", "test")
    )

    assert result.status == "error"
    assert len(tool.calls) == 2
    assert result.metadata["retry_count"] == 1
    assert result.metadata["attempts"] == 2


def test_executor_timeout_is_terminal_and_not_retried() -> None:
    tool = SlowTool("graph_tool", "graph")
    registry = ToolRegistry((ToolBinding(
        descriptor=ToolDescriptor(name="graph_tool", kind="graph", description="test"),
        tool=tool,
    ),))
    trace = {"trace_id": "tool-timeout", "events": [], "selected_tools": [], "fallback": False}

    result = run(
        UnifiedToolExecutor(
            registry,
            max_transient_retries=1,
            retry_backoff_seconds=0,
            timeout_seconds=0.01,
        ).execute("graph_tool", "慢查询", trace=trace)
    )

    assert result.status == "error"
    assert len(tool.calls) == 1
    assert result.metadata["error_code"] == "tool_timeout"
    assert result.metadata["error_category"] == "timeout"
    assert result.metadata["retry_count"] == 0
    assert result.metadata["attempts"] == 1
    assert result.metadata["timeout_budget_ms"] == 10.0
    retries = [event for event in trace["events"] if event.get("action") == "retry"]
    assert retries == []
    timeout_events = [
        event for event in trace["events"]
        if event.get("stage") == "tool_execution" and event.get("error_code") == "tool_timeout"
    ]
    assert len(timeout_events) == 1
    assert timeout_events[0]["fallback"] is False
