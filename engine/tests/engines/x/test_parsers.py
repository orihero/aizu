"""Pure X GraphQL parsers — shape detection, the two surfaces, composite cursor.

Locks the by-shape contract (a rotated doc_id still parses), that the embedded
quoted parent is never miscounted, that replies vs quote-posts get the right
``is_reply`` flag, and that the composite cursor packs/unpacks losslessly.
"""
from aizu.engines.x.parsers import (looks_like_timeline_response, pack_cursor,
                                          parse_posts, parse_quotes, parse_replies,
                                          unpack_cursor)


def _tweet(rest_id, text, handle, **extra):
    return {"rest_id": rest_id,
            "core": {"user_results": {"result": {"legacy": {"screen_name": handle}}}},
            "legacy": {"full_text": text, "conversation_id_str": rest_id}, **extra}


def _timeline(*tweets):
    # A plausible timeline envelope: tweets nested under entries/itemContent.
    return {"data": {"home": {"timeline": {"instructions": [{"entries": [
        {"content": {"itemContent": {"tweet_results": {"result": t}}}} for t in tweets
    ]}]}}}}


def test_parses_parent_posts():
    body = _timeline(_tweet("1500", "We are hiring a fractional CFO", "acme"),
                     _tweet("1600", "gm", "someone"))
    posts = parse_posts(body)
    assert [(p.reel_id, p.author) for p in posts] == [("1500", "acme"), ("1600", "someone")]
    assert posts[0].caption.startswith("We are hiring")


def test_embedded_quoted_parent_is_not_double_counted():
    # A quoting tweet embeds the parent under quoted_status_result — only the
    # quoting tweet should be parsed, never the embedded parent.
    quoter = _tweet("9001", "great thread, we want this", "lead_user",
                    quoted_status_result={"result": _tweet("1500", "original", "acme")})
    body = _timeline(quoter)
    posts = parse_posts(body)
    assert [p.reel_id for p in posts] == ["9001"]


def test_replies_get_is_reply_true_and_exclude_parent():
    body = _timeline(_tweet("1500", "the original post", "acme"),       # the parent
                     _tweet("2001", "pricing? +1 415 555 0142", "sam"),  # a reply
                     _tweet("2002", "interested too", "alex"))
    replies = parse_replies(body, parent_id="1500")
    assert [(c.comment_id, c.is_reply) for c in replies] == [("2001", True), ("2002", True)]
    assert all(c.is_reply for c in replies)


def test_quotes_get_is_reply_false():
    body = _timeline(_tweet("3001", "quoting this, where to buy? +1 312 555 0199", "buyer"))
    quotes = parse_quotes(body, parent_id="1500")
    assert [(c.comment_id, c.is_reply) for c in quotes] == [("3001", False)]


def test_detection_is_by_shape_not_url():
    body = _timeline(_tweet("1", "hello world", "a"))
    assert looks_like_timeline_response("https://drifted.example/GraphQL/abc", body)
    assert not looks_like_timeline_response("", {"data": {"home": {}}})


def test_composite_cursor_roundtrip():
    assert unpack_cursor(pack_cursor(3, 7)) == (3, 7)
    assert unpack_cursor(None) == (0, 0)
    assert unpack_cursor("5") == (0, 0)         # tolerant of a legacy single value
    assert unpack_cursor("4|2") == (4, 2)
