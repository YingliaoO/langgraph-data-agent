"""Pair trace spans without mixing parallel wall time and accumulated work."""

from __future__ import annotations

from collections import defaultdict, deque
from math import isfinite
from typing import Any


def stage_intervals(events: Any, stage: str, *, completed_only: bool = False) -> list[tuple[float, float]]:
    starts: dict[tuple[str, str], deque[float]] = defaultdict(deque)
    intervals = []
    terminals = {"done", "decision", "error", "timeout"}
    for event in events if isinstance(events, list) else []:
        if not isinstance(event, dict) or event.get("stage") != stage:
            continue
        timestamp = event.get("ts")
        if isinstance(timestamp, bool) or not isinstance(timestamp, (int, float)) or not isfinite(timestamp):
            continue
        # New traces have per-call IDs; historical traces use FIFO within a tool.
        key = (str(event.get("tool") or event.get("tool_name") or ""), str(event.get("call_id") or ""))
        if event.get("action") == "start":
            starts[key].append(float(timestamp))
        elif event.get("action") in terminals and starts[key]:
            start = starts[key].popleft()
            if timestamp >= start and (not completed_only or event.get("action") in {"done", "decision"}):
                intervals.append((start, float(timestamp)))
    return intervals


def interval_durations(intervals: list[tuple[float, float]]) -> dict[str, float]:
    """Wall span includes gaps; accumulated time includes overlapping calls."""
    if not intervals:
        return {}
    return {
        "wall_ms": round((max(end for _, end in intervals) - min(start for start, _ in intervals)) * 1000, 2),
        "sum_ms": round(sum(end - start for start, end in intervals) * 1000, 2),
    }
