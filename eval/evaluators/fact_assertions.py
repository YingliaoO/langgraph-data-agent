"""Optional deterministic assertions, separate from historical literal metrics.

These checks cover declared facts, not general answer faithfulness. Evidence
checks use only the public execution contract; bounded previews are not proof
of facts that are absent from that contract. Aliases must be declared in a case.
An answer_fact pass means a declared phrase occurs, not that it is affirmed or
attached to the correct entity/condition. Negation, contradictions and extra
claims still require review; review_checks alone is not a completed review.
"""

from __future__ import annotations

import re
import unicodedata
from decimal import Decimal, InvalidOperation
from typing import Any


KINDS = {"answer_fact", "evidence_fact", "evidence_row", "evidence_rows", "evidence_scalar"}
SOURCES = {"mysql", "pgvector", "neo4j"}


def _strings(value: Any) -> bool:
    return isinstance(value, list) and bool(value) and all(
        isinstance(item, str) and bool(item.strip()) for item in value
    )


def validate_fact_assertions(assertions: Any) -> None:
    """Reject malformed goldens rather than silently exclude them from scores."""
    if not isinstance(assertions, list):
        raise ValueError("fact_assertions must be a list")
    ids: set[str] = set()
    for assertion in assertions:
        if not isinstance(assertion, dict):
            raise ValueError("Each fact assertion must be an object")
        identifier, kind = assertion.get("id"), assertion.get("kind")
        if not isinstance(identifier, str) or not identifier.strip() or identifier in ids:
            raise ValueError("Fact assertion IDs must be nonempty and unique")
        ids.add(identifier)
        if kind not in KINDS:
            raise ValueError(f"Unknown assertion kind: {kind}")
        if kind != "answer_fact" and assertion.get("source") not in SOURCES:
            raise ValueError("Evidence assertions require a canonical source")
        if kind in {"answer_fact", "evidence_fact"} and not _strings(assertion.get("any_of")):
            raise ValueError("Fact assertions require explicit nonempty any_of strings")
        if kind == "evidence_scalar":
            value = assertion.get("equals")
            if not isinstance(value, (int, float)) or isinstance(value, bool) or _number(value) is None:
                raise ValueError("Scalar assertions require a finite numeric equals value")
        if assertion.get("task_id") is not None and (not isinstance(assertion["task_id"], str) or not assertion["task_id"].strip()):
            raise ValueError("task_id must be a nonempty string")
        if kind == "evidence_rows":
            rows = assertion.get("rows")
            if not isinstance(rows, list) or len(rows) > 30 or not all(isinstance(row, dict) for row in rows):
                raise ValueError("evidence_rows requires at most 30 expected row objects")
            if not isinstance(assertion.get("ordered", False), bool):
                raise ValueError("ordered must be boolean")
        row_checks = assertion.get("rows", []) if kind == "evidence_rows" else [assertion] if kind == "evidence_row" else []
        for row_check in row_checks:
            fields = row_check.get("fields")
            if not isinstance(fields, list) or not fields:
                raise ValueError("Row assertions require fields")
            for field in fields:
                if not isinstance(field, dict) or not _strings(field.get("keys")):
                    raise ValueError("Row fields require explicit key aliases")
                if "equals" not in field or not isinstance(field["equals"], (str, int, float)) or isinstance(field["equals"], bool):
                    raise ValueError("Row fields require a string or numeric equals value")
                if isinstance(field["equals"], (int, float)) and _number(field["equals"]) is None:
                    raise ValueError("Row field numbers must be finite")


def _normalize(value: str) -> str:
    return " ".join(unicodedata.normalize("NFKC", value).casefold().split())


def _contains_fact(text: str, expected: str) -> bool:
    expected = _normalize(expected)
    # Numeric boundaries avoid 5 matching 15 or 5.5. ASCII word boundaries
    # avoid English prefixes; Chinese phrases do not require whitespace.
    left = r"(?<![a-z\d_.+-])(?<!\d,)" if expected[0].isdigit() else r"(?<![a-z0-9_])" if expected[0].isascii() and expected[0].isalpha() else ""
    right = r"(?![a-z\d_])(?![.,]\d)" if expected[-1].isdigit() else r"(?![a-z0-9_])" if expected[-1].isascii() and expected[-1].isalpha() else ""
    return re.search(left + re.escape(expected) + right, _normalize(text)) is not None


def _content_strings(content: Any) -> list[str]:
    # Match values, never metadata keys/source IDs. Keep cells/documents
    # separate so fragments in unrelated records cannot form a new fact.
    if isinstance(content, dict):
        return [text for value in content.values() for text in _content_strings(value)]
    if isinstance(content, list):
        return [text for value in content for text in _content_strings(value)]
    return [str(content)] if isinstance(content, (str, int, float)) and not isinstance(content, bool) else []


def _number(value: Any) -> Decimal | None:
    if isinstance(value, bool) or not isinstance(value, (str, int, float)):
        return None
    try:
        number = Decimal(str(value))
    except InvalidOperation:
        return None
    return number if number.is_finite() else None


def _equal(actual: Any, expected: Any) -> bool:
    if isinstance(expected, (int, float)) and not isinstance(expected, bool):
        return _number(actual) == _number(expected)
    return isinstance(actual, str) and _normalize(actual) == _normalize(expected)


def _row_matches(row: Any, fields: list[dict[str, Any]]) -> bool:
    if not isinstance(row, dict):
        return False
    for field in fields:
        aliases = {_normalize(key) for key in field["keys"]}
        values = [value for key, value in row.items() if _normalize(str(key)) in aliases]
        # Conflicting duplicate aliases must not be cherry-picked as a pass.
        if not values or not all(_equal(value, field["equals"]) for value in values):
            return False
    return True


def _rows_match(actual: list[Any], expected: list[dict[str, Any]], ordered: bool) -> bool:
    if len(actual) != len(expected):
        return False
    if ordered:
        return all(_row_matches(row, spec["fields"]) for row, spec in zip(actual, expected))
    # Bipartite matching also handles overlapping aliases without greedy choices.
    matched: dict[int, int] = {}
    def assign(spec_index: int, visited: set[int]) -> bool:
        for row_index, row in enumerate(actual):
            if row_index in visited or not _row_matches(row, expected[spec_index]["fields"]):
                continue
            visited.add(row_index)
            if row_index not in matched or assign(matched[row_index], visited):
                matched[row_index] = spec_index
                return True
        return False
    return all(assign(index, set()) for index in range(len(expected)))


def evaluate_fact_assertions(assertions: Any, response: dict[str, Any]) -> list[dict[str, Any]]:
    validate_fact_assertions(assertions)
    execution = response.get("execution")
    evidence = execution.get("evidence", []) if isinstance(execution, dict) else []
    evidence = [item for item in evidence if isinstance(item, dict)] if isinstance(evidence, list) else []
    results = []
    for assertion in assertions:
        kind = assertion["kind"]
        items = [item for item in evidence if item.get("source") == assertion.get("source")]
        if assertion.get("task_id") is not None:
            items = [item for item in items if item.get("task_id") == assertion["task_id"]]
        if kind == "answer_fact":
            answer = response.get("message") or ""
            passed = isinstance(answer, str) and any(_contains_fact(answer, value) for value in assertion["any_of"])
        elif kind == "evidence_fact":
            passed = any(
                _contains_fact(text, value)
                for item in items
                for text in _content_strings(item.get("content"))
                for value in assertion["any_of"]
            )
        elif kind == "evidence_row":
            passed = any(_row_matches(item.get("content"), assertion["fields"]) for item in items)
        elif kind == "evidence_rows":
            passed = _rows_match([item.get("content") for item in items], assertion["rows"], assertion.get("ordered", False))
        else:  # evidence_scalar: an aggregate with exactly one returned column
            passed = any(
                isinstance(item.get("content"), dict)
                and len(item["content"]) == 1
                and _equal(next(iter(item["content"].values())), assertion["equals"])
                for item in items
            )
        results.append({"id": assertion["id"], "kind": kind, "passed": bool(passed)})
    return results
