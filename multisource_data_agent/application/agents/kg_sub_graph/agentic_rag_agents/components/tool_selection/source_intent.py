"""Deterministic handling for explicit user-selected data sources.

This module does not infer a tool from topical keywords. It only recognizes an
explicit relational/SQL source constraint from the user, so that a semantic
planner cannot silently replace an explicitly requested MySQL/SQL execution
with another backend.
"""

from __future__ import annotations

import re
from typing import Optional


_SQL_NEGATIONS = (
    "不用sql",
    "不要用sql",
    "不要使用sql",
    "不用mysql",
    "不要用mysql",
    "不要使用mysql",
    "不用关系数据库",
    "不要用关系数据库",
    "不要使用关系数据库",
    "不用关系型数据库",
    "不要用关系型数据库",
    "不要使用关系型数据库",
)

_SQL_SOURCE_PATTERNS = (
    re.compile(
        r"(?:请)?(?:使用|通过|用)\s*(?:关系型?数据库|mysql|sql_agent|sql)",
        re.IGNORECASE,
    ),
    re.compile(r"(?:use|using|via)\s+(?:mysql|sql_agent|sql)\b", re.IGNORECASE),
)


def explicit_source_tool_hint(text: object) -> Optional[str]:
    """Return a canonical tool only when the user explicitly selects a source.

    Topic words such as ``统计`` / ``关系`` are deliberately ignored. The
    helper remains a source-constraint guard rather than becoming a keyword
    router.
    """

    value = str(text or "").strip()
    if not value:
        return None

    compact = re.sub(r"\s+", "", value.casefold())
    if any(marker in compact for marker in _SQL_NEGATIONS):
        return None

    if any(pattern.search(value) for pattern in _SQL_SOURCE_PATTERNS):
        return "sql_agent"

    return None


__all__ = ["explicit_source_tool_hint"]
