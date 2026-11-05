from __future__ import annotations

from pathlib import Path

from langchain_core.messages import HumanMessage

from eval.evaluators.metrics import evaluate_case
from multisource_data_agent.application.agents.lg_builder import _ensure_router, route_query
from multisource_data_agent.application.agents.lg_states import AgentState, Router
from multisource_data_agent.application.agents.routing import (
    CANONICAL_ROUTE_TYPES,
    LEGACY_ROUTE_ALIASES,
    normalize_route_type,
)


def test_router_exposes_exactly_four_product_routes():
    assert CANONICAL_ROUTE_TYPES == (
        "general-query",
        "additional-query",
        "data-query",
        "file-query",
    )


def test_legacy_data_routes_normalize_to_data_query():
    assert LEGACY_ROUTE_ALIASES == {
        "text2sql-query": "data-query",
        "kb-query": "data-query",
        "graphrag-query": "data-query",
        "image-query": "general-query",
    }
    for route in ("text2sql-query", "kb-query", "graphrag-query"):
        assert normalize_route_type(route) == "data-query"
    assert normalize_route_type("image-query") == "general-query"


def test_router_model_keeps_legacy_input_compatibility_but_exposes_canonical_view():
    router = Router(type="text2sql-query", logic="legacy")
    assert router.type == "text2sql-query"
    assert router.canonical_type == "data-query"
    assert _ensure_router(router).type == "data-query"


def test_route_query_sends_all_data_routes_to_unified_research_plan():
    for route in ("data-query", "text2sql-query", "kb-query", "graphrag-query"):
        state = AgentState(
            messages=[HumanMessage(content="test")],
            router=Router(type=route),
        )
        assert route_query(state) == "create_research_plan"
        assert state.router.type == "data-query"


def test_router_prompt_is_semantic_not_source_specific():
    source = Path("multisource_data_agent/application/agents/lg_prompts.py").read_text(encoding="utf-8")
    prompt = source.split('ROUTER_SYSTEM_PROMPT = """', 1)[1].split('"""', 1)[0]
    assert "data-query" in prompt
    assert "sql_agent" in prompt
    assert "knowledge_rag" in prompt
    assert "graph_tool" in prompt
    assert "text2sql-query" not in prompt
    assert "graphrag-query" not in prompt
    assert "kb-query" not in prompt
    assert "image-query" not in prompt


def test_router_no_longer_contains_keyword_fallback():
    source = Path("multisource_data_agent/application/agents/lg_builder.py").read_text(encoding="utf-8-sig")
    assert "def _heuristic_router" not in source
    assert "graphrag_keywords" not in source
    assert "text2sql_keywords" not in source
    assert 'type="data-query"' in source


def test_data_query_does_not_eagerly_initialize_neo4j_in_top_level_builder():
    source = Path("multisource_data_agent/application/agents/lg_builder.py").read_text(encoding="utf-8-sig")
    function = source.split("async def create_research_plan", 1)[1].split("async def check_hallucinations", 1)[0]
    assert "get_neo4j_graph(" not in function
    assert "RecipeCypherRetriever(" not in function
    assert "neo4j_graph = None" in function
    assert "cypher_retriever = None" in function


def test_data_query_trace_uses_canonical_stage_name():
    source = Path("multisource_data_agent/application/agents/lg_builder.py").read_text(encoding="utf-8-sig")
    function = source.split("async def create_research_plan", 1)[1].split("async def check_hallucinations", 1)[0]
    assert 'trace_event(trace, "data_query", "start"' in function
    assert '"workflow": "data-query"' in function
    assert '"graphrag_query"' not in function


def test_eval_route_accuracy_accepts_legacy_golden_labels():
    case = {
        "id": "legacy-route",
        "expected_route": "text2sql-query",
        "expected_tools": [],
    }
    response = {"route": "data-query", "message": "ok", "metadata": {}}
    result = evaluate_case(case, response, latency_ms=1.0)
    assert result["metrics"]["route_correct"] is True
    assert result["actual"]["canonical_route"] == "data-query"


def test_eval_route_accuracy_still_distinguishes_non_data_routes():
    case = {
        "id": "wrong-route",
        "expected_route": "general-query",
        "expected_tools": [],
    }
    response = {"route": "data-query", "message": "ok", "metadata": {}}
    result = evaluate_case(case, response, latency_ms=1.0)
    assert result["metrics"]["route_correct"] is False
