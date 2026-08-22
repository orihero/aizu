"""Per-platform term validation (Remedy Sheet #1 / Remedy C).

The load-bearing contract is that "we could not check" is never presentable as
"this is fine": every failure path produces an `unknown` verdict, and `unknown`
keeps the term (we have no evidence against it) while saying so.
"""
from datetime import datetime, timezone

import pytest

from aizu.discovery.validate import (DEAD, LIVE, THIN, UNKNOWN,
                                     InstagramTagProbe, TermVerdict,
                                     YouTubeTermValidator, partition,
                                     validators_for)


class _YtClient:
    def __init__(self, bodies):
        self._bodies = list(bodies)
        self.calls = []

    def _get(self, path, params):
        self.calls.append((path, params))
        item = self._bodies.pop(0) if self._bodies else {"items": []}
        if isinstance(item, Exception):
            raise item
        return item


def _yt(bodies, budget=25):
    return YouTubeTermValidator(
        _YtClient(bodies), now=lambda: datetime(2026, 8, 20, tzinfo=timezone.utc),
        budget=budget)


def _videos(n, total=0):
    return {"items": [{"id": {"videoId": f"v{i}"}} for i in range(n)],
            "pageInfo": {"totalResults": total}}


def test_recent_videos_make_a_term_live():
    v = _yt([_videos(10, total=4321)]).validate(["remont"])[0]
    assert (v.verdict, v.recent, v.volume) == (LIVE, 10, 4321)


def test_a_handful_of_videos_is_thin_not_live():
    assert _yt([_videos(1)]).validate(["remont"])[0].verdict == THIN


def test_no_recent_videos_is_dead():
    assert _yt([_videos(0)]).validate(["remont"])[0].verdict == DEAD


def test_the_recency_window_is_actually_sent():
    client = _YtClient([_videos(3)])
    YouTubeTermValidator(client,
                         now=lambda: datetime(2026, 8, 20, tzinfo=timezone.utc)
                         ).validate(["remont"])
    _path, params = client.calls[0]
    assert params["publishedAfter"] == "2026-07-21T00:00:00Z"
    assert params["part"] == "id"        # never `snippet` — this is a cheap probe


def test_a_quota_error_is_unknown_not_dead():
    v = _yt([RuntimeError("quota exceeded")]).validate(["remont"])[0]
    assert v.verdict == UNKNOWN and v.usable


def test_the_search_budget_is_enforced_and_declared():
    out = _yt([_videos(5), _videos(5)], budget=1).validate(["a", "b"])
    assert out[0].verdict == LIVE
    assert out[1].verdict == UNKNOWN and "budget" in out[1].detail


def test_blank_terms_are_skipped():
    assert _yt([]).validate(["", "  "]) == []


# ----- Instagram typeahead -----

class _Page:
    def __init__(self, replies):
        self._replies = list(replies)
        self.calls = []

    def evaluate(self, script, url):
        self.calls.append(url)
        item = self._replies.pop(0) if self._replies else "HTTP:500"
        if isinstance(item, Exception):
            raise item
        return item


class _Feed:
    def __init__(self, page):
        self._page = page


def _probe(replies):
    return InstagramTagProbe(_Feed(_Page(replies)), min_interval=0,
                             sleep=lambda _s: None, clock=lambda: 0.0)


def _body(*tags):
    return ('{"hashtags": ['
            + ",".join('{"hashtag": {"name": "%s", "media_count": %d}}' % t
                       for t in tags)
            + "]}")


def test_an_exact_match_reports_the_post_count_the_ui_hides():
    v = _probe([_body(("remont", 50000))]).validate(["remont"])[0]
    assert (v.verdict, v.volume) == (LIVE, 50000)


def test_a_tiny_tag_is_thin():
    assert _probe([_body(("remont", 4))]).validate(["remont"])[0].verdict == THIN


def test_a_missing_exact_match_is_dead_but_returns_near_matches():
    v = _probe([_body(("remontuz", 900), ("remont2024", 40))]).validate(["remont"])[0]
    assert v.verdict == DEAD
    assert v.alternatives == ["remontuz", "remont2024"]


def test_a_403_flags_our_session_and_stops_the_sweep():
    probe = _probe(["HTTP:403", _body(("b", 100))])
    out = probe.validate(["a", "b"])
    assert probe.session_unhealthy is True
    assert [v.verdict for v in out] == [UNKNOWN, UNKNOWN]
    assert "sweep stopped" in out[1].detail


def test_the_request_carries_the_web_app_id_header():
    probe = _probe([_body(("remont", 10))])
    probe.validate(["remont"])
    assert "%23remont" in probe._feed._page.calls[0]


def test_probing_is_paced():
    waits = []
    probe = InstagramTagProbe(_Feed(_Page([_body(("a", 5)), _body(("b", 5))])),
                              min_interval=10.0, sleep=waits.append,
                              clock=lambda: 0.0)
    probe.validate(["a", "b"])
    assert waits == [10.0]        # no wait before the first call, one before the second


def test_a_feed_with_no_page_is_unknown_not_a_crash():
    class _Bare:
        _page = None
    out = InstagramTagProbe(_Bare(), min_interval=0, sleep=lambda _s: None).validate(["a"])
    assert out[0].verdict == UNKNOWN


# ----- shared -----

def test_unknown_terms_are_kept_and_dead_ones_dropped():
    keep, drop = partition([TermVerdict("a", "youtube", LIVE),
                            TermVerdict("b", "youtube", UNKNOWN),
                            TermVerdict("c", "youtube", THIN),
                            TermVerdict("d", "youtube", DEAD)])
    assert keep == ["a", "b", "c"] and drop == ["d"]


@pytest.mark.parametrize("platform", ["x", "linkedin", "reddit", "telegram"])
def test_platforms_the_research_says_not_to_probe_have_no_validator(platform):
    assert validators_for(platform, client=object(), feed=object()) is None


def test_validators_resolve_where_they_exist():
    assert isinstance(validators_for("youtube", client=object()), YouTubeTermValidator)
    assert isinstance(validators_for("instagram", feed=object()), InstagramTagProbe)
