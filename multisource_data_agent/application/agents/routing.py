"""Canonical top-level route semantics for the Data Agent.

The runtime exposes four product-facing semantic routes. Historical data-source-specific
routes are accepted as compatibility aliases, but are normalized before
orchestration so SQL/RAG/Graph selection belongs to Planner + Tool Selection.
"""

from __future__ import annotations

from typing import Final


CANONICAL_ROUTE_TYPES: Final[tuple[str, ...]] = (
    "general-query",
    "additional-query",
    "data-query",
    "file-query",
)

LEGACY_ROUTE_ALIASES: Final[dict[str, str]] = {
    "text2sql-query": "data-query",
    "kb-query": "data-query",
    "graphrag-query": "data-query",
    # Image/Vision is no longer a product-facing route. Keep the historical
    # label as a compatibility input and degrade it to a scoped general reply.
    "image-query": "general-query",
}

ALL_ROUTE_TYPES: Final[tuple[str, ...]] = (
    *CANONICAL_ROUTE_TYPES,
    *LEGACY_ROUTE_ALIASES.keys(),
)


def normalize_route_type(route_type: object, *, default: str = "data-query") -> str:
    """Return the canonical route for a current or historical route label."""

    value = str(route_type or "").strip().lower()
    if value in CANONICAL_ROUTE_TYPES:
        return value
    if value in LEGACY_ROUTE_ALIASES:
        return LEGACY_ROUTE_ALIASES[value]
    return default


def is_legacy_route(route_type: object) -> bool:
    """Whether ``route_type`` is one of the historical source-specific labels."""

    return str(route_type or "").strip().lower() in LEGACY_ROUTE_ALIASES


__all__ = [
    "ALL_ROUTE_TYPES",
    "CANONICAL_ROUTE_TYPES",
    "LEGACY_ROUTE_ALIASES",
    "is_legacy_route",
    "normalize_route_type",
]
