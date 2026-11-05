"""Few-shot Cypher examples for the default synthetic Enterprise Retail graph.

This retriever only supplies examples to dynamic Text2Cypher generation. It does
not select a tool or route a request; canonical tool selection remains semantic.
"""

from __future__ import annotations

import re

from .base import BaseCypherExampleRetriever


class RetailCypherExampleRetriever(BaseCypherExampleRetriever):
    """Return compact retail relationship examples for Text2Cypher prompting."""

    _examples = (
        {
            "question": "Aurora ANC Headphones 由哪个供应商供货？",
            "cypher": "MATCH (p:RetailProduct {name: $product_name})-[:SUPPLIED_BY]->(s:RetailSupplier) RETURN p.name AS product_name, s.name AS supplier_name",
        },
        {
            "question": "NorthStar Electronics 提供哪些商品？",
            "cypher": "MATCH (s:RetailSupplier {name: $supplier_name})<-[:SUPPLIED_BY]-(p:RetailProduct) RETURN p.name AS product_name, p.sku AS sku ORDER BY p.name",
        },
        {
            "question": "Aurora ANC Headphones 属于哪个商品分类？",
            "cypher": "MATCH (p:RetailProduct {name: $product_name})-[:IN_CATEGORY]->(c:RetailCategory) RETURN c.name AS category_name",
        },
        {
            "question": "Aurora ANC Headphones 参加哪些活动？",
            "cypher": "MATCH (p:RetailProduct {name: $product_name})-[:IN_CAMPAIGN]->(c:RetailCampaign) RETURN c.name AS campaign_name, c.start_date, c.end_date ORDER BY c.start_date",
        },
        {
            "question": "Electronics 分类下有哪些商品？",
            "cypher": "MATCH (c:RetailCategory {name: $category_name})<-[:IN_CATEGORY]-(p:RetailProduct) RETURN p.name AS product_name, p.sku AS sku ORDER BY p.name",
        },
    )

    def get_examples(self, query: str, k: int = 5) -> str:
        query_words = set(re.findall(r"\w+", query.lower()))

        def score(example: dict[str, str]) -> int:
            words = set(re.findall(r"\w+", example["question"].lower()))
            return len(query_words.intersection(words))

        selected = sorted(self._examples, key=score, reverse=True)[: max(1, k)]
        return "\n\n".join(
            f"Question: {item['question']}\nCypher: {item['cypher']}"
            for item in selected
        )
