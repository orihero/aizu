"""Autocomplete mining — what real users in a locale actually type.

Campaign Lab, Remedy Sheet #1 / Remedy A.1. `suggestqueries.google.com/complete/
search` is free, keyless and returns live query completions; `ds=yt` switches the
corpus to YouTube. `client=chrome` additionally returns a `google:suggestrelevance`
array — a free ranking signal, which matters because YouTube publishes no search
volume at all and every commercial "YouTube search volume" number is a model.

`hl`/`gl` are the whole point for this engine's briefs: `hl=uz&gl=UZ` returns what
Uzbek users type, which is frequently not the language a marketer would guess.

This is an UNOFFICIAL endpoint. Three consequences shape the code:
  * it can 429/503 without warning — every failure degrades to fewer candidates,
    never to an exception reaching a caller;
  * it must be paced — the miner sleeps between requests and stops early on a
    rate-limit rather than hammering through a whole alphabet;
  * its response shape is loose — parsing is defensive at every index.

Verified live 2026-08-20 (per the research pass). Re-verify before relying on a
behaviour change.
"""
from __future__ import annotations

import json
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from typing import Callable, Iterable, Optional, Sequence

from ..core.logsetup import get_logger

log = get_logger(__name__)

SUGGEST_URL = "https://suggestqueries.google.com/complete/search"
_TIMEOUT_SECONDS = 6.0
# A browser-ish UA: the endpoint serves a terse or empty body to obvious scripts.
_USER_AGENT = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
               "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36")

# Pacing. Well inside anything the endpoint reacts to, and the whole alphabet
# sweep for one seed is ~30 requests ≈ 8s — cheap next to the 3-5 min browser
# budget a campaign creation already spends.
DEFAULT_DELAY_SECONDS = 0.25
# Consecutive failures after which the sweep gives up on this run. Two is enough
# to distinguish "one flaky response" from "we are being throttled".
_FAILURE_BUDGET = 2

# Suffix alphabets for the expansion sweep. Seed + each letter turns one query
# into a fan of real completions; the Cyrillic row is what surfaces ru/uz-Cyrillic
# phrasings that the Latin row cannot reach.
LATIN_ALPHABET = "abcdefghijklmnopqrstuvwxyz"
CYRILLIC_ALPHABET = "абвгдежзийклмнопрстуфхцчшщэюя"


@dataclass
class Suggestion:
    """One completion and its relevance, if the endpoint supplied one."""
    query: str
    relevance: int = 0
    source: str = ""          # the probe string that produced it


@dataclass
class SuggestClient:
    """Thin, paced, failure-tolerant client for the suggest endpoint.

    `opener` and `sleep` are injected so tests never touch the network and never
    actually wait."""
    ds: str = ""                      # "" = web search, "yt" = YouTube
    hl: str = "en"
    gl: str = ""
    delay_seconds: float = DEFAULT_DELAY_SECONDS
    opener: Optional[Callable[[str], bytes]] = None
    sleep: Callable[[float], None] = time.sleep
    _failures: int = field(default=0, init=False)
    _requests: int = field(default=0, init=False)

    @property
    def exhausted(self) -> bool:
        """True once the failure budget is spent — the caller should stop."""
        return self._failures >= _FAILURE_BUDGET

    def _fetch(self, url: str) -> bytes:
        if self.opener is not None:
            return self.opener(url)
        req = urllib.request.Request(url, headers={"User-Agent": _USER_AGENT})
        with urllib.request.urlopen(req, timeout=_TIMEOUT_SECONDS) as resp:
            return resp.read()

    def suggest(self, query: str) -> list[Suggestion]:
        """Completions for `query`. Returns [] on any failure — an unofficial
        endpoint going quiet must cost candidates, never the caller's run."""
        query = (query or "").strip()
        if not query or self.exhausted:
            return []
        params = {"client": "chrome", "q": query, "hl": self.hl}
        if self.ds:
            params["ds"] = self.ds
        if self.gl:
            params["gl"] = self.gl
        url = f"{SUGGEST_URL}?{urllib.parse.urlencode(params)}"
        if self._requests:
            self.sleep(self.delay_seconds)
        self._requests += 1
        try:
            raw = self._fetch(url)
        except urllib.error.HTTPError as e:
            # 429/503 are the throttle signals; anything else is a one-off.
            self._failures += 1 if e.code in (429, 503) else 0
            log.debug("suggest HTTP %s for %r", e.code, query)
            return []
        except Exception:  # noqa: BLE001 — network/DNS/timeout all degrade alike
            self._failures += 1
            log.debug("suggest failed for %r", query, exc_info=True)
            return []
        self._failures = 0
        return _parse(raw, query)


def _parse(raw: bytes, probe: str) -> list[Suggestion]:
    """Decode a `client=chrome` suggest body.

    Shape is `[query, [completions], [descriptions], [], {metadata}]` with the
    relevance scores under `google:suggestrelevance`. Every index is optional in
    practice, so nothing here indexes without checking."""
    try:
        # The endpoint occasionally serves latin-1-ish bytes under a utf-8 label.
        data = json.loads(raw.decode("utf-8", errors="replace"))
    except Exception:  # noqa: BLE001 — a non-JSON body is just no suggestions
        return []
    if not isinstance(data, list) or len(data) < 2:
        return []
    completions = data[1] if isinstance(data[1], list) else []
    meta = next((x for x in data[2:] if isinstance(x, dict)), {})
    scores = meta.get("google:suggestrelevance")
    scores = scores if isinstance(scores, list) else []
    out: list[Suggestion] = []
    for i, item in enumerate(completions):
        if not isinstance(item, str) or not item.strip():
            continue
        score = scores[i] if i < len(scores) and isinstance(scores[i], int) else 0
        out.append(Suggestion(query=item.strip(), relevance=score, source=probe))
    return out


def _probes(seed: str, alphabets: Sequence[str],
            prefixes: Sequence[str]) -> list[str]:
    """The probe strings for one seed: the bare seed, seed+letter across each
    alphabet, and prefix+seed for the question forms."""
    out = [seed]
    for alphabet in alphabets:
        out.extend(f"{seed} {ch}" for ch in alphabet)
    out.extend(f"{p} {seed}" for p in prefixes)
    return out


def mine(seed: str, *, client: Optional[SuggestClient] = None,
         alphabets: Sequence[str] = (LATIN_ALPHABET,),
         prefixes: Sequence[str] = (),
         limit: int = 200) -> list[Suggestion]:
    """Sweep one seed into real user queries, best-relevance first.

    Deduped on the completion text, keeping the highest relevance seen — the same
    completion surfaces under several probes and its best score is the honest one.

    Stops early when the client's failure budget is spent, so a throttled sweep
    returns what it already has instead of grinding through the rest of the
    alphabet collecting empty responses."""
    seed = (seed or "").strip().lstrip("#")
    if not seed:
        return []
    client = client or SuggestClient()
    best: dict[str, Suggestion] = {}
    for probe in _probes(seed, alphabets, prefixes):
        if client.exhausted:
            log.info("autocomplete sweep stopped early (throttled) · seed=%r · "
                     "%d suggestion(s) kept", seed, len(best))
            break
        for s in client.suggest(probe):
            key = s.query.lower()
            if key not in best or s.relevance > best[key].relevance:
                best[key] = s
    out = sorted(best.values(), key=lambda s: (-s.relevance, s.query))
    return out[:limit]


def mine_many(seeds: Iterable[str], *, client: Optional[SuggestClient] = None,
              alphabets: Sequence[str] = (LATIN_ALPHABET,),
              prefixes: Sequence[str] = (),
              per_seed: int = 60) -> list[Suggestion]:
    """`mine` across several seeds, sharing one client (and therefore one failure
    budget and one pacing clock)."""
    client = client or SuggestClient()
    out: list[Suggestion] = []
    seen: set[str] = set()
    for seed in seeds:
        for s in mine(seed, client=client, alphabets=alphabets,
                      prefixes=prefixes, limit=per_seed):
            key = s.query.lower()
            if key not in seen:
                seen.add(key)
                out.append(s)
    return out
