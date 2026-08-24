"""Seed expansion — LLM proposes nouns, oracles expand and rank them.

Campaign Lab, Remedy Sheet #1 / Remedy A. The generator's job shrinks from
"invent 3-8 hashtags that exist" (a task with a 30-45% entity-hallucination base
rate) to "name the nouns for this product" — something a language model is
actually reliable at. Everything downstream comes from oracles:

  * script variants        (`translit`)  — free, offline, deterministic
  * demand-side requests   (`patterns`)  — free, offline, fixed matrix
  * autocomplete           (`autocomplete`) — free, keyless, live, and the only
                                              layer that can fail

Ranking is deliberately simple and explainable: provenance beats cleverness here,
because an operator has to be able to look at a proposed seed and see why it was
proposed. `ExpansionResult.candidates` carries that trail.

Nothing in here writes to the DB or spends money.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional, Sequence

from ..core.logsetup import get_logger
from .autocomplete import (CYRILLIC_ALPHABET, LATIN_ALPHABET, SuggestClient,
                           mine_many)
from .patterns import QUESTION_PREFIXES, demand_queries
from .translit import script_variants

log = get_logger(__name__)

# How each origin is weighted. Autocomplete evidence is the only layer that
# reflects real, current demand, so it outranks the deterministic expansions —
# but a script variant of a noun the client actually named is worth more than a
# stranger's long-tail completion, hence `seed` on top.
_ORIGIN_WEIGHT = {"seed": 100.0, "autocomplete": 60.0, "script": 40.0,
                  "request": 30.0}

# `gl` is the market, `hl` the interface language — the pair is what makes suggest
# return local phrasings rather than global English ones. There is no default
# market: an unset `gl` lets the endpoint infer one, which is strictly better than
# pinning every campaign to this deployment's home country. Operators who DO want
# a fixed market set AIZU_SEED_GEO (see `campaign_gen`); `SUGGESTED_GEO` names the
# value this deployment was built for.
SUGGESTED_GEO = "UZ"

# Languages routinely written in Cyrillic. The transliteration fan-out is gated on
# these: running it unconditionally turns an English brief's "marathon" into
# "маратҳон", a string no human has ever typed, and pollutes the seed list with
# confident nonsense. A brief that names no language gets no fan-out at all —
# guessing the script is exactly the class of mistake this sheet exists to remove.
_CYRILLIC_SCRIPT_LANGS = frozenset(
    {"ru", "uz", "kk", "ky", "tg", "mn", "sr", "bg", "uk", "be", "mk"})


@dataclass
class SeedCandidate:
    """One proposed search term and where it came from."""
    term: str
    origins: list[str] = field(default_factory=list)
    relevance: int = 0            # best autocomplete relevance seen, 0 if none
    lang: str = ""

    @property
    def score(self) -> float:
        """Sum of origin weights plus a small autocomplete-relevance bonus.

        Summed, not maxed: a term that is BOTH a script variant of the client's
        noun AND a live autocomplete completion is better evidenced than either
        alone, and that is exactly the term we most want at the top."""
        base = sum(_ORIGIN_WEIGHT.get(o, 0.0) for o in set(self.origins))
        return base + min(self.relevance, 2000) / 100.0

    def as_dict(self) -> dict[str, Any]:
        return {"term": self.term, "origins": sorted(set(self.origins)),
                "relevance": self.relevance, "lang": self.lang,
                "score": round(self.score, 2)}


@dataclass
class ExpansionResult:
    candidates: list[SeedCandidate]
    queries: list[str]            # demand-side search strings (for X/TG/Reddit/LI)
    degraded: bool = False        # True when the live layer failed or was skipped

    def hashtags(self, limit: int = 8) -> list[str]:
        """The top single-token candidates — the ones usable as hashtags.

        Multi-word candidates are excellent *search queries* and useless as
        hashtags, so they are filtered here rather than handed to a tag page that
        will 302 to keyword search (the exact 2026-08-19 failure)."""
        out = [c.term for c in self.candidates if " " not in c.term]
        return out[:limit]

    def as_dict(self) -> dict[str, Any]:
        return {"candidates": [c.as_dict() for c in self.candidates],
                "queries": list(self.queries), "degraded": self.degraded}


def _lang_codes(langs: Sequence[str]) -> set[str]:
    return {str(l).strip().lower()[:2] for l in langs if str(l).strip()}


def _uses_cyrillic(langs: Sequence[str]) -> bool:
    return bool(_lang_codes(langs) & _CYRILLIC_SCRIPT_LANGS)


def _alphabets(langs: Sequence[str]) -> tuple[str, ...]:
    """Which suffix alphabets to sweep. Cyrillic is included only when the brief
    names a language written in it — sweeping both for an English-only brief
    doubles the request count for nothing."""
    out = [LATIN_ALPHABET]
    if _uses_cyrillic(langs):
        out.append(CYRILLIC_ALPHABET)
    return tuple(out)


def expand_seeds(nouns: Sequence[str], *,
                 langs: Sequence[str] = (),
                 geo: str = "",
                 online: bool = True,
                 client: Optional[SuggestClient] = None,
                 limit: int = 40,
                 query_limit: int = 60) -> ExpansionResult:
    """Expand LLM-proposed `nouns` into ranked candidate seeds + search queries.

    `langs` drives every locale decision and defaults to EMPTY, not to a guess:
    with no declared languages there is no transliteration fan-out and no
    non-English request patterns, because inventing a script for the audience is
    the same failure mode as inventing a hashtag.

    `online=False` (or a failing suggest endpoint) yields the deterministic layers
    only and flags the result `degraded` — an honest "we could not check this
    live" is the point; silently returning guesses is what this whole sheet exists
    to stop.
    """
    nouns = [str(n).strip().lstrip("#") for n in nouns if str(n).strip()]
    if not nouns:
        return ExpansionResult(candidates=[], queries=[], degraded=not online)

    by_term: dict[str, SeedCandidate] = {}

    def add(term: str, origin: str, *, relevance: int = 0, lang: str = "") -> None:
        term = (term or "").strip().lstrip("#")
        if not term:
            return
        key = term.lower()
        cand = by_term.get(key)
        if cand is None:
            cand = SeedCandidate(term=term, lang=lang)
            by_term[key] = cand
        cand.origins.append(origin)
        cand.relevance = max(cand.relevance, relevance)
        if lang and not cand.lang:
            cand.lang = lang

    # 1. The nouns themselves, plus their other scripts — but only for briefs whose
    #    audience actually writes in Cyrillic (see _CYRILLIC_SCRIPT_LANGS).
    fan_out_scripts = _uses_cyrillic(langs)
    for noun in nouns:
        add(noun, "seed")
        if fan_out_scripts:
            for variant in script_variants(noun)[1:]:
                add(variant, "script")

    # 2. Demand-side request strings. These are search QUERIES, not tags — kept
    #    in their own list, and also registered as candidates so a single-word
    #    pattern hit ("posovetuyte X" is multi-word; "narxi" alone is not) is not
    #    lost.
    queries = demand_queries(nouns, langs=langs, limit=query_limit)
    for q in queries:
        add(q, "request")

    # 3. The one live layer.
    degraded = not online
    if online:
        prefixes: list[str] = []
        for code in {str(l).strip().lower()[:2] for l in langs}:
            prefixes.extend(QUESTION_PREFIXES.get(code, ()))
        hl = next(iter(_lang_codes(langs)), "") or "en"
        client = client or SuggestClient(hl=hl, gl=geo)
        try:
            mined = mine_many(nouns, client=client, alphabets=_alphabets(langs),
                              prefixes=tuple(prefixes))
        except Exception:  # noqa: BLE001 — the live layer must never be fatal
            log.warning("autocomplete expansion failed — deterministic layers only",
                        exc_info=True)
            mined = []
        for s in mined:
            add(s.query, "autocomplete", relevance=s.relevance, lang=hl)
        # `exhausted` means the sweep stopped early on repeated throttling: the
        # result is real but incomplete, and the caller deserves to know.
        degraded = not mined or client.exhausted

    ranked = sorted(by_term.values(), key=lambda c: (-c.score, c.term))
    log.info("Seed expansion · nouns=%d candidates=%d queries=%d degraded=%s",
             len(nouns), len(ranked), len(queries), degraded)
    return ExpansionResult(candidates=ranked[:limit], queries=queries,
                           degraded=degraded)
