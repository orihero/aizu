"""Reddit FeedSource via the official Data API over OAuth2 (PRD: docs/prd/reddit-lead-agent-PRD.md).

Read-only, app-only ``client_credentials`` auth. Discovery is the operator's seeded
subreddits (``seed_channels``) and, optionally, per-subreddit search queries
(``seed_hashtags`` reused as ``subreddit.search(q)`` terms — still scoped to the
seed so it can't drift off-niche). A submission ≈ ``Reel`` (``t3``; title+selftext
→ caption, author → author); a comment ≈ ``Comment`` (``t1``), and the WHOLE
deeply-nested tree is in scope (``is_reply = depth > 0``), not just top-level.

HTTP is isolated behind ``RedditApiPort`` so the submission→Reel / comment→Comment
mapping is pure and unit-tested with a fake; the live ``RedditDataApiClient`` uses
httpx. The official API (documented JSON, PRAW-shaped endpoints) is the primary
path; a CDP-interception fallback (PRD Path B) is documented break-glass only and
not built here.

Vision is deferred (PRD §6): Reddit is text-first, so ``capture_frames`` is empty
and relevance is judged on title + selftext.
"""
from __future__ import annotations

import os
import time
from dataclasses import dataclass
from typing import Iterator, Optional, Protocol, Sequence

from ...core.feed import Comment, FeedSource, Reel
from ...core.logsetup import get_logger

log = get_logger(__name__)

# Submissions to pull per seed (subreddit or query) each session. A bounded recent
# window keeps a daytime burst far under the ~100 QPM/client_id ceiling (PRD §10).
DEFAULT_PER_SEED = 25
# Nested-tree bounds (PRD §7, §10): cap depth and the number of `more`/`morechildren`
# expansion calls so a giant thread can't blow the rate budget. v1 flattens broadly;
# selective "expand only match-rich branches" is a v2 refinement.
DEFAULT_MAX_DEPTH = 8
DEFAULT_MAX_MORECHILDREN_CALLS = 4

_OAUTH_BASE = "https://oauth.reddit.com"
_TOKEN_URL = "https://www.reddit.com/api/v1/access_token"
_REEL_SEP = "/"

# Data API resilience (mirrors the YouTube engine). A 429 (rate limit) or a
# transient 5xx is retried with bounded backoff; if it still fails, the call raises
# RedditApiError so the engine can stop the run GRACEFULLY (a halt with a clear
# reason) instead of letting a raw HTTP error crash the loop. 401/403 are NOT
# wrapped — they keep their httpx response so the CLI's auth-error handler can flag
# the integration as needs-reconnect.
_RETRYABLE_STATUS = frozenset({429, 500, 502, 503, 504})
_MAX_RETRIES = 3
_BACKOFF_BASE_S = 1.0
_MAX_BACKOFF_S = 8.0
_TOKEN_SKEW_S = 30.0  # refresh a bit before true expiry


class RedditApiError(RuntimeError):
    """A Data API call failed unrecoverably (rate limit / transient after retries).
    Carries the HTTP status so the engine can report why it stopped."""

    def __init__(self, message: str, status: Optional[int] = None):
        super().__init__(message)
        self.status = status


def _retry_after_seconds(response) -> Optional[float]:
    """Honor an integer ``Retry-After`` / ``X-Ratelimit-Reset`` header (delta-seconds)
    when present, capped. Reddit thins the budget via ``X-Ratelimit-*``."""
    try:
        headers = response.headers
        raw = (headers.get("Retry-After") or headers.get("X-Ratelimit-Reset") or "").strip()
    except Exception:  # noqa: BLE001 — header access must never break the retry
        return None
    try:
        return min(float(raw), _MAX_BACKOFF_S) if raw else None
    except ValueError:
        return None


@dataclass
class RedditSubmission:
    submission_id: str          # t3 id36 (without the "t3_" prefix)
    title: str = ""
    selftext: str = ""
    author: str = ""


@dataclass
class RedditComment:
    comment_id: str             # t1 id36
    text: str = ""
    author: str = ""
    depth: int = 0              # 0 = top-level; >0 = nested reply
    created_utc: float = 0.0    # re-poll watermark


class RedditApiPort(Protocol):
    """The only surface RedditFeed needs. The Data API adapter implements it for
    live runs; tests implement a fake (no network, no secret)."""

    def list_submissions(self, *, subreddit: str, query: Optional[str],
                          limit: int) -> list[RedditSubmission]: ...

    def comment_tree(self, submission_id: str, subreddit: str
                     ) -> list[RedditComment]: ...


def _caption(sub: RedditSubmission) -> str:
    """Title + selftext is the relevance signal (text-first; vision deferred)."""
    return f"{sub.title}\n{sub.selftext}".strip()


class RedditFeed(FeedSource):
    """Walks seeded subreddits + per-subreddit search queries; reads each
    submission's nested comment tree. Read-only: engagement methods stay the
    FeedSource no-ops."""

    def __init__(self, *, client: RedditApiPort,
                 subreddits: Sequence[str] = (), queries: Sequence[str] = (),
                 per_seed: int = DEFAULT_PER_SEED):
        self._client = client
        self._subreddits = tuple(subreddits)
        self._queries = tuple(queries)
        self._per_seed = per_seed
        # Remember which subreddit each submission came from, so fetch_comments can
        # build the right /r/{sub}/comments path without a second lookup.
        self._subreddit_of: dict[str, str] = {}

    def attach(self) -> None:
        """Pre-warm auth so a bad credential fails loudly here (an early, clean 401
        canary) rather than mid-walk. No-op when the client has no warm() hook."""
        warm = getattr(self._client, "warm", None)
        if callable(warm):
            warm()

    def walk(self) -> Iterator[Reel]:
        seen: set[str] = set()           # a submission can surface via listing AND search
        for subreddit in self._subreddits:
            yield from self._emit(seen, subreddit=subreddit, query=None)
            for query in self._queries:
                yield from self._emit(seen, subreddit=subreddit, query=query)

    def _emit(self, seen: set[str], *, subreddit: str,
              query: Optional[str]) -> Iterator[Reel]:
        subs = self._client.list_submissions(subreddit=subreddit, query=query,
                                             limit=self._per_seed)
        log.info("Reddit source · r/%s · %s · %d submission(s)", subreddit,
                 f"query={query!r}" if query else "new", len(subs))
        for s in subs:
            if s.submission_id in seen:
                continue
            seen.add(s.submission_id)
            self._subreddit_of[s.submission_id] = subreddit
            yield Reel(reel_id=s.submission_id, caption=_caption(s), author=s.author)

    def fetch_comments(self, reel_id: str, since_cursor: Optional[str]
                       ) -> tuple[list[Comment], Optional[str]]:
        subreddit = self._subreddit_of.get(reel_id, "")
        raw = self._client.comment_tree(reel_id, subreddit)
        # Re-poll watermark: only score comments newer than the last session's
        # high-water mark (cursor = newest created_utc scored). Bounds re-poll spend;
        # idempotent upsert on comment_id keeps it correct even without it.
        watermark = float(since_cursor) if since_cursor else 0.0
        fresh = [c for c in raw if c.created_utc > watermark]
        comments = [Comment(comment_id=f"{reel_id}{_REEL_SEP}{c.comment_id}",
                            username=c.author or "", text=c.text or "",
                            is_reply=c.depth > 0)
                    for c in fresh]
        newest = max((c.created_utc for c in raw), default=watermark)
        log.debug("Reddit comments · submission=%s n=%d (of %d) watermark=%.0f",
                  reel_id, len(comments), len(raw), newest)
        return comments, str(newest)

    def capture_frames(self, reel: Reel, n: int = 3) -> list[str]:
        return []  # text-first in v1; image/video OCR is a follow-up (PRD §6)

    def healthy(self) -> bool:
        return True  # stateless HTTP client; per-call errors surface on the call


class RedditDataApiClient:
    """Live Data API adapter (httpx). App-only ``client_credentials`` OAuth,
    read-only public data. Implements RedditApiPort."""

    def __init__(self, *, client_id: str, client_secret: str, user_agent: str):
        try:
            import httpx
        except Exception as e:  # pragma: no cover - exercised only on a live run
            raise RuntimeError("httpx is required for live Reddit runs") from e
        self._client_id = client_id
        self._client_secret = client_secret
        self._user_agent = user_agent
        self._httpx = httpx
        self._token: str = ""
        self._token_expiry: float = 0.0

    @classmethod
    def from_env(cls) -> "RedditDataApiClient":
        cid = os.environ.get("REDDIT_CLIENT_ID", "").strip()
        secret = os.environ.get("REDDIT_CLIENT_SECRET", "").strip()
        ua = os.environ.get("REDDIT_USER_AGENT", "").strip()
        if not (cid and secret and ua):
            raise RuntimeError(
                "Reddit live run needs REDDIT_CLIENT_ID, REDDIT_CLIENT_SECRET and "
                "REDDIT_USER_AGENT in the environment (.env)")
        return cls(client_id=cid, client_secret=secret, user_agent=ua)

    @classmethod
    def from_credentials(cls, credentials: Optional[dict]) -> "RedditDataApiClient":
        """Build from a per-org stored secret dict
        ({"client_id","client_secret","user_agent"}). Loud (and per-campaign
        foldable) when a field is missing."""
        creds = credentials or {}
        cid = str(creds.get("client_id", "")).strip()
        secret = str(creds.get("client_secret", "")).strip()
        ua = str(creds.get("user_agent", "")).strip()
        if not (cid and secret and ua):
            raise RuntimeError(
                "Reddit credentials missing client_id/client_secret/user_agent — "
                "reconnect Reddit in Settings")
        return cls(client_id=cid, client_secret=secret, user_agent=ua)

    # ---- auth ----
    def warm(self) -> None:
        """Mint the bearer token now (called from RedditFeed.attach)."""
        self._ensure_token()

    def _ensure_token(self) -> str:
        now = time.time()
        if self._token and now < self._token_expiry - _TOKEN_SKEW_S:
            return self._token
        r = self._httpx.post(
            _TOKEN_URL,
            data={"grant_type": "client_credentials"},
            auth=(self._client_id, self._client_secret),
            headers={"User-Agent": self._user_agent},
            timeout=30.0)
        # 401 here = bad app credentials → keep the response so the CLI auth handler
        # flags needs-reconnect (raise_for_status carries .response on the error).
        r.raise_for_status()
        body = r.json()
        token = str(body.get("access_token", "")).strip()
        if not token:
            raise RedditApiError("Reddit token endpoint returned no access_token")
        self._token = token
        self._token_expiry = now + float(body.get("expires_in", 3600))
        return token

    def _headers(self) -> dict:
        return {"Authorization": f"Bearer {self._ensure_token()}",
                "User-Agent": self._user_agent}

    # ---- requests ----
    def _get(self, path: str, params: dict) -> object:
        url = f"{_OAUTH_BASE}/{path}"
        for attempt in range(_MAX_RETRIES + 1):
            r = self._httpx.get(url, params={**params, "raw_json": 1},
                                headers=self._headers(), timeout=30.0)
            if r.status_code in _RETRYABLE_STATUS:
                if attempt < _MAX_RETRIES:
                    delay = _retry_after_seconds(r) or min(
                        _BACKOFF_BASE_S * (2 ** attempt), _MAX_BACKOFF_S)
                    log.warning("Reddit API %s on /%s — retry %d/%d in %.1fs",
                                r.status_code, path, attempt + 1, _MAX_RETRIES, delay)
                    time.sleep(delay)
                    continue
                raise RedditApiError(
                    f"Reddit API {r.status_code} on /{path} after {_MAX_RETRIES} "
                    "retries (rate limit / client throttled)", status=r.status_code)
            r.raise_for_status()  # 401/403 keep .response for the auth handler
            return r.json()
        raise RedditApiError(f"Reddit API unreachable on /{path}")  # pragma: no cover

    def _post(self, path: str, data: dict) -> object:
        url = f"{_OAUTH_BASE}/{path}"
        for attempt in range(_MAX_RETRIES + 1):
            r = self._httpx.post(url, data={**data, "raw_json": 1},
                                 headers=self._headers(), timeout=30.0)
            if r.status_code in _RETRYABLE_STATUS:
                if attempt < _MAX_RETRIES:
                    delay = _retry_after_seconds(r) or min(
                        _BACKOFF_BASE_S * (2 ** attempt), _MAX_BACKOFF_S)
                    time.sleep(delay)
                    continue
                raise RedditApiError(
                    f"Reddit API {r.status_code} on /{path} after {_MAX_RETRIES} "
                    "retries (rate limit / client throttled)", status=r.status_code)
            r.raise_for_status()
            return r.json()
        raise RedditApiError(f"Reddit API unreachable on /{path}")  # pragma: no cover

    # ---- RedditApiPort ----
    def list_submissions(self, *, subreddit: str, query: Optional[str],
                          limit: int) -> list[RedditSubmission]:
        if query:
            body = self._get(f"r/{subreddit}/search",
                             {"q": query, "restrict_sr": 1, "sort": "new",
                              "limit": min(limit, 100)})
        else:
            body = self._get(f"r/{subreddit}/new", {"limit": min(limit, 100)})
        return [_submission_from_child(ch) for ch in _children(body)
                if _kind(ch) == "t3"]

    def comment_tree(self, submission_id: str, subreddit: str
                     ) -> list[RedditComment]:
        body = self._get(f"r/{subreddit}/comments/{submission_id}",
                         {"depth": DEFAULT_MAX_DEPTH, "limit": 200})
        # The comments endpoint returns [t3 listing, t1 listing]; we want the second.
        listings = body if isinstance(body, list) else []
        comment_listing = listings[1] if len(listings) > 1 else {}
        out: list[RedditComment] = []
        more_calls = 0
        more_ids: list[str] = []
        for child in _children(comment_listing):
            more_calls += self._collect(child, depth=0, out=out, more_ids=more_ids)
        # Expand a bounded number of collapsed branches at the top of the tree.
        for batch_start in range(0, len(more_ids), 100):
            if more_calls >= DEFAULT_MAX_MORECHILDREN_CALLS:
                log.debug("Reddit more-children budget exhausted on %s", submission_id)
                break
            more_calls += 1
            ids = more_ids[batch_start:batch_start + 100]
            things = self._morechildren(submission_id, ids)
            for thing in things:
                if _kind(thing) == "t1":
                    out.append(_comment_from_child(thing))
        return out

    def _collect(self, child: dict, *, depth: int, out: list,
                 more_ids: list) -> int:
        """Recursively flatten one tree node into `out`; queue `more` ids. Returns 0
        (call accounting is done by the caller); kept depth-bounded."""
        kind = _kind(child)
        if kind == "more":
            data = child.get("data") or {}
            more_ids.extend(str(x) for x in data.get("children", []))
            return 0
        if kind != "t1":
            return 0
        out.append(_comment_from_child(child, depth=depth))
        if depth >= DEFAULT_MAX_DEPTH:
            return 0
        replies = (child.get("data") or {}).get("replies")
        if isinstance(replies, dict):
            for grand in _children(replies):
                self._collect(grand, depth=depth + 1, out=out, more_ids=more_ids)
        return 0

    def _morechildren(self, submission_id: str, ids: list) -> list:
        body = self._post("api/morechildren",
                          {"api_type": "json", "link_id": f"t3_{submission_id}",
                           "children": ",".join(ids)})
        if not isinstance(body, dict):
            return []
        things = (((body.get("json") or {}).get("data") or {}).get("things"))
        return things if isinstance(things, list) else []


# ---- tolerant JSON → dataclass mappers (never trust external shape) ----
def _children(listing: object) -> list:
    """The `data.children` list of a Reddit Listing, or [] for any other shape."""
    if not isinstance(listing, dict):
        return []
    data = listing.get("data")
    children = data.get("children") if isinstance(data, dict) else None
    return children if isinstance(children, list) else []


def _kind(child: object) -> str:
    return str(child.get("kind", "")) if isinstance(child, dict) else ""


def _submission_from_child(child: dict) -> RedditSubmission:
    d = child.get("data") or {}
    return RedditSubmission(
        submission_id=str(d.get("id", "")),
        title=str(d.get("title", "") or ""),
        selftext=str(d.get("selftext", "") or ""),
        author=str(d.get("author", "") or ""))


def _comment_from_child(child: dict, depth: int = 0) -> RedditComment:
    d = child.get("data") or {}
    # Reddit exposes a `depth` field; trust it when present, else use the walk depth.
    raw_depth = d.get("depth")
    eff_depth = int(raw_depth) if isinstance(raw_depth, (int, float)) else depth
    return RedditComment(
        comment_id=str(d.get("id", "")),
        text=str(d.get("body", "") or ""),
        author=str(d.get("author", "") or ""),
        depth=eff_depth,
        created_utc=float(d.get("created_utc", 0.0) or 0.0))
