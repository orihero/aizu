"""YouTube FeedSource via the official Data API v3 (PRD: docs/prd/youtube-lead-agent-PRD.md).

Read-only, API-key auth. Discovery is the operator's seeded channels
(`seed_channels` = channel ids) and search queries (`seed_hashtags` reused as
query terms); comments come from `commentThreads.list` (paginated). A video ≈
`Reel` (title+description → caption, channel → author); a top-level comment ≈
`Comment`.

The official API replaces the brittle InnerTube-interception approach: endpoints
and response shapes are documented and stable. HTTP is isolated behind
`YouTubeApiPort` so the video→Reel / comment→Comment mapping is pure and
unit-tested with a fake; the live `YouTubeDataApiClient` uses httpx.

Note: the Data API exposes no video frames, so the vision tier does not apply —
relevance is judged on the title + description text (sufficient for v1). The
spoken-transcript tier (PRD §6) remains a follow-up (needs `router.transcribe`).
"""
from __future__ import annotations

import os
import re
import time
from dataclasses import dataclass
from typing import Iterator, Optional, Protocol, Sequence

from ...core.feed import (SOURCE_ACCOUNT, SOURCE_HASHTAG, Comment, FeedSource,
                          Reel, SourceOutcome)
from ...core.logsetup import get_logger

log = get_logger(__name__)

# Videos to pull per seed (channel or query) each session. The Data API caps a
# single search.list page at 50; keep it modest for quota (search = 100 units).
DEFAULT_PER_SEED = 25

_API_BASE = "https://www.googleapis.com/youtube/v3"
_REEL_SEP = "/"

# Data API resilience. A 429 (rate limit / daily-quota exhausted) or a transient
# 5xx is retried with bounded backoff; if it still fails, the call raises
# YouTubeApiError so the engine can stop the run GRACEFULLY (a halt with a clear
# reason) instead of letting a raw HTTP error crash the whole loop. 401/403 are
# NOT wrapped — they keep their httpx response so the CLI's auth-error handler can
# still flag the integration as needs-reconnect.
_RETRYABLE_STATUS = frozenset({429, 500, 502, 503, 504})
_MAX_RETRIES = 3
_BACKOFF_BASE_S = 1.0
_MAX_BACKOFF_S = 8.0


class YouTubeApiError(RuntimeError):
    """A Data API call failed unrecoverably (rate limit / quota / transient after
    retries). Carries the HTTP status so the engine can report why it stopped."""

    def __init__(self, message: str, status: Optional[int] = None):
        super().__init__(message)
        self.status = status


class YouTubeSeedError(RuntimeError):
    """ONE seed is bad — a malformed channel id, or a resource that is gone.

    Deliberately NOT a `YouTubeApiError`: that class means "the API is done
    talking to us" and the session catches it as a halt. A bad seed is a
    per-source problem and must skip that source, not end the run.

    This is the class of failure that used to escape everything. `channelId=@name`
    (a handle rather than a `UC…` id) returns 400, `_get` reached
    `raise_for_status()`, and the resulting `httpx.HTTPStatusError` is not a
    `YouTubeApiError` — so it blew straight past the catch in
    `youtube/session.py` and crashed the whole run on one mistyped seed."""

    def __init__(self, message: str, status: Optional[int] = None):
        super().__init__(message)
        self.status = status


# A canonical channel id: literal "UC" + 22 chars of base64url. Anything else the
# operator typed (a @handle, a legacy username, a full URL) has to be resolved.
_CHANNEL_ID_RE = re.compile(r"^UC[A-Za-z0-9_-]{22}$")
# Statuses that mean "this request/resource is wrong", not "this key is wrong"
# (401/403, which stay raw so cli._is_auth_error can flag needs-reconnect).
_SEED_ERROR_STATUS = frozenset({400, 404})


def _retry_after_seconds(response) -> Optional[float]:
    """Honor an integer ``Retry-After`` header (delta-seconds) when present, capped."""
    try:
        raw = (response.headers.get("Retry-After", "") or "").strip()
    except Exception:  # noqa: BLE001 — header access must never break the retry
        return None
    return min(float(raw), _MAX_BACKOFF_S) if raw.isdigit() else None


@dataclass
class YtVideo:
    video_id: str
    title: str = ""
    description: str = ""
    channel_title: str = ""
    # `snippet.channelId` — the `UC…` id, already in every search payload and
    # dropped until now. `channel_title` is a display name that changes on a
    # rename and cannot be fed back in as a seed; this can.
    channel_id: str = ""


@dataclass
class YtComment:
    comment_id: str
    text: str = ""
    author: str = ""


class YouTubeApiPort(Protocol):
    """The only surface YouTubeFeed needs. The Data API adapter implements it for
    live runs; tests implement a fake (no network, no key)."""

    def resolve_channel(self, seed: str) -> Optional[str]:
        """Turn whatever the operator typed into a canonical `UC…` channel id.

        Accepts `UCxxxx…` (returned untouched, no request), `@handle`,
        `youtube.com/@handle`, `youtube.com/channel/UC…`, `youtube.com/c/Name`
        and `youtube.com/user/Name`. Returns None when the channel does not
        exist — the caller records a dead seed rather than walking a URL that
        cannot produce anything.

        Costs 1 quota unit (`channels.list` is a flat 1), and the result is cached
        per client so a multi-session run resolves each seed once. NEVER uses
        `search.list` for this: that would be 100 units AND, since June 2026, one
        of only 100 searches available that day."""
        raw = str(seed or "").strip()
        if not raw:
            return None
        if raw in self._channel_cache:
            return self._channel_cache[raw]
        handle, username, channel_id = _parse_channel_seed(raw)
        resolved: Optional[str] = channel_id
        if resolved is None:
            # `forHandle` covers the modern @handle; `forUsername` the legacy
            # /user/ names that predate them. Try the one the shape implies, then
            # the other — an operator who types a bare word could mean either.
            attempts = ([{"forHandle": handle}] if handle else [])
            attempts += ([{"forUsername": username}] if username else [])
            for params in attempts:
                try:
                    body = self._get("channels", {"part": "id", **params})
                except YouTubeSeedError:
                    continue          # this lookup shape is wrong; try the next
                items = [i for i in (body.get("items") or []) if isinstance(i, dict)]
                if items and items[0].get("id"):
                    resolved = str(items[0]["id"])
                    break
        self._channel_cache[raw] = resolved
        if resolved is None:
            log.warning("YouTube seed %r does not resolve to a channel", raw)
        elif resolved != raw:
            log.info("YouTube seed %r resolved to %s", raw, resolved)
        return resolved

    def search_videos(self, *, channel_id: Optional[str], query: Optional[str],
                       limit: int) -> list[YtVideo]: ...

    def list_comments(self, video_id: str, page_token: Optional[str]
                      ) -> tuple[list[YtComment], Optional[str]]: ...

    def resolve_channel(self, seed: str) -> Optional[str]: ...


def _seed_error_detail(response) -> str:
    """The API's own `error.message`, when it sends one — it names the offending
    parameter, which is the difference between "400" and "channelId is not a valid
    channel id". Never raises: this runs on an error path."""
    try:
        body = response.json()
        msg = ((body.get("error") or {}).get("message") or "").strip()
        return msg[:200] or "no detail"
    except Exception:  # noqa: BLE001 — a non-JSON error body is still an error
        return "no detail"


def _parse_channel_seed(raw: str) -> tuple[Optional[str], Optional[str], Optional[str]]:
    """Split a seed into `(handle, username, channel_id)`, at most one non-None.

    Pure and side-effect free so the URL/handle grammar can be tested without a
    key or a network."""
    text = raw.strip()
    lowered = text.lower()
    for prefix in ("https://", "http://"):
        if lowered.startswith(prefix):
            text = text[len(prefix):]
            break
    text = text.split("?", 1)[0].split("#", 1)[0].strip("/")
    if not text:
        return (None, None, None)
    parts = [p for p in text.split("/") if p]
    # Drop a leading host, with or without a scheme: operators paste
    # `youtube.com/@name` as often as the full URL, and treating the host as the
    # channel name resolves every such seed to nothing.
    head_l = parts[0].lower()
    if head_l == "youtu.be" or head_l == "youtube.com" or head_l.endswith(".youtube.com"):
        parts = parts[1:]
    if not parts:
        return (None, None, None)
    head = parts[0]
    if head in ("channel", "c", "user") and len(parts) > 1:
        target = parts[1]
        if head == "channel":
            return (None, None, target) if _CHANNEL_ID_RE.match(target) else (None, target, None)
        if head == "user":
            return (None, target, None)
        # /c/Name is a vanity URL; it resolves as a handle far more often than not.
        return (target.lstrip("@"), target, None)
    if head.startswith("@"):
        return (head, None, None)
    if _CHANNEL_ID_RE.match(head):
        return (None, None, head)
    # A bare word: could be a handle or a legacy username. Try both.
    return (f"@{head}", head, None)


def _caption(video: YtVideo) -> str:
    """Title + description is the relevance signal (no frames over the API)."""
    return f"{video.title}\n{video.description}".strip()


class YouTubeFeed(FeedSource):
    """Walks seeded channels + search queries; reads each video's top-level
    comments. Read-only: engagement methods stay the FeedSource no-ops."""

    def __init__(self, *, client: YouTubeApiPort,
                 channels: Sequence[str] = (), queries: Sequence[str] = (),
                 per_seed: int = DEFAULT_PER_SEED):
        self._client = client
        self._channels = tuple(channels)
        self._queries = tuple(queries)
        self._per_seed = per_seed
        # Resolved UC-id -> the seed string the brief actually names. The ledger
        # has to report the operator's own term, not an id they never typed.
        self._seed_of: dict[str, str] = {}
        # Seeds that resolved to nothing — reported once, as dead sources.
        self._dead_channels: list[str] = []

    def attach(self) -> None:
        """Resolve every channel seed to a canonical `UC…` id, once, before the
        walk starts.

        This is the pre-flight the Campaign Lab audit asked for (Remedy Sheet #2 /
        Remedy E), placed here rather than in `dispatch.build_feed` for the same
        reason Reddit pre-mints its OAuth token in `attach()`: it keeps
        platform-specific knowledge inside the platform package, and `build_feed`
        calls `attach()` immediately anyway.

        Two failures it removes. A `@handle` seed returned 400 from
        `search.list`, which escaped `YouTubeApiError` and crashed the whole run;
        it is now resolved to a real id (1 quota unit) before anything walks. A
        seed that resolves to nothing is dropped and recorded as an unavailable
        source, instead of being walked forever and returning zero videos —
        previously indistinguishable from a channel that is merely quiet.

        Never raises: an unresolvable seed is a dead seed, and a client with no
        `resolve_channel` (a test fake, an older adapter) is left exactly as it
        was."""
        resolve = getattr(self._client, "resolve_channel", None)
        if not callable(resolve) or not self._channels:
            return
        live: list[str] = []
        for seed in self._channels:
            try:
                resolved = resolve(seed)
            except Exception:  # noqa: BLE001 — a failed lookup is not a dead seed
                log.warning("YouTube seed %r could not be resolved (keeping it)",
                            seed, exc_info=True)
                live.append(seed)
                continue
            if resolved:
                live.append(resolved)
                self._seed_of[resolved] = seed
            else:
                self._dead_channels.append(seed)
        self._channels = tuple(live)
        if self._dead_channels:
            log.warning("YouTube dropping %d unresolvable channel seed(s): %s",
                        len(self._dead_channels), ", ".join(self._dead_channels))

    def walk(self) -> Iterator[Reel]:
        seen: set[str] = set()           # a video can surface via channel AND query
        # Report the seeds that resolved to nothing FIRST, so `source_stats` learns
        # they are dead on the very first run instead of after three of them.
        for dead in self._dead_channels:
            self._record_source(SourceOutcome(source=dead, kind=SOURCE_ACCOUNT,
                                              unavailable=True))
        for channel_id in self._channels:
            yield from self._emit(seen, channel_id=channel_id, query=None)
        for query in self._queries:
            yield from self._emit(seen, channel_id=None, query=query)

    def _emit(self, seen: set[str], *, channel_id: Optional[str],
              query: Optional[str]) -> Iterator[Reel]:
        """One seed's worth of videos, stamped with the seed that produced them.

        An empty `items[]` here is indistinguishable from a quiet channel by
        design (the API returns 200 either way), so it is recorded as a dry
        source, never as a dead one — only an explicit not-found may set
        `unavailable`."""
        seed = (self._seed_of.get(channel_id, channel_id) if channel_id
                else (query or ""))
        kind = SOURCE_ACCOUNT if channel_id else SOURCE_HASHTAG
        try:
            videos = self._client.search_videos(channel_id=channel_id, query=query,
                                                limit=self._per_seed)
        except YouTubeSeedError as e:
            # ONE seed is bad. Skip it, record it, keep walking — this is the whole
            # reason the class exists separately from YouTubeApiError.
            log.warning("YouTube source rejected · %s · %s",
                        f"channel={channel_id}" if channel_id else f"query={query!r}", e)
            self._record_source(SourceOutcome(source=seed, kind=kind,
                                              unavailable=True))
            return
        log.info("YouTube source · %s · %d video(s)",
                 f"channel={channel_id}" if channel_id else f"query={query!r}", len(videos))
        fresh = 0
        try:
            for v in videos:
                if v.video_id in seen:
                    continue
                seen.add(v.video_id)
                fresh += 1
                yield Reel(reel_id=v.video_id, caption=_caption(v),
                           author=v.channel_title, author_id=v.channel_id,
                           source=seed)
        finally:
            self._record_source(SourceOutcome(source=seed, kind=kind,
                                              yielded=fresh,
                                              carried_over=len(videos) - fresh))

    def fetch_comments(self, reel_id: str, since_cursor: Optional[str]
                       ) -> tuple[list[Comment], Optional[str]]:
        raw, next_token = self._client.list_comments(reel_id, since_cursor)
        comments = [Comment(comment_id=f"{reel_id}{_REEL_SEP}{c.comment_id}",
                            username=c.author or "", text=c.text or "")
                    for c in raw]
        log.debug("YouTube comments · video=%s n=%d", reel_id, len(comments))
        return comments, next_token

    def capture_frames(self, reel: Reel, n: int = 3) -> list[str]:
        return []  # the Data API exposes no frames; relevance is text-only here

    def healthy(self) -> bool:
        return True  # stateless HTTP client; per-call errors surface on the call


class YouTubeDataApiClient:
    """Live Data API v3 adapter (httpx). API key auth, read-only public data."""

    def __init__(self, api_key: str):
        try:
            import httpx
        except Exception as e:  # pragma: no cover - exercised only on a live run
            raise RuntimeError("httpx is required for live YouTube runs") from e
        self._key = api_key
        self._httpx = httpx
        # seed-as-typed -> UC-id (or None for "does not exist"). Resolution costs a
        # quota unit, and a target-leads run builds a fresh feed per session.
        self._channel_cache: dict[str, Optional[str]] = {}

    @classmethod
    def from_env(cls) -> "YouTubeDataApiClient":
        key = os.environ.get("YOUTUBE_API_KEY", "")
        if not key:
            raise RuntimeError("YouTube live run needs YOUTUBE_API_KEY in the environment (.env)")
        return cls(key)

    @classmethod
    def from_credentials(cls, credentials: Optional[dict]) -> "YouTubeDataApiClient":
        """Build from a per-org stored secret dict ({"api_key": ...}). Loud (and
        per-campaign foldable) when the key is missing."""
        key = str((credentials or {}).get("api_key", "")).strip()
        if not key:
            raise RuntimeError(
                "YouTube credentials missing api_key — reconnect YouTube in Settings")
        return cls(key)

    def _get(self, path: str, params: dict) -> dict:
        params = {**params, "key": self._key}
        url = f"{_API_BASE}/{path}"
        for attempt in range(_MAX_RETRIES + 1):
            r = self._httpx.get(url, params=params, timeout=30.0)
            if r.status_code in _RETRYABLE_STATUS:
                if attempt < _MAX_RETRIES:
                    delay = _retry_after_seconds(r) or min(
                        _BACKOFF_BASE_S * (2 ** attempt), _MAX_BACKOFF_S)
                    log.warning("YouTube Data API %s on /%s — retry %d/%d in %.1fs",
                                r.status_code, path, attempt + 1, _MAX_RETRIES, delay)
                    time.sleep(delay)
                    continue
                raise YouTubeApiError(
                    f"YouTube Data API {r.status_code} on /{path} after "
                    f"{_MAX_RETRIES} retries (rate limit or daily quota exhausted)",
                    status=r.status_code)
            # 400/404 mean the REQUEST is wrong (a handle where a UC-id belongs, a
            # deleted resource), not that the key is. Raising them as a seed error
            # is what stops one mistyped seed from ending the run.
            if r.status_code in _SEED_ERROR_STATUS:
                raise YouTubeSeedError(
                    f"YouTube Data API {r.status_code} on /{path} "
                    f"({_seed_error_detail(r)})", status=r.status_code)
            # Non-retryable: 401/403 raise the raw httpx error (keeps .response so the
            # CLI auth handler can flag needs-reconnect); other 4xx surface loudly.
            r.raise_for_status()
            return r.json()
        raise YouTubeApiError(f"YouTube Data API unreachable on /{path}")  # pragma: no cover

    def search_videos(self, *, channel_id: Optional[str], query: Optional[str],
                       limit: int) -> list[YtVideo]:
        params: dict = {"part": "snippet", "type": "video", "order": "date",
                        "maxResults": min(limit, 50)}
        if channel_id:
            params["channelId"] = channel_id
        if query:
            params["q"] = query
        body = self._get("search", params)
        out: list[YtVideo] = []
        for item in body.get("items", []):
            vid = (item.get("id") or {}).get("videoId")
            sn = item.get("snippet") or {}
            if vid:
                out.append(YtVideo(video_id=vid, title=sn.get("title", ""),
                                   description=sn.get("description", ""),
                                   channel_title=sn.get("channelTitle", ""),
                                   channel_id=str(sn.get("channelId") or "")))
        return out

    def list_comments(self, video_id: str, page_token: Optional[str]
                      ) -> tuple[list[YtComment], Optional[str]]:
        params: dict = {"part": "snippet", "videoId": video_id, "maxResults": 100,
                        "textFormat": "plainText"}
        if page_token:
            params["pageToken"] = page_token
        body = self._get("commentThreads", params)
        out: list[YtComment] = []
        for item in body.get("items", []):
            top = ((item.get("snippet") or {}).get("topLevelComment") or {})
            sn = top.get("snippet") or {}
            cid = top.get("id")
            if cid:
                out.append(YtComment(comment_id=cid,
                                     text=sn.get("textDisplay", ""),
                                     author=sn.get("authorDisplayName", "")))
        return out, body.get("nextPageToken")
