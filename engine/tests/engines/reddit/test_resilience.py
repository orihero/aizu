"""Reddit API resilience: mint a token, retry transient rate-limits, then halt
GRACEFULLY.

A 429 (rate limit / client throttle) must not crash the run with a raw HTTP
traceback. `_get` retries transient 429/5xx with backoff; a persistent failure
raises RedditApiError, which the session folds into a clean halt (leads found so
far are kept; the summary carries halt_reason so the back-to-back CLI loop stops).
401/403 keep the raw httpx error so the CLI flags needs-reconnect.
"""
import os
import tempfile

import pytest

from reelradar.core.feed import Comment, Reel
from reelradar.core.router import Decision
from reelradar.engines.reddit import feed as rfeed
from reelradar.engines.reddit.feed import RedditApiError, RedditDataApiClient
from reelradar.engines.reddit.session import run_session


# ---------------- token + _get retry/backoff ----------------

class _Resp:
    def __init__(self, status, body=None, headers=None):
        self.status_code = status
        self._body = body if body is not None else {}
        self.headers = headers or {}

    def json(self):
        return self._body

    def raise_for_status(self):
        if self.status_code >= 400:
            raise FakeHttpx.HTTPStatusError(f"HTTP {self.status_code}")


class FakeHttpx:
    class HTTPStatusError(Exception):
        pass

    def __init__(self, get_responses=None, post_responses=None, token_response=None):
        self._get = list(get_responses or [])
        self._post = list(post_responses or [])
        self._token = token_response or _Resp(200, {"access_token": "tok", "expires_in": 3600})
        self.get_calls = 0
        self.post_calls = 0

    def post(self, url, data=None, auth=None, headers=None, timeout=None):
        self.post_calls += 1
        if url == rfeed._TOKEN_URL:
            return self._token
        return self._post.pop(0)

    def get(self, url, params=None, headers=None, timeout=None):
        self.get_calls += 1
        return self._get.pop(0)


def _client(get_responses, monkeypatch, token_response=None):
    monkeypatch.setattr(rfeed.time, "sleep", lambda _s: None)  # no real backoff wait
    c = RedditDataApiClient(client_id="cid", client_secret="sec", user_agent="ua")
    c._httpx = FakeHttpx(get_responses=get_responses, token_response=token_response)
    return c


def test_get_recovers_after_transient_429(monkeypatch):
    c = _client([_Resp(429), _Resp(200, {"ok": 1})], monkeypatch)
    assert c._get("r/x/new", {}) == {"ok": 1}
    assert c._httpx.get_calls == 2          # retried once, then succeeded


def test_get_raises_reddit_api_error_after_persistent_429(monkeypatch):
    c = _client([_Resp(429)] * (rfeed._MAX_RETRIES + 1), monkeypatch)
    with pytest.raises(RedditApiError) as ei:
        c._get("r/x/new", {})
    assert ei.value.status == 429
    assert c._httpx.get_calls == rfeed._MAX_RETRIES + 1


def test_get_honors_ratelimit_reset_header(monkeypatch):
    waited = []
    monkeypatch.setattr(rfeed.time, "sleep", lambda s: waited.append(s))
    c = RedditDataApiClient(client_id="cid", client_secret="sec", user_agent="ua")
    c._httpx = FakeHttpx(get_responses=[
        _Resp(429, headers={"X-Ratelimit-Reset": "2"}), _Resp(200, {"ok": 1})])
    c._get("r/x/new", {})
    assert waited == [2.0]


def test_get_does_not_wrap_403(monkeypatch):
    # 401/403 must keep raising the raw httpx error so the CLI flags needs-reconnect.
    c = _client([_Resp(403)], monkeypatch)
    with pytest.raises(FakeHttpx.HTTPStatusError):
        c._get("r/x/new", {})


def test_token_mint_401_raises_raw_for_reconnect(monkeypatch):
    # A bad app credential fails at the token endpoint with the raw httpx error
    # (carrying .response), the auth-error path the CLI keys off.
    c = _client([], monkeypatch, token_response=_Resp(401))
    with pytest.raises(FakeHttpx.HTTPStatusError):
        c.warm()


def test_token_is_cached_across_calls(monkeypatch):
    c = _client([_Resp(200, {"a": 1}), _Resp(200, {"b": 2})], monkeypatch)
    c._get("r/x/new", {})
    c._get("r/y/new", {})
    assert c._httpx.post_calls == 1         # token minted once, reused


# ---------------- session graceful halt ----------------

class _HaltRouter:
    def classify_text(self, *, instruction, content, campaign_id, stage,
                      session_id=None, system=None):
        low = content.lower()
        if stage == "relevance":
            rel = any(k in low for k in ("acme", "app"))
            return Decision(label="relevant" if rel else "irrelevant",
                            score=0.9 if rel else 0.1, confidence=0.96)
        lead = any(k in low for k in ("price", "+1"))
        return Decision(label="yes" if lead else "no",
                        score=0.92 if lead else 0.1, confidence=0.96,
                        extracted={"phone": "+14155550142"} if lead else {})

    def classify_image(self, **_kw):  # pragma: no cover - must never be called
        raise AssertionError("Reddit must not call vision")


class FlakyFeed:
    """Yields one relevant submission (with a matching comment), then the API gives out."""

    def walk(self):
        yield Reel(reel_id="s1", caption="Acme app demo, free trial", author="x")
        raise RedditApiError("Reddit API 429 on /r/x/new after 3 retries "
                             "(rate limit / client throttled)", status=429)

    def fetch_comments(self, reel_id, cursor):
        return [Comment(comment_id="s1/c1", username="a", text="pricing? +1 415 555 0142")], None

    def capture_frames(self, reel, n=3):
        return []

    def healthy(self):
        return True


def _campaign():
    from reelradar.core.config import campaign_from_brief
    return campaign_from_brief("reddit-flaky", {
        "platform": "reddit", "threshold": 0.7,
        "relevance_def": "saas product", "match_def": "buyer intent",
        "extract_def": "- phone"})


def _store():
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    from reelradar.core.store import Store
    return Store(path)


def test_session_halts_gracefully_on_api_error():
    store = _store()
    summary = run_session(campaign=_campaign(), store=store, router=_HaltRouter(),
                          feed=FlakyFeed(), soul=None, pacer=None, run_id="run-q")
    # No exception escaped; the run reports the halt so the CLI loop stops.
    assert summary["halt_reason"] and "429" in summary["halt_reason"]
    # The lead found BEFORE the API gave out is still persisted.
    assert summary["matches"] == 1
    rows = store.matches("reddit-flaky")
    assert [r["comment_id"] for r in rows] == ["s1/c1"]
    # A halt health-flag was raised for the operator.
    assert any(f["kind"] == "reddit_api" for f in store.open_flags())
