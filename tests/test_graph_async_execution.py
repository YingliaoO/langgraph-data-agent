"""Graph sync SDK calls must not block the loop or swallow tool cancellation."""

import asyncio
import threading
from types import SimpleNamespace

import pytest

from multisource_data_agent.application.agents.tool_execution.graph_tool import (
    GraphTool,
    PredefinedCypherStrategy,
    Text2CypherStrategy,
)
from multisource_data_agent.application.agents.kg_sub_graph.agentic_rag_agents.components.cypher_tools import utils


class BlockingCall:
    """Controlled synchronous I/O; no server and no timing-dependent sleep."""

    def __init__(self, result):
        self.result = result
        self.started = threading.Event()
        self.release = threading.Event()
        self.finished = threading.Event()
        self.thread_id = None

    def __call__(self, *args, **kwargs):
        self.thread_id = threading.get_ident()
        self.started.set()
        try:
            if not self.release.wait(timeout=5):
                raise RuntimeError("test did not release simulated I/O")
            return self.result
        finally:
            self.finished.set()


async def assert_timeout_while_io_is_pending(operation, blocking):
    """The request must time out before the synchronous call finishes."""
    task = asyncio.create_task(operation)

    async def await_started():
        while not blocking.started.is_set():
            await asyncio.sleep(0.001)

    try:
        await asyncio.wait_for(await_started(), timeout=2)
        # This coroutine is an independent request/heartbeat on the same loop.
        assert blocking.thread_id != threading.get_ident()
        assert not blocking.finished.is_set()
        with pytest.raises(asyncio.TimeoutError):
            await asyncio.wait_for(task, timeout=0.05)
        assert not blocking.finished.is_set()
    finally:
        task.cancel()
        blocking.release.set()
        await asyncio.gather(task, return_exceptions=True)
        if blocking.started.is_set():
            assert await asyncio.to_thread(blocking.finished.wait, 2)


@pytest.mark.parametrize("stage", ["match", "parameters", "initialization", "query"])
def test_predefined_io_allows_timeout_without_fallback_or_later_query(stage):
    calls = []

    def query(*args, **kwargs):
        calls.append("query")
        return [{"supplier": "NorthStar"}]

    graph = SimpleNamespace(query=query)
    outputs = {
        "match": [{"query_name": "supplier", "similarity": 1}],
        "parameters": {"product": "Aurora"},
        "initialization": graph,
        "query": [{"supplier": "NorthStar"}],
    }
    blocking = BlockingCall(outputs[stage])
    matcher = SimpleNamespace(
        match_query=blocking if stage == "match" else lambda *a, **k: outputs["match"],
        extract_parameters=blocking if stage == "parameters" else lambda *a, **k: outputs["parameters"],
    )
    if stage == "query":
        graph.query = blocking
    strategy = PredefinedCypherStrategy(
        graph=None if stage == "initialization" else graph,
        graph_factory=blocking if stage == "initialization" else lambda: graph,
        predefined_cypher_dict={"supplier": "MATCH (p {name: $product}) RETURN p"},
        matcher=matcher,
        llm=object(),
    )

    class UnexpectedFallback:
        strategy_name = "text2cypher"

        async def execute(self, *args, **kwargs):
            calls.append("fallback")
            raise AssertionError("cancellation must not trigger fallback")

    tool = GraphTool(predefined_strategy=strategy, text2cypher_strategy=UnexpectedFallback())
    asyncio.run(assert_timeout_while_io_is_pending(tool.execute("Aurora supplier"), blocking))
    assert calls == []


def test_dynamic_initialization_allows_cancellation_before_generation():
    blocking = BlockingCall(SimpleNamespace())
    strategy = Text2CypherStrategy(graph_factory=blocking, llm=object())
    asyncio.run(assert_timeout_while_io_is_pending(
        strategy.execute("Aurora supplier", context={}), blocking
    ))


@pytest.mark.parametrize("stage", ["explain", "execution", "mapping", "examples"])
def test_dynamic_sync_io_yields_to_loop(stage, monkeypatch):
    blocking = BlockingCall([])
    graph = SimpleNamespace(
        query=blocking,
        structured_schema={
            "relationships": [],
            "node_props": {"RetailProduct": [{"property": "name", "type": "STRING"}]},
        },
    )
    state = {"task": "Aurora supplier", "statement": "MATCH (p) RETURN p"}

    if stage == "explain":
        operation = utils.create_text2cypher_validation_node(graph=graph)(state)
    elif stage == "execution":
        operation = utils.create_text2cypher_execution_node(graph, state["statement"])(state)
    elif stage == "mapping":
        monkeypatch.setattr(utils, "retrieve_and_parse_schema_from_graph_for_prompts", lambda g: "schema")

        class ValidationChain:
            async def ainvoke(self, payload):
                return SimpleNamespace(errors=[], filters=[SimpleNamespace(
                    node_label="RetailProduct", property_key="name", property_value="Aurora"
                )])

        operation = utils.validate_cypher_query_with_llm(
            ValidationChain(), state["task"], graph, state["statement"]
        )
    else:
        from langchain_core.language_models.fake_chat_models import FakeListChatModel

        operation = utils.create_text2cypher_generation_node(
            FakeListChatModel(responses=[state["statement"]]),
            graph,
            SimpleNamespace(get_examples=blocking),
        )(state)
    asyncio.run(assert_timeout_while_io_is_pending(operation, blocking))


@pytest.mark.parametrize("strategy_class,resource", [
    (PredefinedCypherStrategy, "graph"),
    (Text2CypherStrategy, "graph"),
    (PredefinedCypherStrategy, "llm"),
    (Text2CypherStrategy, "llm"),
    (PredefinedCypherStrategy, "matcher"),
])
def test_concurrent_lazy_initialization_reuses_resource(strategy_class, resource, monkeypatch):
    instance = object()
    calls = []
    blocking = BlockingCall(instance)

    def factory(*args, **kwargs):
        calls.append("factory")
        return blocking()

    strategy = strategy_class(graph_factory=factory)
    if resource == "llm":
        import langchain_openai

        monkeypatch.setattr(langchain_openai, "ChatOpenAI", factory)
    elif resource == "matcher":
        from multisource_data_agent.application.agents.kg_sub_graph.agentic_rag_agents.components.predefined_cypher import utils as matcher_utils

        monkeypatch.setattr(matcher_utils, "create_vector_query_matcher", factory)
    getter = getattr(strategy, f"_get_{resource}")

    async def exercise():
        first = asyncio.create_task(asyncio.to_thread(getter))
        second = asyncio.create_task(asyncio.to_thread(getter))
        try:
            while not blocking.started.is_set():
                await asyncio.sleep(0.001)
        finally:
            blocking.release.set()
        assert await asyncio.gather(first, second) == [instance, instance]

    asyncio.run(exercise())
    assert calls == ["factory"]
