"""Caption co-occurrence miner — new seed hashtags from our own labelled corpus.

Campaign Lab, Remedy Sheet #1 / Remedy A.4. Zero external requests: every input
already sits in SQLite. `seen_reels` holds `caption` plus the `relevant` label
every engine writes, so for any hashtag appearing in captions we can ask the only
question that matters — *of the posts carrying this tag, what share passed the
relevance gate?* — and compare it to the campaign's own base rate.

Why lift and not raw counts: the most frequent co-occurring tag on an Uzbek
renovation post is `#toshkent`, on every post, relevant or not. Raw frequency
ranks the generic tags first by construction. Lift ranks the tags that are
*disproportionately* present on the relevant ones, which is what a seed has to be.

Why a Wilson lower bound and not raw precision: a tag seen once, on one relevant
post, has precision 1.0 and would top a raw ranking forever. The lower bound of
the Wilson score interval collapses toward 0 when support is thin and approaches
the observed rate as support grows, so support and precision are traded off in
one number instead of via an arbitrary min-count cutoff. The implementation is
shared with the eval harness (`core/evalstats.py`) — the same discipline Sheet #3
imposes there, applied here.

The miner is deliberately read-only and side-effect free. It proposes; the
operator (or the generator prompt) disposes.
"""
from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from typing import Any, Iterable, Optional, Sequence

from .evalstats import wilson_lower_bound
from .logsetup import get_logger
from .parsers import extract_hashtags

log = get_logger(__name__)

# A tag needs to be seen this many times before it can be proposed at all. The
# Wilson bound already punishes thin support; this is a floor on how much of the
# corpus we are willing to read a signal into, not a second statistical test.
DEFAULT_MIN_SUPPORT = 3


@dataclass(frozen=True)
class TagCandidate:
    """One mined hashtag and the evidence behind it."""
    tag: str
    support: int          # captions containing the tag
    relevant: int         # …of which passed the relevance gate
    precision: float      # relevant / support (the point estimate — reported, not ranked on)
    score: float          # Wilson lower bound of precision (what the ranking uses)
    lift: float           # score ÷ campaign base rate; >1 = better than the corpus average

    def as_dict(self) -> dict[str, Any]:
        return {"tag": self.tag, "support": self.support, "relevant": self.relevant,
                "precision": round(self.precision, 4), "score": round(self.score, 4),
                "lift": round(self.lift, 3)}


def mine_captions(rows: Iterable[dict[str, Any]], *,
                  exclude: Sequence[str] = (),
                  min_support: int = DEFAULT_MIN_SUPPORT,
                  limit: int = 25) -> list[TagCandidate]:
    """Rank co-occurring hashtags in `rows` (dicts with `caption` and `relevant`).

    Pure and store-free so it can be unit-tested on fixtures and reused over any
    caption corpus — including the warming engine's, which is forbidden from
    touching the router or spending money but may absolutely count words.

    `relevant` is tri-state in the DB (1/0/NULL, NULL = never gated). NULL rows
    count toward neither numerator nor denominator: a tag whose posts were all
    skipped before scoring has no evidence either way, and treating "not judged"
    as "judged irrelevant" would quietly bury every tag from a session that halted
    early.
    """
    drop = {str(x).lstrip("#").lower() for x in exclude}
    support: Counter[str] = Counter()
    positive: Counter[str] = Counter()
    corpus_total = corpus_relevant = 0
    for row in rows:
        rel = row.get("relevant")
        if rel is None:
            continue
        rel = bool(rel)
        corpus_total += 1
        corpus_relevant += int(rel)
        # ocr_text is deliberately NOT read here: on-screen text is vision output,
        # not the author's own tagging, and its `#` marks are usually OCR noise.
        tags = set(extract_hashtags(row.get("caption") or ""))
        for tag in tags:
            if tag in drop:
                continue
            support[tag] += 1
            if rel:
                positive[tag] += 1
    if not corpus_total:
        return []
    base_rate = corpus_relevant / corpus_total
    out: list[TagCandidate] = []
    for tag, n in support.items():
        if n < min_support:
            continue
        k = positive[tag]
        score = wilson_lower_bound(k, n)
        out.append(TagCandidate(
            tag=tag, support=n, relevant=k, precision=k / n, score=score,
            # A zero base rate means the campaign has never had a relevance pass;
            # there is nothing to be better than, so lift is reported as 0 rather
            # than as infinity.
            lift=(score / base_rate) if base_rate > 0 else 0.0))
    out.sort(key=lambda c: (-c.score, -c.support, c.tag))
    log.debug("tagmine · corpus=%d relevant=%d base_rate=%.3f candidates=%d",
              corpus_total, corpus_relevant, base_rate, len(out))
    return out[:limit]


def mine_campaign(store, campaign_id: str, *,
                  platform: Optional[str] = None,
                  exclude: Sequence[str] = (),
                  min_support: int = DEFAULT_MIN_SUPPORT,
                  limit: int = 25) -> list[TagCandidate]:
    """`mine_captions` over one campaign's `seen_reels`.

    `exclude` should carry the campaign's current seeds: a post discovered on
    `#remont` carries `#remont`, so an un-excluded seed tag wins its own ranking
    and pushes out every genuinely new candidate."""
    rows = store.reels(campaign_id)
    if platform:
        rows = [r for r in rows if r.get("platform") == platform]
    return mine_captions(rows, exclude=exclude, min_support=min_support, limit=limit)
