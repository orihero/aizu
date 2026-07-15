"""Shared match-decision helpers used by every platform's session loop."""
from __future__ import annotations

from typing import Any


def compute_found_by(primary_model: str, comparisons: list[dict[str, Any]],
                     threshold: float) -> list[str]:
    """Which models independently would have called this comment a match.

    Always includes `primary_model` (it's what created the lead). Every
    comparison model whose own score cleared the campaign's threshold is added
    too, in order, de-duplicated; a comparison that errored is excluded (an
    unknown verdict is not a "found it"). Empty/falsy inputs degrade gracefully
    (no primary model name, or the feature off with no comparisons) to `[]`.
    """
    found: list[str] = []
    if primary_model:
        found.append(primary_model)
    for c in comparisons:
        model = c.get("model")
        score = c.get("score")
        if not model or model in found or c.get("error"):
            continue
        if score is not None and score >= threshold:
            found.append(model)
    return found
