"""Reddit feed mapping — the genuinely new piece: flatten a deeply-NESTED comment
tree (with `more`/`morechildren` continuation) into flat Comment objects, and the
re-poll watermark cursor.

Two layers:
  - RedditDataApiClient (live httpx adapter): parses Reddit's JSON Listings into
    RedditSubmission / RedditComment, recursing the inline reply tree and expanding
    a bounded number of `more` branches.
  - RedditFeed: maps RedditComment → core Comment (composite id, is_reply=depth>0)
    and applies the created_utc watermark so re-polls only score fresh comments.
"""
from aizu.core.feed import Comment as CoreComment
from aizu.engines.reddit import feed as rfeed
from aizu.engines.reddit.feed import (RedditComment, RedditDataApiClient,
                                           RedditFeed, RedditSubmission)


# ---------------- RedditFeed: watermark + is_reply mapping (Port fake) ----------------

class _PortFake:
    def __init__(self, subs, tree):
        self._subs = subs
        self._tree = tree

    def list_submissions(self, *, subreddit, query, limit):
        return [] if query is not None else list(self._subs)

    def comment_tree(self, submission_id, subreddit):
        return list(self._tree)


def _walked_feed():
    feed = RedditFeed(
        client=_PortFake(
            subs=[RedditSubmission("s1", title="T", selftext="B", author="u")],
            tree=[RedditComment("c1", "top", "a", depth=0, created_utc=100.0),
                  RedditComment("c2", "nested", "b", depth=2, created_utc=200.0)]),
        subreddits=["realestate"])
    list(feed.walk())   # populate the submission→subreddit map fetch_comments needs
    return feed


def test_fetch_comments_maps_depth_to_is_reply_and_composite_id():
    feed = _walked_feed()
    comments, cursor = feed.fetch_comments("s1", None)
    assert [c.comment_id for c in comments] == ["s1/c1", "s1/c2"]   # reel_id-prefixed
    by_id = {c.comment_id: c for c in comments}
    assert by_id["s1/c1"].is_reply is False        # depth 0 = top-level
    assert by_id["s1/c2"].is_reply is True         # depth>0 = nested reply
    assert cursor == "200.0"                        # newest created_utc watermark


def test_fetch_comments_watermark_skips_already_scored():
    feed = _walked_feed()
    comments, cursor = feed.fetch_comments("s1", "150")   # only c2 (created 200>150)
    assert [c.comment_id for c in comments] == ["s1/c2"]
    assert cursor == "200.0"


def test_walk_yields_submission_as_reel_with_title_plus_selftext():
    feed = _walked_feed()
    reels = list(feed.walk())
    assert [r.reel_id for r in reels] == ["s1"]
    assert reels[0].caption == "T\nB"               # title + selftext is the signal
    assert reels[0].author == "u"


# ---------------- RedditDataApiClient: JSON Listing parsing + nested flatten ----------------

class _Resp:
    def __init__(self, status, body, headers=None):
        self.status_code = status
        self._body = body
        self.headers = headers or {}

    def json(self):
        return self._body

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


class _Http:
    def __init__(self, get_body=None, post_bodies=None):
        self._get_body = get_body
        self._post = list(post_bodies or [])
        self.post_calls = 0

    def post(self, url, data=None, auth=None, headers=None, timeout=None):
        self.post_calls += 1
        if url == rfeed._TOKEN_URL:
            return _Resp(200, {"access_token": "tok", "expires_in": 3600})
        return _Resp(200, self._post.pop(0))

    def get(self, url, params=None, headers=None, timeout=None):
        return _Resp(200, self._get_body)


def _client(get_body, post_bodies=None):
    c = RedditDataApiClient(client_id="cid", client_secret="sec", user_agent="ua")
    c._httpx = _Http(get_body=get_body, post_bodies=post_bodies)
    return c


def test_list_submissions_maps_t3_children():
    body = {"data": {"children": [
        {"kind": "t3", "data": {"id": "s1", "title": "Tur", "selftext": "Body", "author": "u"}},
        {"kind": "t3", "data": {"id": "s2", "title": "Two", "selftext": "", "author": "v"}},
        {"kind": "more", "data": {"children": ["x"]}},   # not a submission → ignored
    ]}}
    subs = _client(body).list_submissions(subreddit="realestate", query=None, limit=25)
    assert [(s.submission_id, s.title, s.selftext, s.author) for s in subs] == [
        ("s1", "Tur", "Body", "u"), ("s2", "Two", "", "v")]


def test_comment_tree_flattens_nested_replies_and_expands_more():
    # [t3 listing, t1 listing]; the t1 listing has a top comment with a nested reply
    # plus a `more` placeholder, and a top-level `more`.
    t1_listing = {"data": {"children": [
        {"kind": "t1", "data": {
            "id": "c1", "body": "top", "author": "a", "depth": 0, "created_utc": 100.0,
            "replies": {"data": {"children": [
                {"kind": "t1", "data": {"id": "c2", "body": "reply", "author": "b",
                                        "depth": 1, "created_utc": 101.0, "replies": ""}},
                {"kind": "more", "data": {"children": ["c9", "c10"]}},
            ]}}}},
        {"kind": "more", "data": {"children": ["c20"]}},
    ]}}
    body = [{"data": {"children": []}}, t1_listing]
    # morechildren returns one more t1 (a still-deeper reply).
    more_body = {"json": {"data": {"things": [
        {"kind": "t1", "data": {"id": "c9", "body": "deep", "author": "z",
                                "depth": 2, "created_utc": 102.0}}]}}}
    out = _client(body, post_bodies=[more_body]).comment_tree("s1", "realestate")
    by_id = {c.comment_id: c for c in out}
    assert set(by_id) == {"c1", "c2", "c9"}        # inline tree + expanded `more`
    assert by_id["c1"].depth == 0
    assert by_id["c2"].depth == 1                  # nested reply kept its depth
    assert by_id["c9"].depth == 2                  # came from morechildren expansion


def test_comment_tree_tolerates_unexpected_shape():
    # A malformed body (not the [t3, t1] pair) must not crash — return nothing.
    assert _client({"unexpected": True}).comment_tree("s1", "realestate") == []
