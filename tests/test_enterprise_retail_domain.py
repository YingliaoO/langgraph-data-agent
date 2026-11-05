from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def read(relative: str) -> str:
    return (ROOT / relative).read_text(encoding="utf-8")


def test_current_prompts_use_enterprise_retail_semantics():
    prompt_files = [
        "multisource_data_agent/application/agents/lg_prompts.py",
        "multisource_data_agent/application/agents/lg_builder.py",
        "multisource_data_agent/application/agents/kg_sub_graph/prompts/kg_prompts.py",
        "multisource_data_agent/application/agents/kg_sub_graph/agentic_rag_agents/components/planner/prompts.py",
        "multisource_data_agent/application/agents/kg_sub_graph/agentic_rag_agents/components/guardrails/node.py",
    ]
    combined = "\n".join(read(path) for path in prompt_files)
    for term in ("retail_orders", "RetailProduct", "RetailSupplier", "退款", "退货政策"):
        assert term.lower() in combined.lower()


def test_text2sql_prompts_do_not_inject_recipe_domain_summary():
    from multisource_data_agent.application.agents.text2sql.components.query_analysis.prompts import (
        create_query_analysis_prompt,
    )
    from multisource_data_agent.application.agents.text2sql.components.sql_generation.prompts import (
        create_sql_generation_prompt,
    )
    from multisource_data_agent.application.agents.text2sql.components.visualization.prompts import (
        create_visualization_prompt,
    )

    rendered = []
    rendered.extend(
        message.content
        for message in create_query_analysis_prompt().format_messages(
            db_type="MySQL",
            schema="CREATE TABLE retail_orders (id BIGINT);",
            value_mappings="无值映射信息",
            question="查询订单总数",
        )
    )
    rendered.extend(
        message.content
        for message in create_sql_generation_prompt().format_messages(
            db_type="MySQL",
            schema="CREATE TABLE retail_orders (id BIGINT);",
            value_mappings="无值映射信息",
            analysis_summary="统计订单",
            question="查询订单总数",
        )
    )
    rendered.extend(
        message.content
        for message in create_visualization_prompt().format_messages(
            question="查询订单总数",
            analysis_summary="统计订单",
            sql_statement="SELECT COUNT(*) FROM retail_orders",
            sample_rows='[{"count": 5}]',
        )
    )

    prompt_text = "\n".join(str(value) for value in rendered).lower()
    for legacy_term in ("recipes", "ingredients", "cuisines", "recipe_steps"):
        assert legacy_term not in prompt_text


def test_retail_predefined_queries_cover_core_relationships_without_removing_legacy_templates():
    source = read(
        "multisource_data_agent/application/agents/kg_sub_graph/agentic_rag_agents/components/predefined_cypher/cypher_dict.py"
    )
    for query_name in (
        '"retail_product_supplier"',
        '"retail_supplier_products"',
        '"retail_product_category"',
        '"retail_product_campaigns"',
        '"retail_category_products"',
    ):
        assert query_name in source
    for relationship in ("SUPPLIED_BY", "IN_CATEGORY", "IN_CAMPAIGN"):
        assert relationship in source
    assert '"ingredients_of_dish"' in source


def test_graph_tool_dynamic_examples_use_retail_retriever():
    graph_tool = read("multisource_data_agent/application/agents/tool_execution/graph_tool.py")
    assert "RetailCypherExampleRetriever" in graph_tool
    assert "RecipeCypherRetriever" not in graph_tool
    retriever = read(
        "multisource_data_agent/application/agents/kg_sub_graph/agentic_rag_agents/retrievers/cypher_examples/retail_retriever.py"
    )
    assert "RetailProduct" in retriever
    assert "SUPPLIED_BY" in retriever


def test_default_docs_and_console_examples_are_enterprise_retail():
    public_files = [
        "README.md",
        "docs/QUICK_START.md",
        "docs/API_CONTRACT.md",
        "docs/ARCHITECTURE.md",
        "docs/ENTERPRISE_RETAIL_DEMO.md",
        "web/src/components/LandingPage.vue",
        "web/src/components/ChatWidget.vue",
    ]
    combined = "\n".join(read(path) for path in public_files)
    for term in ("Enterprise Retail", "retail_orders", "Aurora ANC Headphones", "NorthStar"):
        assert term.lower() in combined.lower()
    # Recipe examples remain only in explicitly historical wording or legacy files.
    assert "红烧肉" not in combined
    assert "宫保鸡丁" not in combined
    assert "佛跳墙" not in combined
