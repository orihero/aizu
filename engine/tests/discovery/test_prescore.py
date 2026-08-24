"""Seed-account prescoring (Campaign Lab, Remedy Sheet #2 / Remedy C).

One request per candidate, and the load-bearing rule throughout: a signal the
platform did not report is UNKNOWN, never zero. A channel that hides its
subscriber count must not read as a channel with no subscribers, and a probe that
could not run must not read as a pass.
"""
import json
import time

import pytest

from aizu.discovery.prescore import (ALIVE, DEAD, STALE, UNKNOWN, AccountProfile,
                                     InstagramProfileProbe, TelegramPreviewProbe,
                                     YouTubeChannelProbe, liveness_gate, probe_for)

DAY = 86400.0


def _now(offset_days=0.0):
    return time.time() - offset_days * DAY


# ---------------- derived metrics ----------------

def test_unreported_signals_stay_none_rather_than_zero():
    p = AccountProfile(seed="x", platform="youtube", checked=True)
    assert p.followers is None
    assert p.engagement_rate is None
    assert p.comment_like_ratio is None
    assert p.follower_ratio is None
    assert p.last_post_age_days is None


def test_comment_like_ratio_is_the_metric_this_engine_cares_about():
    """Leads live in comment sections: 50 likes / 30 comments beats 500 / 3."""
    rich = AccountProfile(seed="a", platform="instagram", followers=1000,
                          recent_likes=[50], recent_comments=[30])
    thin = AccountProfile(seed="b", platform="instagram", followers=1000,
                          recent_likes=[500], recent_comments=[3])
    assert rich.comment_like_ratio > thin.comment_like_ratio


def test_posts_in_window_counts_only_that_window():
    p = AccountProfile(seed="x", platform="instagram",
                       recent_post_ats=[_now(1), _now(10), _now(40)])
    assert p.posts_in(30) == 2


# ---------------- liveness gate ----------------

def test_an_unchecked_profile_is_unknown_and_still_usable():
    v = liveness_gate(AccountProfile(seed="x", platform="instagram",
                                     detail="network down"))
    assert v.verdict == UNKNOWN and v.usable


def test_a_missing_account_is_dead():
    v = liveness_gate(AccountProfile(seed="x", platform="youtube",
                                     checked=True, exists=False))
    assert v.verdict == DEAD and not v.usable


def test_a_healthy_account_passes():
    p = AccountProfile(seed="x", platform="instagram", checked=True,
                       followers=10_000, following=500,
                       recent_post_ats=[_now(1), _now(4), _now(9), _now(20)],
                       recent_likes=[300, 250, 400], recent_comments=[20, 15, 30])
    assert liveness_gate(p).verdict == ALIVE


def test_a_dormant_account_is_stale_with_a_reason():
    p = AccountProfile(seed="x", platform="instagram", checked=True,
                       followers=10_000, following=500,
                       recent_post_ats=[_now(60)])
    v = liveness_gate(p)
    assert v.verdict == STALE
    assert any("last post" in r for r in v.reasons)


def test_a_thin_comment_section_is_called_out():
    p = AccountProfile(seed="x", platform="instagram", checked=True,
                       followers=10_000, following=100,
                       recent_post_ats=[_now(1), _now(3), _now(5)],
                       recent_likes=[1000], recent_comments=[2])
    v = liveness_gate(p)
    assert v.verdict == STALE
    assert any("thin comment section" in r for r in v.reasons)


def test_a_private_account_can_never_be_a_harvest_seed():
    p = AccountProfile(seed="x", platform="instagram", checked=True, private=True,
                       followers=10_000, following=100,
                       recent_post_ats=[_now(1), _now(2), _now(3)])
    assert "private" in " ".join(liveness_gate(p).reasons)


def test_unreported_inputs_never_fail_the_gate():
    """A Telegram channel publishes no following count; it must not fail the
    follower-ratio test that Telegram does not have an input for."""
    p = AccountProfile(seed="x", platform="telegram", checked=True,
                       followers=5000,
                       recent_post_ats=[_now(1), _now(2), _now(3)])
    assert liveness_gate(p).verdict == ALIVE


# ---------------- Instagram ----------------

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


def _ig(replies, **kw):
    kw.setdefault("min_interval", 0)
    return InstagramProfileProbe(_Feed(_Page(replies)), sleep=lambda _s: None,
                                 clock=lambda: 0.0, **kw)


def _ig_body(**over):
    node = {"id": "1785551234", "is_private": False, "category_name": "Shopping",
            "edge_followed_by": {"count": 12000}, "edge_follow": {"count": 300},
            "edge_owner_to_timeline_media": {
                "count": 412,
                "edges": [{"node": {"taken_at_timestamp": int(_now(2)),
                                    "edge_liked_by": {"count": 200},
                                    "edge_media_to_comment": {"count": 25}}}]}}
    node.update(over)
    return json.dumps({"data": {"user": node}})


def test_instagram_reads_the_stable_id_and_the_counts():
    p = _ig([_ig_body()]).probe("acme")
    assert p.checked and p.exists
    assert p.stable_id == "1785551234"
    assert (p.followers, p.following, p.posts) == (12000, 300, 412)
    assert p.category == "Shopping"
    assert p.recent_likes == [200] and p.recent_comments == [25]


def test_instagram_404_is_a_dead_account_not_a_failed_probe():
    p = _ig(["HTTP:404"]).probe("ghost")
    assert p.checked and not p.exists
    assert liveness_gate(p).verdict == DEAD


@pytest.mark.parametrize("code", ["401", "403", "429"])
def test_instagram_auth_codes_flag_our_session_and_stop_the_sweep(code):
    probe = _ig([f"HTTP:{code}", _ig_body()])
    first = probe.probe("a")
    assert probe.session_unhealthy is True
    assert liveness_gate(first).verdict == UNKNOWN
    second = probe.probe("b")
    assert "sweep stopped" in second.detail
    assert probe._feed._page.calls == ["https://i.instagram.com/api/v1/users/"
                                       "web_profile_info/?username=a"]


def test_instagram_probing_is_paced():
    waits = []
    probe = InstagramProfileProbe(_Feed(_Page([_ig_body(), _ig_body()])),
                                  min_interval=8.0, sleep=waits.append,
                                  clock=lambda: 0.0)
    probe.probe_many(["a", "b"])
    assert waits == [8.0]      # no wait before the first, one before the second


def test_instagram_a_drifted_body_degrades_a_field_not_the_probe():
    p = _ig([json.dumps({"data": {"user": {"id": "9"}}})]).probe("acme")
    assert p.checked and p.exists and p.stable_id == "9"
    assert p.followers is None and p.recent_post_ats == []


def test_instagram_an_empty_body_reads_as_deleted():
    p = _ig([json.dumps({"data": {}})]).probe("gone")
    assert p.checked and not p.exists


# ---------------- YouTube ----------------

class _YtClient:
    def __init__(self, channels=None, uploads=None, raises=None):
        self._channels = channels or {}
        self._uploads = uploads or []
        self._raises = raises
        self.calls = []

    def _get(self, path, params):
        self.calls.append((path, params))
        if self._raises:
            raise self._raises
        if path == "channels":
            ids = params["id"].split(",")
            return {"items": [self._channels[i] for i in ids if i in self._channels]}
        return {"items": [{"snippet": {"publishedAt": d}} for d in self._uploads]}


def _yt_channel(cid, subs="9000", videos="120", uploads=None, hidden=False):
    item = {"id": cid,
            "statistics": {"subscriberCount": subs, "videoCount": videos},
            "brandingSettings": {"channel": {"title": f"{cid} title"}}}
    if hidden:
        item["statistics"]["hiddenSubscriberCount"] = True
    if uploads:
        item["contentDetails"] = {"relatedPlaylists": {"uploads": uploads}}
    return item


def test_youtube_scores_a_whole_batch_in_one_channels_call():
    client = _YtClient(channels={f"UC{i}": _yt_channel(f"UC{i}") for i in range(5)})
    out = YouTubeChannelProbe(client).probe_many([f"UC{i}" for i in range(5)])
    assert len(out) == 5 and all(p.checked and p.exists for p in out)
    assert [c[0] for c in client.calls] == ["channels"]      # exactly one call
    assert client.calls[0][1]["id"] == "UC0,UC1,UC2,UC3,UC4"


def test_youtube_never_uses_search_list():
    """search.list is 100 units AND one of only 100 searches a day since 6/2026."""
    client = _YtClient(channels={"UC1": _yt_channel("UC1")})
    YouTubeChannelProbe(client).probe("UC1")
    assert all(path != "search" for path, _ in client.calls)


def test_youtube_an_id_absent_from_items_does_not_exist():
    """Previously indistinguishable from a channel that is merely quiet."""
    client = _YtClient(channels={})
    (p,) = YouTubeChannelProbe(client).probe_many(["UCghost"])
    assert p.checked and not p.exists
    assert liveness_gate(p).verdict == DEAD


def test_youtube_hidden_subscriber_count_is_unknown_not_zero():
    client = _YtClient(channels={"UC1": _yt_channel("UC1", subs="0", hidden=True)})
    (p,) = YouTubeChannelProbe(client).probe_many(["UC1"])
    assert p.followers is None


def test_youtube_reads_upload_cadence_from_the_uploads_playlist():
    client = _YtClient(channels={"UC1": _yt_channel("UC1", uploads="UU1")},
                       uploads=["2026-08-19T10:00:00Z", "2026-08-12T10:00:00Z"])
    (p,) = YouTubeChannelProbe(client).probe_many(["UC1"])
    assert len(p.recent_post_ats) == 2
    assert client.calls[1][0] == "playlistItems"
    assert client.calls[1][1]["playlistId"] == "UU1"


def test_youtube_a_quota_error_is_unknown_for_every_id_in_the_batch():
    client = _YtClient(raises=RuntimeError("quota exceeded"))
    out = YouTubeChannelProbe(client).probe_many(["UC1", "UC2"])
    assert all(liveness_gate(p).verdict == UNKNOWN for p in out)


def test_youtube_batches_beyond_fifty_ids():
    ids = [f"UC{i}" for i in range(120)]
    client = _YtClient(channels={i: _yt_channel(i) for i in ids})
    out = YouTubeChannelProbe(client).probe_many(ids)
    assert len(out) == 120
    assert [c[0] for c in client.calls] == ["channels"] * 3      # 50 + 50 + 20


# ---------------- Telegram ----------------

def _tg(html):
    return TelegramPreviewProbe(opener=lambda _u: html.encode(),
                                sleep=lambda _s: None, clock=lambda: 0.0)


_TG_HTML = '''
<div class="tgme_channel_info_counter"><span class="tgme_page_extra">12 345 subscribers</span></div>
<div class="tgme_widget_message"><time datetime="2026-08-19T10:00:00+00:00"></time>
  <span class="tgme_widget_message_views">4.2K</span>
  <a class="tgme_widget_message_footer_comments">Comments</a></div>
'''


def test_telegram_reads_subscribers_recency_and_the_comments_bubble():
    p = _tg(_TG_HTML).probe("@growthlab")
    assert p.checked and p.exists
    assert p.followers == 12345                 # thin-space separators handled
    assert len(p.recent_post_ats) == 1
    # A linked discussion group means the channel HAS reachable commenters.
    assert p.has_discussion is True


def test_telegram_without_a_comments_control_reports_no_discussion_group():
    html = _TG_HTML.replace(
        '<a class="tgme_widget_message_footer_comments">Comments</a>', "")
    assert _tg(html).probe("@broadcast").has_discussion is False


def test_telegram_an_empty_preview_reads_as_nonexistent():
    p = _tg("<html><body>nothing here</body></html>").probe("@ghost")
    assert p.checked and not p.exists
    assert liveness_gate(p).verdict == DEAD


def test_telegram_the_handle_is_the_stable_id():
    """Documented exception: t.me exposes no numeric id, so a rename really is a
    new seed."""
    p = _tg(_TG_HTML).probe("@growthlab")
    assert p.stable_id == "growthlab" == p.handle


@pytest.mark.parametrize("seed", ["@x", "x", "t.me/x", "https://t.me/s/x"])
def test_telegram_accepts_the_forms_operators_paste(seed):
    urls = []

    def opener(u):
        urls.append(u)
        return _TG_HTML.encode()

    TelegramPreviewProbe(opener=opener, sleep=lambda _s: None,
                         clock=lambda: 0.0).probe(seed)
    assert urls == ["https://t.me/s/x"]


def test_telegram_an_unreachable_preview_is_unknown_not_dead():
    def boom(_u):
        raise OSError("dns is down")

    p = TelegramPreviewProbe(opener=boom, sleep=lambda _s: None,
                             clock=lambda: 0.0).probe("@x")
    assert liveness_gate(p).verdict == UNKNOWN


# ---------------- registry ----------------

@pytest.mark.parametrize("platform", ["x", "linkedin", "reddit"])
def test_platforms_the_research_says_not_to_probe_have_none(platform):
    assert probe_for(platform, feed=object(), client=object()) is None


def test_probes_resolve_where_they_exist():
    assert isinstance(probe_for("instagram", feed=object()), InstagramProfileProbe)
    assert isinstance(probe_for("youtube", client=object()), YouTubeChannelProbe)
    assert isinstance(probe_for("telegram"), TelegramPreviewProbe)
