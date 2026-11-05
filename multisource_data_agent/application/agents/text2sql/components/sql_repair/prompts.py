"""Prompt for the single controlled SQL repair attempt."""

from langchain_core.prompts import ChatPromptTemplate


def create_sql_repair_prompt() -> ChatPromptTemplate:
    """Return a compact prompt that repairs one failed read-only SQL query."""
    return ChatPromptTemplate.from_messages(
        [
            (
                "system",
                """
你是一名 SQL 修复助手。你的任务不是重新设计查询，而是根据明确错误修复已有 SQL。

严格要求：
1. 保持用户原始查询意图，不添加额外业务逻辑。
2. 只能使用给定 Schema 中存在的表和字段。
3. 只能输出一条只读 SELECT/CTE 查询。
4. 重点修复给出的失败原因；不要修改与错误无关的逻辑。
5. 只输出 SQL，不要解释，不要 Markdown 代码块。
""".strip(),
            ),
            (
                "human",
                """
数据库类型：{db_type}

数据库 Schema：
{schema}

用户问题：
{question}

失败 SQL：
{failed_sql}

失败原因：
{failure_reason}

请输出修复后的 SQL。
""".strip(),
            ),
        ]
    )


__all__ = ["create_sql_repair_prompt"]
