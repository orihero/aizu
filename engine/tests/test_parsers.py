"""Fixture tests for the interception parsers.

The fixtures mimic the *shapes* of Instagram's internal JSON (clips feed +
comments), including wrapper nesting and a GraphQL-style caption, so we verify
shape-driven detection without depending on exact endpoint IDs.
"""
import json
from pathlib import Path

from reelradar.core.parsers import (looks_like_comment_response,
                               looks_like_reel_response, media_pk_codes,
                               parse_comments, parse_reels)

FIXTURES = Path(__file__).parent / "fixtures"

# A clips/feed batch: items wrapped under feed_items -> media_or_ad -> media.
CLIPS_FEED = {
    "feed_items": [
        {"media_or_ad": {
            "pk": "3001",
            "code": "Cabc123",
            "media_type": 2,
            "user": {"username": "acme.io", "pk": "55"},
            "caption": {"text": "Acme app demo, free trial available now"},
            "clips_metadata": {"music_info": None},
        }},
        {"media_or_ad": {
            "pk": "3002",
            "code": "Cdef456",
            "media_type": 2,
            "user": {"username": "acme.product"},
            # GraphQL-style caption shape
            "edge_media_to_caption": {"edges": [{"node": {"text": "New dashboard feature"}}]},
            "video_versions": [{"url": "x"}],
        }},
    ],
    "num_results": 2,
    "more_available": True,
}

# A comments response with pagination cursor and a nested reply.
COMMENTS = {
    "comments": [
        {"pk": "9001", "text": "How much is the Pro plan? +1 415 555 0142",
         "user": {"username": "aziz", "pk": "1"}, "created_at": 1718000000},
        {"pk": "9002", "text": "🔥🔥", "user": {"username": "bot", "pk": "2"}},
        {"pk": "9003", "text": "reply here", "user": {"username": "x"},
         "parent_comment_id": "9001"},
    ],
    "next_max_id": "QVFC_cursor_token",
    "has_more_comments": True,
}


def test_reel_detection_and_extraction():
    assert looks_like_reel_response("/api/v1/clips/home/connect_v2/", CLIPS_FEED)
    reels = parse_reels(CLIPS_FEED)
    ids = {r.reel_id for r in reels}
    assert ids == {"Cabc123", "Cdef456"}            # code preferred over pk
    by_id = {r.reel_id: r for r in reels}
    assert by_id["Cabc123"].author == "acme.io"
    assert "demo" in by_id["Cabc123"].caption
    assert by_id["Cdef456"].caption == "New dashboard feature"  # graphql caption shape


def test_comment_detection_and_extraction():
    assert looks_like_comment_response("/api/v1/media/3001/comments/", COMMENTS)
    comments, cursor = parse_comments(COMMENTS)
    assert cursor == "QVFC_cursor_token"
    ids = [c.comment_id for c in comments]
    assert ids == ["9001", "9002", "9003"]
    assert comments[0].username == "aziz"
    assert comments[2].is_reply is True


def test_media_not_misread_as_comment():
    # The clips feed has caption.text but must NOT be parsed as comments.
    comments, _ = parse_comments(CLIPS_FEED)
    assert comments == []
    assert not looks_like_comment_response("/clips/", CLIPS_FEED)


def test_comments_not_misread_as_media():
    assert parse_reels(COMMENTS) == []
    assert not looks_like_reel_response("/comments/", COMMENTS)


def test_reels_deduped():
    doubled = {"feed_items": CLIPS_FEED["feed_items"] + CLIPS_FEED["feed_items"]}
    assert len(parse_reels(doubled)) == 2


def test_graphql_comments_connection_live_shape():
    """The real reels comment thread: xdt_api...comments__connection with
    edges[].node (XDTCommentDict) + page_info.end_cursor. Captured from live
    Instagram, sanitized. Locks the shape the engine depends on for leads."""
    body = json.loads((FIXTURES / "comments_connection.json").read_text(encoding="utf-8"))
    assert looks_like_comment_response("https://www.instagram.com/graphql/query", body)
    comments, cursor = parse_comments(body)
    ids = [c.comment_id for c in comments]
    assert ids == ["17900000000000001", "17900000000000002", "17900000000000003"]
    assert comments[0].username == "aziz_dev"
    assert "Pro plan" in comments[0].text              # lead text survives
    assert comments[2].is_reply is True                # parent_comment_id set
    assert cursor == "CURSOR_PAGE_2_TOKEN"             # nested page_info.end_cursor
    # and it must not be misread as a reel feed
    assert not looks_like_reel_response("/graphql/query", body)


def test_graphql_cursor_stops_on_last_page():
    body = json.loads((FIXTURES / "comments_connection.json").read_text(encoding="utf-8"))
    conn = body["data"]["xdt_api__v1__media__media_id__comments__connection"]
    conn["page_info"]["has_next_page"] = False
    _, cursor = parse_comments(body)
    assert cursor is None                              # no forward cursor when exhausted


# A permalink/feed media node the way live Instagram actually sends it: the
# caption is a full comment-SHAPED node (pk + text + user) and preview_comments
# carry real comments belonging to THIS media — but in a feed batch they belong
# to many different media, so comment parsing must never read either.
REALISTIC_MEDIA = {
    "items": [{
        "pk": "3621447163753077123",
        "id": "3621447163753077123_55",
        "code": "DXcBq4RiC7y",
        "media_type": 2,
        "user": {"username": "acme_team_1", "pk": "55"},
        "caption": {
            "pk": "18100000000000001",
            "text": "🚀 Acme — the productivity app your team wanted",
            "user": {"username": "acme_team_1", "pk": "55"},
            "created_at": 1718000000,
        },
        "preview_comments": [
            {"pk": "18200000000000001", "text": "How much is the Pro plan?",
             "user": {"username": "some_other_user", "pk": "77"}},
        ],
        "video_versions": [{"url": "x"}],
    }],
}


def test_caption_and_preview_comments_never_parsed_as_comments():
    comments, _ = parse_comments(REALISTIC_MEDIA)
    assert comments == []
    assert not looks_like_comment_response("/graphql", REALISTIC_MEDIA)
    # ...while reel extraction still pairs the code with ITS author.
    reels = parse_reels(REALISTIC_MEDIA)
    assert [(r.reel_id, r.author) for r in reels] == [("DXcBq4RiC7y", "acme_team_1")]


def test_media_pk_codes_maps_pk_and_compound_id_to_shortcode():
    mapping = media_pk_codes(REALISTIC_MEDIA)
    assert mapping["3621447163753077123"] == "DXcBq4RiC7y"
    assert mapping["3621447163753077123_55"] == "DXcBq4RiC7y"


def test_media_without_shortcode_is_skipped():
    # pk-only media cannot be opened via a permalink — must not become a reel.
    body = {"items": [{"pk": "999", "media_type": 2,
                       "user": {"username": "someone"},
                       "caption": {"text": "hi"}}]}
    assert parse_reels(body) == []


def test_garbage_bodies_are_safe():
    for junk in [None, 42, "string", [], {}, {"random": "noise"}]:
        assert parse_reels(junk) == []
        assert parse_comments(junk) == ([], None)
        assert not looks_like_reel_response("/x/", junk)
        assert not looks_like_comment_response("/x/", junk)
