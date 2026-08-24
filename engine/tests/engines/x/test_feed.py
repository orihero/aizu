"""XFeed.fetch_comments — merges the reply tree + Quotes timeline behind the single
interface, paged by the composite cursor (no browser).

Drives _classify directly in each mode (as the live interception would) and stubs
the page-loading so the merge + composite-cursor watermark are tested in isolation.
"""
from aizu.engines.x.cdp import XFeed


def _tweet(rest_id, text, handle):
    return {"rest_id": rest_id,
            "core": {"user_results": {"result": {"legacy": {"screen_name": handle}}}},
            "legacy": {"full_text": text, "conversation_id_str": rest_id}}


def _body(*tweets):
    return {"data": {"entries": [{"tweet_results": {"result": t}} for t in tweets]}}


def _feed_with_stubs(monkeypatch, post_id, reply_tweets, quote_tweets):
    feed = XFeed()
    feed._current_post_id = post_id

    def load_replies():
        feed._mode = "reply"
        feed._classify("TweetDetail", _body(*reply_tweets), None)

    def load_quotes(rid):
        feed._mode = "quote"
        feed._classify("Quotes", _body(*quote_tweets), None)

    monkeypatch.setattr(feed, "_load_replies", load_replies)
    monkeypatch.setattr(feed, "_load_quotes", load_quotes)
    return feed


def test_merges_replies_and_quotes(monkeypatch):
    feed = _feed_with_stubs(
        monkeypatch, "1500",
        reply_tweets=[_tweet("2001", "pricing? +1 415 555 0142", "aziz")],
        quote_tweets=[_tweet("3001", "want to buy? +1 415 555 0142", "buyer")])
    comments, cursor = feed.fetch_comments("1500", None)
    by_id = {c.comment_id: c for c in comments}
    assert by_id["2001"].is_reply is True            # reply surface
    assert by_id["3001"].is_reply is False           # quote surface
    assert cursor == "1|1"                            # one reply, one quote seen


def test_composite_cursor_only_returns_new_per_surface(monkeypatch):
    feed = _feed_with_stubs(
        monkeypatch, "1500",
        reply_tweets=[_tweet("2001", "first reply", "a")],
        quote_tweets=[_tweet("3001", "first quote", "b")])
    first, cursor = feed.fetch_comments("1500", None)
    assert len(first) == 2 and cursor == "1|1"

    # A second poll adds one new reply and one new quote; only the new ones return.
    def load_replies():
        feed._mode = "reply"
        feed._classify("TweetDetail", _body(_tweet("2001", "first reply", "a"),
                                            _tweet("2002", "second reply", "c")), None)

    def load_quotes(rid):
        feed._mode = "quote"
        feed._classify("Quotes", _body(_tweet("3001", "first quote", "b"),
                                       _tweet("3002", "second quote", "d")), None)

    monkeypatch.setattr(feed, "_load_replies", load_replies)
    monkeypatch.setattr(feed, "_load_quotes", load_quotes)
    second, cursor2 = feed.fetch_comments("1500", cursor)
    assert {c.comment_id for c in second} == {"2002", "3002"}
    assert cursor2 == "2|2"


def test_discover_mode_enqueues_parent_posts():
    feed = XFeed()
    feed._mode = "discover"
    feed._classify("HomeTimeline", _body(_tweet("1500", "hiring a CFO", "acme")), None)
    assert [r.reel_id for r in feed._reel_queue] == ["1500"]


def test_fetch_comments_snapshots_both_surfaces_under_the_queue_lock(monkeypatch):
    """The composite cursor packs BOTH lengths, so both lists must be read in one
    atomic step.

    `page.on("response")` handlers dispatch on the Playwright owner thread now
    (`core/pw_owner.py`), so `_classify` appends there while `fetch_comments`
    reads on the caller's — widest exactly after a deadline expiry, when the
    caller has walked away but the owner keeps pumping the dispatcher. A list
    that grows mid-read packs a watermark past tweets that were never returned;
    those are never scored and never re-read on a later poll.
    """
    import threading

    feed = _feed_with_stubs(monkeypatch, "1500", reply_tweets=[], quote_tweets=[])
    monkeypatch.setattr(feed, "_load_replies", lambda: None)
    monkeypatch.setattr(feed, "_load_quotes", lambda rid: None)
    feed._mode = "reply"
    feed._classify("TweetDetail", _body(_tweet("2001", "a reply", "a")), None)
    feed._mode = "quote"
    feed._classify("Quotes", _body(_tweet("3001", "a quote", "b")), None)

    out: dict = {}

    def _read():
        out["result"] = feed.fetch_comments("1500", None)

    reader = threading.Thread(target=_read, daemon=True)
    with feed._queue_lock:          # stand in for `_classify` on the owner thread
        reader.start()
        reader.join(timeout=0.5)
        assert reader.is_alive(), (
            "fetch_comments read the live reply/quote lists without taking "
            "_queue_lock — the slices and the packed cursor can disagree")
        feed._mode = "quote"
        feed._quotes_by_reel["1500"].append(
            feed._quotes_by_reel["1500"][0].__class__(
                comment_id="3002", username="c", text="late quote", is_reply=False))
    reader.join(timeout=5)

    comments, cursor = out["result"]
    assert cursor == "1|2"
    assert {c.comment_id for c in comments} == {"2001", "3001", "3002"}
