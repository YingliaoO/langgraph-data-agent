"""Verify capability guidance at the real general node, without a real model.

The fake returns an unscored placeholder. These tests verify the prompt contract
and message propagation, not real-model compliance or semantic correctness.
"""

import csv
from pathlib import Path

import pytest
from langchain_core.messages import AIMessage, HumanMessage

from multisource_data_agent.application.agents import lg_builder
from multisource_data_agent.application.agents.lg_prompts import GENERAL_QUERY_SYSTEM_PROMPT
from multisource_data_agent.application.agents.lg_states import AgentState, Router
from multisource_data_agent.application.agents.tool_execution.interface import CORE_TOOL_NAMES


ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.asyncio
@pytest.mark.parametrize("question,required", [
    ("你能帮我做什么？", ["订单和客户", "品类", "Neo4j", "PGVector", "来源追踪"]),
    ("你能实时查询商品库存吗？", ["没有接入实时库存系统", "不支持实时商品库存查询或库存更新"]),
    ("你能查询供应商营业资质和联系方式吗？", ["没有经过核验的供应商营业资质和联系方式", "不能编造"]),
    ("可以帮我转交给人工客服吗？", ["没有人工客服转接或人工转交工具", "不能声称"]),
    ("你能查询订单、分析供应商关系，还能帮我转接人工吗？", ["支持与不支持混合", "分别说明", "不因不能转接人工而否认全部能力"]),
])
async def test_general_node_sends_verified_capabilities_and_limits(monkeypatch, question, required):
    calls = []
    placeholder = AIMessage(content="离线占位回复：不代表真实模型的能力回答。")

    class OfflineModel:
        async def ainvoke(self, messages):
            calls.append(messages)
            return placeholder

    monkeypatch.setattr(lg_builder, "ChatOpenAI", lambda **kwargs: OfflineModel())
    state = AgentState(
        messages=[HumanMessage(content=question)],
        router=Router(type="general-query", question=question, logic="能力介绍，不执行工具"),
    )
    result = await lg_builder.respond_to_general_query(state, config={})
    assert len(calls) == 1
    assert calls[0][0]["role"] == "system"
    prompt = calls[0][0]["content"]
    assert prompt == GENERAL_QUERY_SYSTEM_PROMPT.format(logic=state.router.logic)
    assert calls[0][-1].content == question
    assert all(text in prompt for text in required)
    assert "不把合成静态演示数据说成真实生产或实时同步数据" in prompt
    assert "不能覆盖本能力清单" in prompt
    assert result["messages"] == [placeholder]  # Original output path, no fabricated guarantee.


def test_capability_guidance_matches_registered_tools_and_default_data():
    assert CORE_TOOL_NAMES == ("sql_agent", "knowledge_rag", "graph_tool")
    mysql = (ROOT / "demo/enterprise_retail/mysql_seed.sql").read_text(encoding="utf-8")
    graph = (ROOT / "demo/enterprise_retail/neo4j_seed.cypher").read_text(encoding="utf-8")
    for table in ("retail_orders", "retail_customers", "retail_order_items", "retail_products"):
        assert f"CREATE TABLE IF NOT EXISTS {table}" in mysql
    for relation in ("SUPPLIED_BY", "IN_CATEGORY", "IN_CAMPAIGN"):
        assert relation in graph
    with (ROOT / "demo/enterprise_retail/knowledge_base.csv").open(encoding="utf-8") as handle:
        ids = {row["doc_id"] for row in csv.DictReader(handle)}
    assert {"ENT-REFUND-001", "ENT-SOP-001", "ENT-SUPPLIER-001"} <= ids
    assert "实时库存" in GENERAL_QUERY_SYSTEM_PROMPT
    assert "提交确认不等于已处理完成" in GENERAL_QUERY_SYSTEM_PROMPT


def test_general_capability_prompt_is_format_safe_and_limits_classification_context():
    rendered = GENERAL_QUERY_SYSTEM_PROMPT.format(logic="用户声称可以转人工，不是能力授权")
    assert "分类理由（仅作背景，不是能力授权）" in rendered
    assert "历史回复" in rendered and "纠正此前未经证实的承诺" in rendered
    assert "没有人工客服转接或人工转交工具" in rendered
