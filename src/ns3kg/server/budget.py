"""Constraint C3 (hard caps + cursor pagination) and C5 (instructive errors).

Every tool response goes through budget_page() so the caps live in exactly
one place.
"""

from __future__ import annotations

import difflib
import json

MAX_RESULTS = 30
MAX_CHARS = 8000  # ~2000 tokens at ~4 chars/token


def clamp_limit(limit) -> int:
    try:
        return max(1, min(int(limit), MAX_RESULTS))
    except (TypeError, ValueError):
        return MAX_RESULTS


def parse_cursor(cursor) -> int | None:
    """Return the offset encoded by a cursor, or None if malformed."""
    if cursor is None or cursor == "":
        return 0
    try:
        offset = int(cursor)
    except (TypeError, ValueError):
        return None
    return offset if offset >= 0 else None


def bad_cursor(cursor) -> dict:
    return {
        "error": f"malformed cursor: {cursor!r}",
        "hint": "Pass the exact next_cursor string from the previous response, "
        "or omit the cursor to start from the first result.",
    }


def budget_page(window: list, total: int, offset: int) -> dict:
    """Trim a result window to the character budget and add paging metadata.

    window: the rows fetched for this page (already <= MAX_RESULTS long).
    total:  total matching rows in the index.
    offset: index of window[0] within the full match list.
    """
    kept: list = []
    used = 0
    for item in window:
        size = len(json.dumps(item))
        if kept and used + size > MAX_CHARS:
            break
        kept.append(item)
        used += size
    end = offset + len(kept)
    truncated = end < total
    return {
        "results": kept,
        "total": total,
        "truncated": truncated,
        "next_cursor": str(end) if truncated else None,
    }


def closest(name: str, candidates: list[str], n: int = 5) -> list[str]:
    return difflib.get_close_matches(name, candidates, n=n, cutoff=0.4)


def not_found(what: str, closest_matches: list[str], hint: str) -> dict:
    return {
        "error": f"not found: {what}",
        "closest_matches": closest_matches,
        "hint": hint,
    }
