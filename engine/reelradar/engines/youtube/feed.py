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
import time
from dataclasses import dataclass
from typing import Iterator, Optional, Protocol, Sequence

from ...core.feed import Comment, FeedSource, Reel
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


@dataclass
class YtComment:
    comment_id: str
    text: str = ""
    author: str = ""


class YouTubeApiPort(Protocol):
    """The only surface YouTubeFeed needs. The Data API adapter implements it for
    live runs; tests implement a fake (no network, no key)."""

    def search_videos(self, *, channel_id: Optional[str], query: Optional[str],
                       limit: int) -> list[YtVideo]: ...

    def list_comments(self, video_id: str, page_token: Optional[str]
                      ) -> tuple[list[YtComment], Optional[str]]: ...


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

    def attach(self) -> None:
        """Symmetry hook with CDPFeed; the API client needs no connection."""

    def walk(self) -> Iterator[Reel]:
        seen: set[str] = set()           # a video can surface via channel AND query
        for channel_id in self._channels:
            yield from self._emit(seen, channel_id=channel_id, query=None)
        for query in self._queries:
            yield from self._emit(seen, channel_id=None, query=query)

    def _emit(self, seen: set[str], *, channel_id: Optional[str],
              query: Optional[str]) -> Iterator[Reel]:
        videos = self._client.search_videos(channel_id=channel_id, query=query,
                                            limit=self._per_seed)
        log.info("YouTube source · %s · %d video(s)",
                 f"channel={channel_id}" if channel_id else f"query={query!r}", len(videos))
        for v in videos:
            if v.video_id in seen:
                continue
            seen.add(v.video_id)
            yield Reel(reel_id=v.video_id, caption=_caption(v), author=v.channel_title)

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
                                   channel_title=sn.get("channelTitle", "")))
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
