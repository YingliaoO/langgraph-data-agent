"""Exercise the real matcher/templates, not a matcher stub returning the golden."""

import asyncio

import pytest

from multisource_data_agent.application.agents.kg_sub_graph.agentic_rag_agents.components.predefined_cypher.cypher_dict import predefined_cypher_dict
from multisource_data_agent.application.agents.kg_sub_graph.agentic_rag_agents.components.predefined_cypher.descriptions import QUERY_DESCRIPTIONS
from multisource_data_agent.application.agents.kg_sub_graph.agentic_rag_agents.components.predefined_cypher.utils import create_vector_query_matcher
from multisource_data_agent.application.agents.tool_execution.graph_tool import GraphTool, PredefinedCypherStrategy


@pytest.fixture(scope="module")
def matcher():
    return create_vector_query_matcher(predefined_cypher_dict, QUERY_DESCRIPTIONS)


@pytest.mark.parametrize("entity", ["Aurora ANC Headphones", "Vertex Wireless Mouse"])
@pytest.mark.parametrize("intent,expected", [
    ("由哪家供应商供货", "retail_product_supplier"),
    ("属于哪个商品分类", "retail_product_category"),
    ("参加了哪个活动", "retail_product_campaigns"),
])
def test_same_entity_does_not_override_requested_relationship(matcher, entity, intent, expected):
    matches = matcher.match_query(f"查询 {entity} {intent}？")
    assert matches and matches[0]["query_name"] == expected
    assert matcher.extract_parameters(f"查询 {entity} {intent}？", expected) == {"product_name": entity}


def test_supplier_product_list_is_not_inverse_relationship(matcher):
    question = "查询供应商关系图：Riverside Supply 提供哪些商品？"
    assert matcher.match_query(question)[0]["query_name"] == "retail_supplier_products"
    assert matcher.extract_parameters("供应商Riverside Supply提供哪些商品？", "retail_supplier_products") == {"supplier_name": "Riverside Supply"}


@pytest.mark.parametrize("question", [
    "Aurora ANC Headphones", "查询商品信息", "NorthStar Electronics 的承诺适用于什么订单？",
])
def test_entity_alone_or_policy_scope_does_not_select_a_graph_template(matcher, question):
    assert matcher.match_query(question) == []


class RecordingGraph:
    def __init__(self):
        self.calls = []

    def query(self, query, params):
        self.calls.append((query, params))
        if params["product_name"] == "P9999":
            return []
        if "IN_CAMPAIGN" in query:
            return [{"campaign_name": "Autumn Member Week"}]
        return [{"supplier_name": "NorthStar Electronics"}]


class ForbiddenDynamic:
    strategy_name = "text2cypher"

    async def execute(self, *args, **kwargs):
        raise AssertionError("A completed predefined query must not run dynamic fallback")


@pytest.mark.parametrize("product_key,status", [("P1001", "success"), ("P9999", "empty")])
@pytest.mark.parametrize("question", ["从商品供应商关系图查询 SKU {key} 由谁供货。", "商品SKU为{key}由谁供货？"])
def test_sku_lookup_remains_parameterized_and_empty_does_not_fall_back(matcher, product_key, status, question):
    graph = RecordingGraph()
    tool = GraphTool(predefined_strategy=PredefinedCypherStrategy(graph=graph, matcher=matcher, llm=object()),
                     text2cypher_strategy=ForbiddenDynamic())
    result = asyncio.run(tool.execute(question.format(key=product_key)))
    assert result.status == status and result.metadata["fallback_used"] is False
    statement, params = graph.calls[0]
    assert "p.sku = $product_name" in statement and product_key not in statement
    assert params == {"product_name": product_key}


def test_campaign_execution_returns_campaign_evidence_not_supplier_evidence(matcher):
    graph = RecordingGraph()
    strategy = PredefinedCypherStrategy(graph=graph, matcher=matcher, llm=object())
    result = asyncio.run(strategy.execute("查询 Aurora ANC Headphones 参加了哪个活动。", context={}))
    assert result.status == "success"
    assert "IN_CAMPAIGN" in result.statement.text and "SUPPLIED_BY" not in result.statement.text
    assert result.evidence[0].content == {"campaign_name": "Autumn Member Week"}
    assert result.metadata["query_name"] == "retail_product_campaigns"
