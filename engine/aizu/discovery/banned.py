"""Banned / restricted hashtag prefilter.

Campaign Lab, Remedy Sheet #1 / Remedy C. Seeding a campaign with a banned tag
costs a navigation and four empty-scroll rounds every session and returns nothing
— the exact waste `source_stats` was built to measure. A prefilter is strictly
cheaper than measuring it: it costs zero requests.

Two sources, in this order:

  1. **What we have already observed.** `source_stats.banned_at` is set when a walk
     lands on a "page isn't available" screen. This is first-party, current, and
     specific to the platform account that hit it — it beats any list.
  2. **A static list.** Instagram permanently blocks a set of tags and restricts
     ("recent posts hidden") a much larger, undocumented, drifting set. Published
     compilations exist but are stale within months.

On (2) this module ships MECHANISM plus a deliberately small seed list, not a
scraped block of thousands of entries: an inaccurate blocklist silently deletes
working seeds, which is a worse failure than the one it prevents. Operators point
`AIZU_BANNED_TAGS_FILE` at their own maintained list (one tag per line, `#`
optional, blank lines and `//` comments ignored).

The generic-word check is separate and is the one that actually fires on real
briefs: a single very common word is not banned, it is simply so broad that the
platform's own ranking buries the niche content a campaign is looking for.
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Iterable, Optional, Sequence

from ..core.logsetup import get_logger

log = get_logger(__name__)

# Tags Instagram is documented to block outright, kept short and verifiable on
# purpose. This is a seed for the mechanism, NOT a claim to completeness — a
# campaign's real protection is `source_stats.banned_at` plus an operator list.
_SEED_BLOCKED: frozenset[str] = frozenset({
    "alone", "beautyblogger", "besties", "curvy", "desk", "dm", "elevator",
    "kansas", "loseweight", "master", "mustfollow", "nasty", "pushup",
    "singlelife", "snap", "snapchat", "sunbathing", "tag4like", "tagsforlikes",
    "todayimwearing", "valentinesday", "woman", "workflow",
})

# Words so broad that a hashtag page of them is pure noise for a lead campaign.
# Distinct from banned: these WORK, they are just worthless as seeds.
_TOO_GENERIC: frozenset[str] = frozenset({
    "love", "instagood", "photo", "photooftheday", "fashion", "beautiful",
    "happy", "cute", "like", "follow", "followme", "picoftheday", "art",
    "instagram", "style", "repost", "nature", "fun", "life", "reels", "viral",
    "trending", "explore", "explorepage", "fyp", "foryou", "foryoupage",
})

# A tag shorter than this carries no topical meaning at all.
MIN_MEANINGFUL_LENGTH = 3

BANNED_TAGS_FILE_ENV = "AIZU_BANNED_TAGS_FILE"


def _load_operator_list(path: Optional[str] = None) -> frozenset[str]:
    """Read the operator's own blocklist, if configured. A missing or unreadable
    file is not an error — the prefilter degrades to the built-in list."""
    path = path or os.environ.get(BANNED_TAGS_FILE_ENV, "").strip()
    if not path:
        return frozenset()
    try:
        text = Path(path).read_text(encoding="utf-8")
    except Exception:  # noqa: BLE001 — an unreadable list must not break generation
        log.warning("Banned-tag list %s could not be read — using the built-in list",
                    path)
        return frozenset()
    out = set()
    for line in text.splitlines():
        line = line.split("//", 1)[0].strip().lstrip("#").lower()
        if line:
            out.add(line)
    log.info("Loaded %d operator banned tag(s) from %s", len(out), path)
    return frozenset(out)


def blocked_tags(path: Optional[str] = None) -> frozenset[str]:
    """The effective blocklist: built-in ∪ operator file."""
    return _SEED_BLOCKED | _load_operator_list(path)


def reason_to_skip(tag: str, *, blocked: Optional[frozenset[str]] = None,
                   known_dead: Iterable[str] = ()) -> Optional[str]:
    """Why `tag` should not be seeded, or None if it is fine.

    Ordered most-specific first, so the reason an operator reads is the most
    informative one available."""
    norm = str(tag).strip().lstrip("#").lower()
    if not norm:
        return "empty"
    if norm in {str(d).strip().lstrip("#").lower() for d in known_dead}:
        return "this campaign's own runs found it dead or dry"
    if norm in (blocked if blocked is not None else blocked_tags()):
        return "on the banned/restricted hashtag list"
    if len(norm) < MIN_MEANINGFUL_LENGTH:
        return "too short to carry a topic"
    if norm in _TOO_GENERIC:
        return "too generic to surface niche content"
    return None


def prefilter(tags: Sequence[str], *, known_dead: Iterable[str] = (),
              path: Optional[str] = None) -> tuple[list[str], dict[str, str]]:
    """Split `tags` into (keep, {dropped_tag: reason}).

    Zero requests, so this runs before any validator and shrinks what the paid /
    rate-limited layers have to look at."""
    blocked = blocked_tags(path)
    dead = set(known_dead)
    keep: list[str] = []
    dropped: dict[str, str] = {}
    for tag in tags:
        reason = reason_to_skip(tag, blocked=blocked, known_dead=dead)
        if reason is None:
            keep.append(str(tag).strip().lstrip("#"))
        else:
            dropped[str(tag).strip().lstrip("#")] = reason
    if dropped:
        log.info("Banned-tag prefilter dropped %d of %d · %s",
                 len(dropped), len(tags),
                 ", ".join(f"{t} ({r})" for t, r in dropped.items()))
    return keep, dropped
