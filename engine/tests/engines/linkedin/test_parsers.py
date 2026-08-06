"""Pure LinkedIn Voyager parsers — shape detection + extraction, no browser.

Locks the by-shape contract: posts and comments are recognized by their structure
(not the request URL), so a drifted endpoint still parses; the several Voyager
text-nesting variants are tolerated; and the paging cursor advances correctly.
"""
from aizu.engines.linkedin.parsers import (looks_like_comment_response,
                                                 looks_like_post_response,
                                                 parse_comments, parse_posts)


def _post(urn, text, author):
    return {"commentary": {"text": {"text": text}},
            "actor": {"name": {"text": author}},
            "entityUrn": urn}


def _comment(urn, text, who, **extra):
    return {"commenter": {"title": {"text": who}},
            "commentV2": {"text": {"text": text}},
            "entityUrn": urn, **extra}


def _feed_body():
    return {"data": {"elements": [
        _post("urn:li:activity:111", "We are hiring a fractional CFO", "Acme Corp"),
        _post("urn:li:activity:222", "Thoughts on remote work", "Jane Doe"),
    ]}}


def _comments_body():
    return {"paging": {"start": 0, "count": 2, "total": 5}, "elements": [
        _comment("urn:li:comment:(activity:111,1)", "What's the rate? +1 415 555 0142", "Alex K"),
        _comment("urn:li:comment:(activity:111,2)", "Congrats! 🎉", "Bot User"),
    ]}


def test_parses_posts_with_author_and_copy():
    posts = parse_posts(_feed_body())
    assert [(p.reel_id, p.author) for p in posts] == [
        ("urn:li:activity:111", "Acme Corp"),
        ("urn:li:activity:222", "Jane Doe")]
    assert posts[0].caption.startswith("We are hiring")


def test_parses_comments_with_commenter_and_text():
    comments, cursor = parse_comments(_comments_body())
    assert [(c.username, c.comment_id) for c in comments] == [
        ("Alex K", "urn:li:comment:(activity:111,1)"),
        ("Bot User", "urn:li:comment:(activity:111,2)")]
    assert comments[0].text.startswith("What's the rate")
    assert cursor == "2"          # start(0)+count(2) < total(5) → next offset


def test_cursor_is_none_when_no_more_pages():
    body = {"paging": {"start": 4, "count": 2, "total": 5}, "elements": []}
    _, cursor = parse_comments(body)
    assert cursor is None


def test_detection_is_by_shape_not_url():
    # A drifted/unknown endpoint still classifies correctly from the body shape.
    assert looks_like_post_response("https://drifted.example/x", _feed_body())
    assert looks_like_comment_response("https://drifted.example/y", _comments_body())
    # A post payload must NOT classify as comments, and vice-versa.
    assert not looks_like_comment_response("", _feed_body())
    assert not looks_like_post_response("", _comments_body())


def test_tolerates_flat_text_variants():
    # Voyager sometimes flattens text to a bare string or one-level {"text": ...}.
    body = {"elements": [
        {"commentary": {"text": "flat string copy"},
         "actor": {"name": "Flat Author"}, "entityUrn": "urn:li:activity:9"}]}
    posts = parse_posts(body)
    assert posts[0].caption == "flat string copy"
    assert posts[0].author == "Flat Author"


def test_reply_flag_set_from_parent_urn():
    body = {"elements": [
        _comment("urn:li:comment:(activity:111,3)", "agreed", "Replier",
                 parentCommentUrn="urn:li:comment:(activity:111,1)")]}
    comments, _ = parse_comments(body)
    assert comments[0].is_reply is True
