"""YouTube API resilience: retry transient rate-limits, then halt GRACEFULLY.

A 429 (rate limit / daily quota) must not crash the run with a raw HTTP traceback.
`_get` retries transient 429/5xx with backoff; a persistent failure raises
YouTubeApiError, which the session folds into a clean halt (leads found so far are
kept; the summary carries halt_reason so the back-to-back CLI loop stops).
"""
import os
import tempfile

import pytest

from aizu.core.feed import Comment, Reel
from aizu.core.router import Decision
from aizu.engines.youtube import feed as ytfeed
from aizu.engines.youtube.feed import YouTubeApiError, YouTubeDataApiClient
from aizu.engines.youtube.session import run_session


# ---------------- _get retry/backoff ----------------

class _Resp:
    def __init__(self, status, body=None, headers=None):
        self.status_code = status
        self._body = body or {}
        self.headers = headers or {}

    def json(self):
        return self._body

    def raise_for_status(self):
        if self.status_code >= 400:
            raise FakeHttpx.HTTPStatusError(f"HTTP {self.status_code}")


class FakeHttpx:
    class HTTPStatusError(Exception):
        pass

    def __init__(self, responses):
        self._responses = list(responses)
        self.calls = 0

    def get(self, url, params=None, timeout=None):
        self.calls += 1
        return self._responses.pop(0)


def _client(responses, monkeypatch):
    monkeypatch.setattr(ytfeed.time, "sleep", lambda _s: None)  # no real backoff wait
    c = YouTubeDataApiClient("key")
    c._httpx = FakeHttpx(responses)
    return c


def test_get_recovers_after_transient_429(monkeypatch):
    c = _client([_Resp(429), _Resp(200, {"ok": 1})], monkeypatch)
    assert c._get("search", {}) == {"ok": 1}
    assert c._httpx.calls == 2          # retried once, then succeeded


def test_get_raises_youtube_api_error_after_persistent_429(monkeypatch):
    c = _client([_Resp(429)] * (ytfeed._MAX_RETRIES + 1), monkeypatch)
    with pytest.raises(YouTubeApiError) as ei:
        c._get("search", {})
    assert ei.value.status == 429
    assert c._httpx.calls == ytfeed._MAX_RETRIES + 1


def test_get_honors_retry_after_header(monkeypatch):
    waited = []
    monkeypatch.setattr(ytfeed.time, "sleep", lambda s: waited.append(s))
    c = YouTubeDataApiClient("key")
    c._httpx = FakeHttpx([_Resp(429, headers={"Retry-After": "2"}), _Resp(200, {"ok": 1})])
    c._get("search", {})
    assert waited == [2.0]


def test_get_does_not_wrap_403(monkeypatch):
    # 401/403 must keep raising the raw httpx error so the CLI can flag needs-reconnect.
    c = _client([_Resp(403)], monkeypatch)
    with pytest.raises(FakeHttpx.HTTPStatusError):
        c._get("search", {})


# ---------------- session graceful halt ----------------

class SpyRouter:
    def classify_text(self, *, instruction, content, campaign_id, stage,
                      session_id=None, system=None):
        low = content.lower()
        if stage == "relevance":
            rel = any(k in low for k in ("acme", "app"))
            return Decision(label="relevant" if rel else "irrelevant",
                            score=0.9 if rel else 0.1, confidence=0.96)
        lead = any(k in low for k in ("price", "+1", "pricing"))
        return Decision(label="yes" if lead else "no",
                        score=0.92 if lead else 0.1, confidence=0.96,
                        extracted={"phone": "+14155550142"} if lead else {})

    def classify_image(self, **_kw):  # pragma: no cover - must never be called
        raise AssertionError("YouTube must not call vision")


class FlakyFeed:
    """Yields one relevant video (with a matching comment), then the API gives out."""

    def walk(self):
        yield Reel(reel_id="v1", caption="Acme app demo, free trial", author="x")
        raise YouTubeApiError("YouTube Data API 429 on /search after 3 retries "
                              "(rate limit or daily quota exhausted)", status=429)

    def fetch_comments(self, reel_id, cursor):
        return [Comment(comment_id="v1/c1", username="a", text="pricing? +1 1")], None

    def capture_frames(self, reel, n=3):
        return []

    def healthy(self):
        return True


def _campaign():
    from aizu.core.config import campaign_from_brief
    return campaign_from_brief("yt-flaky", {
        "platform": "youtube", "threshold": 0.7,
        "relevance_def": "saas product", "match_def": "buyer intent",
        "extract_def": "- phone"})


def _store():
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    from aizu.core.store import Store
    return Store(path)


def test_session_halts_gracefully_on_api_error():
    store = _store()
    summary = run_session(campaign=_campaign(), store=store, router=SpyRouter(),
                          feed=FlakyFeed(), soul=None, pacer=None, run_id="run-q")
    # No exception escaped; the run reports the halt so the CLI loop stops.
    assert summary["halt_reason"] and "429" in summary["halt_reason"]
    # The lead found BEFORE the API gave out is still persisted.
    assert summary["matches"] == 1
    rows = store.matches("yt-flaky")
    assert [r["comment_id"] for r in rows] == ["v1/c1"]
    # A halt health-flag was raised for the operator.
    assert any(f["kind"] == "youtube_api" for f in store.open_flags())
