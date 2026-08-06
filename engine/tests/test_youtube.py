"""YouTubeFeed mapping (PRD: docs/prd/youtube-lead-agent-PRD.md).

The Data API HTTP is isolated behind YouTubeApiPort, so the video→Reel /
comment→Comment mapping and comment pagination are tested with a fake port —
no API key, no network.
"""
from aizu.engines.youtube.feed import YouTubeFeed, YtComment, YtVideo


class FakeYouTubeApi:
    def __init__(self, by_channel=None, by_query=None, comments=None):
        self._by_channel = by_channel or {}
        self._by_query = by_query or {}
        self._comments = comments or {}  # {video_id: [(page, [YtComment], next_token)]}

    def search_videos(self, *, channel_id, query, limit):
        if channel_id is not None:
            return list(self._by_channel.get(channel_id, []))[:limit]
        return list(self._by_query.get(query, []))[:limit]

    def list_comments(self, video_id, page_token):
        pages = self._comments.get(video_id, [])
        for token, items, nxt in pages:
            if token == page_token:
                return items, nxt
        return [], None


def test_walk_maps_videos_to_reels_with_title_and_channel():
    api = FakeYouTubeApi(by_channel={"UC_dev": [
        YtVideo("v1", title="Acme app demo", description="free trial",
                channel_title="Acme Inc.")]})
    feed = YouTubeFeed(client=api, channels=["UC_dev"])
    reels = list(feed.walk())
    assert [r.reel_id for r in reels] == ["v1"]
    assert "Acme app demo" in reels[0].caption and "free trial" in reels[0].caption
    assert reels[0].author == "Acme Inc."


def test_walk_dedupes_video_across_channel_and_query():
    v = YtVideo("v1", title="t", channel_title="c")
    api = FakeYouTubeApi(by_channel={"UC_dev": [v]}, by_query={"demo": [v]})
    feed = YouTubeFeed(client=api, channels=["UC_dev"], queries=["demo"])
    reels = list(feed.walk())
    assert [r.reel_id for r in reels] == ["v1"]   # surfaced once, not twice


def test_fetch_comments_maps_and_paginates():
    api = FakeYouTubeApi(comments={"v1": [
        (None, [YtComment("cm1", "how much is the Pro plan?", "aziz")], "PAGE2"),
        ("PAGE2", [YtComment("cm2", "+1 415 555 0142", "aziz")], None),
    ]})
    feed = YouTubeFeed(client=api, channels=["UC_dev"])
    first, cursor = feed.fetch_comments("v1", None)
    assert [c.comment_id for c in first] == ["v1/cm1"]
    assert first[0].username == "aziz" and cursor == "PAGE2"
    second, cursor2 = feed.fetch_comments("v1", "PAGE2")
    assert [c.comment_id for c in second] == ["v1/cm2"] and cursor2 is None


def test_no_frames_and_healthy():
    feed = YouTubeFeed(client=FakeYouTubeApi(), channels=["UC_dev"])
    from aizu.core.feed import Reel
    assert feed.capture_frames(Reel(reel_id="v1")) == []
    assert feed.healthy() is True
