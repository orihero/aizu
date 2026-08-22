"""Per-platform shortlist validation — does this search term actually work?

Campaign Lab, Remedy Sheet #1 / Remedy C. Expansion (`expand.py`) produces
plausible terms; this decides which of them are worth a warmed browser's time,
and it does so BEFORE a campaign ever runs rather than after a wasted session.

The table the research settled on, and what is implemented here:

  YouTube   — `search.list(order=date, publishedAfter=now-30d, part=id)` for
              recency, then ONE `videos.list` for statistics. Official, cheap,
              zero risk. Implemented.
  Instagram — one `topsearch` typeahead call per tag from the ALREADY-WARMED
              session: existence + `media_count` + near-matches in a single
              request. Implemented (`InstagramTagProbe`), paced at >=1 req/10s.
  Reddit    — needs a grandfathered pre-Nov-2025 OAuth app; there is no free
              anonymous path since the `.json` API died (~May 2026). Not
              implemented: it is an operator credential decision, not code.
  Telegram  — t.me has no search at all; discovery needs a paid catalog
              (Telemetr/TGStat). Deferred to Sheet #2, where the unit being
              validated is a CHANNEL, not a term.
  X         — deliberately none. Login-walled, attributable, no result counts,
              rotating doc_ids. Validated passively by per-source yield instead
              (that is what `source_stats` is for).
  LinkedIn  — hashtag pages were removed in Oct 2024; there is nothing to probe.
              Invest in query phrasing (`patterns.py`) and stay under the
              commercial-use limit.

Every validator returns a `TermVerdict` and never raises: an unvalidated term is
reported as `unknown`, which is honest, and is what the operator sees.
"""
from __future__ import annotations

import json
import time
import urllib.parse
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Iterable, Optional, Sequence

from ..core.logsetup import get_logger

log = get_logger(__name__)

# Verdicts. `unknown` is a first-class outcome, not an error: "we could not check
# this" must never be presentable as "this is fine".
LIVE = "live"           # exists and is currently active
THIN = "thin"           # exists but has little or no recent activity
DEAD = "dead"           # does not exist / banned / no results at all
UNKNOWN = "unknown"     # not checked (no credentials, network failure, opted out)


@dataclass
class TermVerdict:
    term: str
    platform: str
    verdict: str = UNKNOWN
    volume: int = 0             # media_count / result count, when the platform reports one
    recent: int = 0             # items published in the recency window
    detail: str = ""
    alternatives: list[str] = field(default_factory=list)   # near-matches worth trying

    @property
    def usable(self) -> bool:
        """Whether to keep the term as a seed. `unknown` counts as usable: a term
        we could not check is not a term we have evidence against, and dropping it
        would silently narrow a campaign on the strength of a network hiccup."""
        return self.verdict in (LIVE, THIN, UNKNOWN)

    def as_dict(self) -> dict[str, Any]:
        return {"term": self.term, "platform": self.platform, "verdict": self.verdict,
                "volume": self.volume, "recent": self.recent, "detail": self.detail,
                "alternatives": list(self.alternatives)}


# ---------------------------------------------------------------------------
# YouTube
# ---------------------------------------------------------------------------

# The June-2026 quota change: `search.list` bills to its OWN bucket of 100
# searches/day, separate from the 10k-unit pool. That is the binding constraint
# on this validator, not units — validating 30 terms costs 30% of a day's
# searches, so the cap is enforced here rather than left to the caller.
YOUTUBE_SEARCH_BUDGET = 25
RECENCY_WINDOW_DAYS = 30
# Below this many videos in the window, a term is `thin` — it exists, but a
# campaign seeded with it will run out of fresh content immediately.
YOUTUBE_THIN_BELOW = 3


class YouTubeTermValidator:
    """Validate search terms against the YouTube Data API.

    Reuses `engines.youtube.feed.YouTubeDataApiClient` (or anything with its
    `_get`), so credentials resolve exactly as a run's do. `now` is injected for
    deterministic tests."""

    def __init__(self, client: Any, *,
                 now: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
                 budget: int = YOUTUBE_SEARCH_BUDGET):
        self._client = client
        self._now = now
        self._budget = budget

    def validate(self, terms: Sequence[str]) -> list[TermVerdict]:
        out: list[TermVerdict] = []
        spent = 0
        for term in terms:
            term = str(term).strip().lstrip("#")
            if not term:
                continue
            if spent >= self._budget:
                # Never silently truncate: an unchecked term says so.
                out.append(TermVerdict(term, "youtube", UNKNOWN,
                                       detail="search budget for this run exhausted"))
                continue
            spent += 1
            out.append(self._one(term))
        return out

    def _one(self, term: str) -> TermVerdict:
        after = (self._now() - timedelta(days=RECENCY_WINDOW_DAYS))
        params = {"part": "id", "type": "video", "order": "date",
                  "maxResults": 25, "q": term,
                  "publishedAfter": after.strftime("%Y-%m-%dT%H:%M:%SZ")}
        try:
            body = self._client._get("search", params)
        except Exception as e:  # noqa: BLE001 — quota/network/auth all degrade alike
            log.debug("YouTube term validation failed for %r", term, exc_info=True)
            return TermVerdict(term, "youtube", UNKNOWN, detail=str(e)[:200])
        items = [i for i in (body.get("items") or []) if isinstance(i, dict)]
        recent = len(items)
        # `totalResults` is an ESTIMATE and the API documents it as such — it is
        # reported for the operator, never used to decide the verdict.
        total = int(((body.get("pageInfo") or {}).get("totalResults") or 0))
        if recent == 0:
            return TermVerdict(term, "youtube", DEAD, volume=total,
                               detail=f"no videos in the last {RECENCY_WINDOW_DAYS} days")
        verdict = THIN if recent < YOUTUBE_THIN_BELOW else LIVE
        return TermVerdict(term, "youtube", verdict, volume=total, recent=recent,
                           detail=f"{recent} video(s) in the last "
                                  f"{RECENCY_WINDOW_DAYS} days")


# ---------------------------------------------------------------------------
# Instagram
# ---------------------------------------------------------------------------

# The UI dropped post counts; the typeahead endpoint never did. One call returns
# existence, `media_count`, AND near-matching tags — so a misspelled seed comes
# back with the correct spelling attached instead of just failing.
IG_TOPSEARCH_URL = ("https://www.instagram.com/api/v1/web/search/topsearch/"
                    "?context=hashtag&query=%s")
# Instaloader encodes 199 api/v1 requests per 1800s as the safe ceiling; one
# request per 10s is comfortably inside it. Reads do not trip write action-blocks,
# but they DO count toward the same rate accounting.
IG_MIN_INTERVAL_SECONDS = 10.0
# A tag with fewer posts than this is real but not worth a session.
IG_THIN_BELOW = 100


class InstagramTagProbe:
    """One typeahead call per tag, issued from the already-warmed CDP session.

    Runs inside the page context (`page.evaluate` + `fetch`) so the request
    carries the session's own cookies and headers — it is the same XHR the web app
    fires while a user types, which is what keeps it the lowest-risk read
    available. Never crafts an authenticated API call from outside the browser.

    A 403 here is a signal about OUR session, not about the tag: it is reported as
    `unknown` and stops the sweep, because continuing would burn a session that is
    already unhappy.
    """

    def __init__(self, feed: Any, *, min_interval: float = IG_MIN_INTERVAL_SECONDS,
                 sleep: Callable[[float], None] = time.sleep,
                 clock: Callable[[], float] = time.monotonic):
        self._feed = feed
        self._min_interval = min_interval
        self._sleep = sleep
        self._clock = clock
        # None, not 0.0: `time.monotonic()` can legitimately return 0.0 on a
        # freshly-booted host, and a falsy check there silently disables pacing on
        # exactly the endpoint that must never be hammered.
        self._last_at: Optional[float] = None
        self.session_unhealthy = False

    def _pace(self) -> None:
        if self._last_at is None:
            return
        wait = self._min_interval - (self._clock() - self._last_at)
        if wait > 0:
            self._sleep(wait)

    def _fetch(self, tag: str) -> Optional[dict]:
        page = getattr(self._feed, "_page", None)
        if page is None:
            return None
        url = IG_TOPSEARCH_URL % urllib.parse.quote(f"#{tag}")
        script = ("async (u) => { const r = await fetch(u, "
                  "{credentials: 'include', headers: "
                  "{'x-ig-app-id': '936619743392459'}}); "
                  "return r.ok ? await r.text() : 'HTTP:' + r.status; }")
        raw = page.evaluate(script, url)
        if not isinstance(raw, str):
            return None
        if raw.startswith("HTTP:"):
            code = raw[5:]
            if code in ("401", "403", "429"):
                # About us, not about the tag.
                self.session_unhealthy = True
            raise RuntimeError(f"topsearch HTTP {code}")
        return json.loads(raw)

    def validate(self, tags: Sequence[str]) -> list[TermVerdict]:
        out: list[TermVerdict] = []
        for tag in tags:
            tag = str(tag).strip().lstrip("#")
            if not tag:
                continue
            if self.session_unhealthy:
                out.append(TermVerdict(tag, "instagram", UNKNOWN,
                                       detail="session flagged unhealthy; sweep stopped"))
                continue
            self._pace()
            try:
                body = self._fetch(tag)
            except Exception as e:  # noqa: BLE001 — a probe must never end a run
                log.debug("IG topsearch failed for %r", tag, exc_info=True)
                out.append(TermVerdict(tag, "instagram", UNKNOWN, detail=str(e)[:200]))
                self._last_at = self._clock()
                continue
            self._last_at = self._clock()
            out.append(_ig_verdict(tag, body))
        return out


def _ig_verdict(tag: str, body: Optional[dict]) -> TermVerdict:
    """Read a topsearch body: exact match → counts; near matches → alternatives."""
    if not isinstance(body, dict):
        return TermVerdict(tag, "instagram", UNKNOWN, detail="unreadable response")
    hits = []
    for entry in (body.get("hashtags") or []):
        node = entry.get("hashtag") if isinstance(entry, dict) else None
        if isinstance(node, dict) and node.get("name"):
            hits.append(node)
    exact = next((h for h in hits if str(h.get("name", "")).lower() == tag.lower()), None)
    alternatives = [str(h["name"]) for h in hits
                    if str(h.get("name", "")).lower() != tag.lower()][:5]
    if exact is None:
        # Not in the typeahead at all: either it does not exist or it is banned.
        # The two are indistinguishable from here and must not be conflated — the
        # banner detection during a real walk (`_source_unavailable`) is what
        # separates them.
        return TermVerdict(tag, "instagram", DEAD, detail="no such hashtag in search",
                           alternatives=alternatives)
    try:
        count = int(exact.get("media_count") or 0)
    except (TypeError, ValueError):
        count = 0
    verdict = THIN if count < IG_THIN_BELOW else LIVE
    return TermVerdict(tag, "instagram", verdict, volume=count,
                       detail=f"{count} post(s)", alternatives=alternatives)


def validators_for(platform: str, **kw) -> Optional[Any]:
    """The validator for `platform`, or None where the research says not to probe.

    Returning None is a real answer here, not a gap: on X the correct amount of
    pre-validation is zero, and on LinkedIn there is no hashtag surface left to
    probe."""
    if platform == "youtube" and kw.get("client") is not None:
        return YouTubeTermValidator(kw["client"])
    if platform == "instagram" and kw.get("feed") is not None:
        return InstagramTagProbe(kw["feed"])
    return None


def partition(verdicts: Iterable[TermVerdict]) -> tuple[list[str], list[str]]:
    """Split verdicts into (keep, drop). `unknown` is kept — see `TermVerdict.usable`."""
    keep, drop = [], []
    for v in verdicts:
        (keep if v.usable else drop).append(v.term)
    return keep, drop
