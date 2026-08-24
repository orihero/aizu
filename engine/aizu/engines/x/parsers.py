"""Pure parsers for intercepted X (Twitter) GraphQL JSON (PRD §3, §5, §13).

Read the page's own internal GraphQL responses (HomeTimeline / SearchTimeline /
ListLatestTimeline / TweetDetail / the Quotes timeline); never craft API calls. X
rotates ``doc_id``s and required ``features`` every ~2–4 weeks specifically to break
scrapers, so detection is **by response shape, not by URL/`doc_id`**: we recursively
find tweet-shaped nodes (a ``rest_id`` + a ``legacy.full_text``) and pull the few
fields we need, tolerating missing/renamed wrappers.

A tweet node embeds the tweet it quotes under ``quoted_status_result`` — we do NOT
descend into that wrapper, so the *quoting* tweet is captured once and the embedded
*quoted* parent is never miscounted as a separate item.

These functions are deterministic and side-effect free — the part of the CDP layer
unit-tested against captured fixtures without a browser. ``cdp.py`` feeds real
intercepted bodies through them. Confirm endpoint URL substrings + the live shape
in DevTools and expect drift; see DEFAULT_*_HINTS.
"""
from __future__ import annotations

from typing import Any, Iterator, Optional

from ...core.feed import Comment, Reel

# URL substrings that *hint* a timeline/detail response. Cheap pre-filter only; the
# shape check is the real gate, so a rotated doc_id still parses (PRD §13).
DEFAULT_TIMELINE_URL_HINTS = ("HomeTimeline", "SearchTimeline", "ListLatestTimeline",
                              "graphql")
DEFAULT_DETAIL_URL_HINTS = ("TweetDetail", "Quotes", "graphql")

# Wrapper subtrees that hold a *different* tweet than the node being read: the
# embedded quoted/retweeted parent. Descending into them would double-count.
_TWEET_SKIP_KEYS = ("quoted_status_result", "retweeted_status_result")


def _walk(obj: Any, skip_keys: tuple[str, ...] = ()) -> Iterator[dict]:
    if isinstance(obj, dict):
        yield obj
        for k, v in obj.items():
            if k in skip_keys:
                continue
            yield from _walk(v, skip_keys)
    elif isinstance(obj, list):
        for v in obj:
            yield from _walk(v, skip_keys)


def _legacy(node: dict) -> dict:
    lg = node.get("legacy")
    return lg if isinstance(lg, dict) else {}


def _full_text(node: dict) -> str:
    """The tweet body, tolerant of the note_tweet (long-form) variant."""
    lg = _legacy(node)
    t = lg.get("full_text")
    if isinstance(t, str) and t:
        return t
    t = node.get("full_text")
    if isinstance(t, str) and t:
        return t
    # note_tweet.note_tweet_results.result.text (long tweets)
    nt = node.get("note_tweet")
    if isinstance(nt, dict):
        for d in _walk(nt):
            txt = d.get("text")
            if isinstance(txt, str) and txt:
                return txt
    return ""


def _rest_id(node: dict) -> Optional[str]:
    v = node.get("rest_id") or node.get("rest_id_str")
    if isinstance(v, (str, int)) and str(v):
        return str(v)
    lg = _legacy(node)
    v = lg.get("id_str")
    return str(v) if isinstance(v, (str, int)) and str(v) else None


def _screen_name(node: dict) -> str:
    """The posting account's @handle. Lives under
    ``core.user_results.result.legacy.screen_name``; find it tolerantly."""
    core = node.get("core")
    if isinstance(core, dict):
        for d in _walk(core):
            sn = d.get("screen_name")
            if isinstance(sn, str) and sn:
                return sn
    sn = _legacy(node).get("screen_name")
    return sn if isinstance(sn, str) else ""


def _author_rest_id(node: dict) -> str:
    """The posting account's STABLE `rest_id`, from the user subtree.

    `_screen_name` returns the @handle, which changes on a rename; `rest_id` does
    not, and X's own `UserByScreenName` keys on it. Taken from `core` only — the
    tweet's OWN rest_id lives at the top level and must never be mistaken for the
    author's (Campaign Lab, Remedy Sheet #2)."""
    core = node.get("core")
    if not isinstance(core, dict):
        return ""
    for d in _walk(core):
        rid = d.get("rest_id") or d.get("rest_id_str")
        if isinstance(rid, (str, int)) and str(rid).strip():
            return str(rid)
    return ""


def _looks_like_tweet(d: dict) -> bool:
    """A tweet node: has a rest_id and a non-empty full_text."""
    return _rest_id(d) is not None and bool(_full_text(d))


def _tweet_nodes(body: Any) -> Iterator[dict]:
    """Every top-level tweet node, NOT descending into embedded quoted/RT parents."""
    for d in _walk(body, _TWEET_SKIP_KEYS):
        if _looks_like_tweet(d):
            yield d


def looks_like_timeline_response(url: str, body: Any) -> bool:
    if not isinstance(body, (dict, list)):
        return False
    return any(True for _ in _tweet_nodes(body))


def parse_posts(body: Any) -> list[Reel]:
    """Extract parent posts (any type) from an intercepted timeline, de-duped by
    rest_id. A text-only tweet is first-class; on-screen/OCR text for image/video
    posts is a session-time vision pass, so only text + author are carried here."""
    out: list[Reel] = []
    seen: set[str] = set()
    for d in _tweet_nodes(body):
        rid = _rest_id(d)
        if rid is None or rid in seen:
            continue
        seen.add(rid)
        out.append(Reel(reel_id=rid, caption=_full_text(d), author=_screen_name(d),
                        author_id=_author_rest_id(d)))
    return out


def _tweets_to_comments(body: Any, *, is_reply: bool,
                        exclude_id: Optional[str]) -> list[Comment]:
    """Map tweet nodes onto Comment. ``is_reply`` marks the surface (True = a reply
    in the conversation tree, False = a quote-post). ``exclude_id`` drops the parent
    post itself, which TweetDetail includes as the root of the conversation."""
    out: list[Comment] = []
    seen: set[str] = set()
    for d in _tweet_nodes(body):
        rid = _rest_id(d)
        if rid is None or rid == exclude_id or rid in seen:
            continue
        seen.add(rid)
        out.append(Comment(comment_id=rid, username=_screen_name(d),
                           text=_full_text(d), is_reply=is_reply))
    return out


def parse_replies(body: Any, parent_id: Optional[str] = None) -> list[Comment]:
    """Replies in the conversation tree (incl. nested) → Comment(is_reply=True)."""
    return _tweets_to_comments(body, is_reply=True, exclude_id=parent_id)


def parse_quotes(body: Any, parent_id: Optional[str] = None) -> list[Comment]:
    """Quote-posts (standalone tweets embedding the parent) → Comment(is_reply=False)."""
    return _tweets_to_comments(body, is_reply=False, exclude_id=parent_id)


# ---- composite cursor (two surfaces behind the single cursor slot, PRD §5/§7) ----
_CURSOR_SEP = "|"


def pack_cursor(reply_watermark: int, quote_watermark: int) -> str:
    """One stored cursor encoding both sub-watermarks (``"<reply>|<quote>"``)."""
    return f"{reply_watermark}{_CURSOR_SEP}{quote_watermark}"


def unpack_cursor(cursor: Optional[str]) -> tuple[int, int]:
    """Split the composite cursor back into (reply_watermark, quote_watermark);
    tolerant of an absent/legacy single value."""
    if not cursor or _CURSOR_SEP not in cursor:
        return 0, 0
    r, _, q = cursor.partition(_CURSOR_SEP)
    return (int(r) if r.isdigit() else 0, int(q) if q.isdigit() else 0)
