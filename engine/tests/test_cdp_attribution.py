"""Attribution tests for CDPFeed._on_response (no browser needed).

These lock the fix for the lead-misattribution bug: comments must be keyed to
the reel identified BY THE RESPONSE ITSELF (request media pk → shortcode, or
the permalink URL of the page that received it) — never to a guessed "active"
reel, and never read out of media-bearing payloads (whose caption / preview
comment nodes are comment-shaped but belong to other reels).
"""
from aizu.engines.instagram.cdp import CDPFeed


class FakeRequest:
    def __init__(self, post_data=None):
        self.post_data = post_data


class FakePage:
    def __init__(self, url):
        self.url = url


class FakeFrame:
    def __init__(self, page_url):
        self.page = FakePage(page_url)


class FakeResponse:
    def __init__(self, url, body, page_url="https://www.instagram.com/reels/",
                 post_data=None):
        self.url = url
        self._body = body
        self.request = FakeRequest(post_data)
        self.frame = FakeFrame(page_url)

    def json(self):
        return self._body


def _comment_body(pk, username, text):
    return {"comments": [
        {"pk": pk, "text": text, "user": {"username": username, "pk": "1"}},
    ]}


def _media_body(pk, code, username):
    return {"items": [{
        "pk": pk, "code": code, "media_type": 2,
        "user": {"username": username, "pk": "9"},
        "caption": {"pk": pk + "9", "text": "caption text",
                    "user": {"username": username, "pk": "9"}},
        "preview_comments": [
            {"pk": pk + "7", "text": "preview from another reel",
             "user": {"username": "stranger", "pk": "8"}},
        ],
    }]}


def test_comment_attributed_via_rest_media_pk():
    feed = CDPFeed()
    feed._on_response(FakeResponse(
        "https://www.instagram.com/api/v1/clips/home/",
        _media_body("111", "Cabc123", "author_a")))
    feed._on_response(FakeResponse(
        "https://www.instagram.com/api/v1/media/111/comments/?can_support=...",
        _comment_body("9001", "lead_user", "narxi qancha?")))
    assert [c.username for c in feed._comments_by_reel["Cabc123"]] == ["lead_user"]


def test_comment_attributed_via_graphql_media_id_variable():
    feed = CDPFeed()
    feed._on_response(FakeResponse(
        "https://www.instagram.com/graphql/query",
        _media_body("222", "Cdef456", "author_b")))
    feed._on_response(FakeResponse(
        "https://www.instagram.com/graphql/query",
        _comment_body("9002", "lead_two", "adres?"),
        post_data="variables=%7B%22media_id%22%3A%22222%22%7D"))
    assert [c.username for c in feed._comments_by_reel["Cdef456"]] == ["lead_two"]


def test_comment_attributed_via_permalink_page_url():
    feed = CDPFeed()
    feed._on_response(FakeResponse(
        "https://www.instagram.com/graphql/query",
        _comment_body("9003", "lead_three", "qancha?"),
        page_url="https://www.instagram.com/reel/Cghi789/"))
    assert [c.username for c in feed._comments_by_reel["Cghi789"]] == ["lead_three"]


def test_unknown_pk_falls_back_to_page_url():
    # Comment response arrives before any feed batch populated pk→code:
    # the permalink of the page that received it still attributes correctly.
    feed = CDPFeed()
    feed._on_response(FakeResponse(
        "https://www.instagram.com/api/v1/media/555/comments/",
        _comment_body("9007", "early_bird", "salom"),
        page_url="https://www.instagram.com/reel/Cpqr678/"))
    assert [c.username for c in feed._comments_by_reel["Cpqr678"]] == ["early_bird"]


def test_empty_comment_section_still_satisfies_canary():
    # A reel with zero comments is NOT an interception failure: the canary is
    # about hinted JSON traffic flowing, not about leads found.
    feed = CDPFeed()
    feed._saw_data = False
    feed._on_response(FakeResponse(
        "https://www.instagram.com/graphql/query",
        {"data": {"xdt_api__v1__media__media_id__comments__connection": {
            "edges": [], "page_info": {"end_cursor": None, "has_next_page": False}}}},
        page_url="https://www.instagram.com/reel/Cstu901/"))
    assert feed.healthy()
    assert feed._comments_by_reel == {}


def test_unattributable_comments_are_dropped_not_guessed():
    feed = CDPFeed()
    feed._on_response(FakeResponse(
        "https://www.instagram.com/graphql/query",
        _comment_body("9004", "whoever", "text"),
        page_url="https://www.instagram.com/explore/tags/uy/"))
    assert feed._comments_by_reel == {}


def test_media_bearing_payload_yields_reels_but_never_comments():
    feed = CDPFeed()
    feed._on_response(FakeResponse(
        "https://www.instagram.com/api/v1/clips/home/",
        _media_body("333", "Cjkl012", "author_c"),
        page_url="https://www.instagram.com/reel/Cjkl012/"))
    assert [r.reel_id for r in feed._reel_queue] == ["Cjkl012"]
    assert [r.author for r in feed._reel_queue] == ["author_c"]
    # caption + preview comments in the same payload must NOT become leads
    assert feed._comments_by_reel == {}


def test_fetch_watermark_returns_only_new_comments():
    feed = CDPFeed()
    page_url = "https://www.instagram.com/reel/Cmno345/"
    feed._on_response(FakeResponse(
        "https://www.instagram.com/api/v1/media/444/comments/",
        _comment_body("9005", "first", "a"), page_url=page_url))
    feed._on_response(FakeResponse(
        "https://www.instagram.com/api/v1/media/444/comments/",
        _comment_body("9006", "second", "b"), page_url=page_url))
    bucket = feed._comments_by_reel["Cmno345"]
    assert [c.username for c in bucket] == ["first", "second"]
    new = bucket[1:]  # the count-watermark slice fetch_comments() uses
    assert [c.username for c in new] == ["second"]
