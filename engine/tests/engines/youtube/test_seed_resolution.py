"""YouTube seed resolution (Campaign Lab, Remedy Sheet #2 — audit bug #2).

An `@handle` seed made `search.list` return 400; `_get` reached
`raise_for_status()`, and the resulting httpx error is NOT a `YouTubeApiError`, so
it escaped the catch in `youtube/session.py` and crashed the entire run. A
nonexistent UC-id was the opposite failure: a silent 0 videos, indistinguishable
from a quiet channel.
"""
import pytest

from aizu.core.feed import SOURCE_ACCOUNT, SOURCE_HASHTAG
from aizu.engines.youtube.feed import (YouTubeApiError, YouTubeFeed,
                                       YouTubeSeedError, YtVideo,
                                       _parse_channel_seed)

UC = "UCabcdefghijklmnopqrstuv"


@pytest.mark.parametrize("seed,expected", [
    (UC, (None, None, UC)),
    ("@mkbhd", ("@mkbhd", None, None)),
    ("https://youtube.com/@mkbhd", ("@mkbhd", None, None)),
    ("youtube.com/@mkbhd", ("@mkbhd", None, None)),
    (f"www.youtube.com/channel/{UC}", (None, None, UC)),
    ("youtube.com/user/PewDiePie", (None, "PewDiePie", None)),
    ("bare", ("@bare", "bare", None)),
    ("", (None, None, None)),
    ("youtube.com", (None, None, None)),
])
def test_seed_grammar(seed, expected):
    assert _parse_channel_seed(seed) == expected


class _Client:
    """Fake port: canned resolutions + per-seed search behaviour."""

    def __init__(self, resolve=None, videos=None, raises=None):
        self._resolve = resolve or {}
        self._videos = videos or {}
        self._raises = raises or {}
        self.searches = []

    def resolve_channel(self, seed):
        return self._resolve.get(seed, seed if seed.startswith("UC") else None)

    def search_videos(self, *, channel_id, query, limit):
        key = channel_id or query
        self.searches.append(key)
        if key in self._raises:
            raise self._raises[key]
        return [YtVideo(video_id=v, title=v) for v in self._videos.get(key, [])]

    def list_comments(self, video_id, page_token):
        return [], None


def _feed(client, **kw):
    f = YouTubeFeed(client=client, **kw)
    f.outcomes = []
    f.on_source_done = f.outcomes.append
    return f


def test_a_handle_seed_is_resolved_before_anything_walks():
    client = _Client(resolve={"@mkbhd": UC}, videos={UC: ["v1"]})
    feed = _feed(client, channels=("@mkbhd",))
    feed.attach()
    assert [r.reel_id for r in feed.walk()] == ["v1"]
    assert client.searches == [UC]          # never searched with the raw handle


def test_the_ledger_reports_the_operators_own_seed_not_the_resolved_id():
    client = _Client(resolve={"@mkbhd": UC}, videos={UC: ["v1"]})
    feed = _feed(client, channels=("@mkbhd",))
    feed.attach()
    list(feed.walk())
    assert [(o.source, o.kind) for o in feed.outcomes] == [("@mkbhd", SOURCE_ACCOUNT)]


def test_an_unresolvable_seed_is_dropped_and_recorded_as_dead():
    client = _Client(resolve={"@ghost": None})
    feed = _feed(client, channels=("@ghost",))
    feed.attach()
    assert list(feed.walk()) == []
    assert client.searches == []            # never walked at all
    (out,) = feed.outcomes
    assert (out.source, out.unavailable) == ("@ghost", True)


def test_one_bad_seed_does_not_stop_the_others():
    client = _Client(resolve={"@ghost": None, "@ok": UC}, videos={UC: ["v1"]})
    feed = _feed(client, channels=("@ghost", "@ok"))
    feed.attach()
    assert [r.reel_id for r in feed.walk()] == ["v1"]


def test_a_seed_error_mid_walk_skips_that_source_and_keeps_going():
    """YouTubeSeedError is deliberately NOT a YouTubeApiError: the latter is a
    halt, this is one bad source."""
    client = _Client(videos={"good": ["v1"]},
                     raises={"bad": YouTubeSeedError("400 bad channelId", status=400)})
    feed = _feed(client, queries=("bad", "good"))
    feed.attach()
    assert [r.reel_id for r in feed.walk()] == ["v1"]
    bad, good = feed.outcomes
    assert (bad.source, bad.kind, bad.unavailable) == ("bad", SOURCE_HASHTAG, True)
    assert (good.source, good.yielded) == ("good", 1)


def test_a_real_api_halt_still_propagates():
    # Quota exhaustion must NOT be swallowed as a bad seed.
    client = _Client(raises={"q": YouTubeApiError("quota exhausted", status=403)})
    feed = _feed(client, queries=("q",))
    feed.attach()
    with pytest.raises(YouTubeApiError):
        list(feed.walk())


def test_a_uc_id_seed_costs_no_resolution_request():
    calls = []

    class _Counting(_Client):
        def resolve_channel(self, seed):
            calls.append(seed)
            return super().resolve_channel(seed)

    client = _Counting(videos={UC: []})
    feed = _feed(client, channels=(UC,))
    feed.attach()
    assert calls == [UC]                    # asked, but the client answers offline
    assert client.searches == []            # walk() below hasn't run yet
    list(feed.walk())
    assert client.searches == [UC]


def test_a_client_without_resolve_channel_is_left_untouched():
    """An older adapter or a test fake keeps working exactly as before."""
    class _Legacy:
        def __init__(self): self.searches = []
        def search_videos(self, *, channel_id, query, limit):
            self.searches.append(channel_id or query)
            return []
        def list_comments(self, video_id, page_token): return [], None

    client = _Legacy()
    feed = _feed(client, channels=("@mkbhd",))
    feed.attach()
    list(feed.walk())
    assert client.searches == ["@mkbhd"]


def test_a_failing_resolution_keeps_the_seed_rather_than_killing_it():
    class _Boom(_Client):
        def resolve_channel(self, seed):
            raise RuntimeError("network down")

    client = _Boom(videos={"@mkbhd": ["v1"]})
    feed = _feed(client, channels=("@mkbhd",))
    feed.attach()
    assert [r.reel_id for r in feed.walk()] == ["v1"]
