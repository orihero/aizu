"""Pure parsers for intercepted LinkedIn Voyager JSON (PRD §3, §5, §13).

Read the page's own internal Voyager API responses; never craft API calls. The
exact Voyager shapes drift, so detection is **by shape, not by hardcoded IDs**: we
recursively find dicts that look like a feed post (an UpdateV2 with commentary +
actor) or a comment (a CommentV2 with a commenter), tolerating missing/renamed
wrappers and the several text-nesting variants Voyager uses.

These functions are deterministic and side-effect free — the part of the CDP layer
that is unit-tested against captured fixtures without a browser. ``cdp.py`` feeds
real intercepted bodies through them.

Confirm endpoint URL substrings + the live response shape in DevTools once and
expect drift; see DEFAULT_*_HINTS.
"""
from __future__ import annotations

from typing import Any, Iterator, Optional

from ...core.feed import Comment, Reel

# URL substrings that *hint* a response is the feed/comment payload. Cheap
# pre-filter only; the shape check is the real gate, so a drifted URL still parses.
DEFAULT_POST_URL_HINTS = ("voyagerFeedDash", "/feed/", "feedDashMainFeed",
                          "graphql", "/voyager/")
DEFAULT_COMMENT_URL_HINTS = ("Comments", "socialDetail", "comment", "graphql",
                             "/voyager/")


def _walk(obj: Any) -> Iterator[dict]:
    """Yield every dict nested anywhere in a JSON structure."""
    if isinstance(obj, dict):
        yield obj
        for v in obj.values():
            yield from _walk(v)
    elif isinstance(obj, list):
        for v in obj:
            yield from _walk(v)


def _text(node: Any) -> str:
    """Voyager wraps display text as a string, ``{"text": "..."}``, or
    ``{"text": {"text": "..."}}`` (annotated/attributed text). Tolerate all three."""
    if isinstance(node, str):
        return node
    if isinstance(node, dict):
        inner = node.get("text")
        if isinstance(inner, str):
            return inner
        if isinstance(inner, dict):
            t = inner.get("text")
            if isinstance(t, str):
                return t
    return ""


def _actor_name(node: dict) -> str:
    """The post author's display name (``actor.name.text``)."""
    actor = node.get("actor")
    if isinstance(actor, dict):
        return _text(actor.get("name"))
    return ""


def _commenter_name(node: dict) -> str:
    """The commenter's display name across the commenter-wrapper variants."""
    commenter = node.get("commenter")
    if isinstance(commenter, dict):
        # commenter.title.text (member) or commenter.name.text, or a nested member.
        for key in ("title", "name"):
            t = _text(commenter.get(key))
            if t:
                return t
        for v in commenter.values():
            if isinstance(v, dict):
                t = _text(v.get("name") or v.get("title"))
                if t:
                    return t
    return ""


def _urn(node: dict) -> Optional[str]:
    """A stable id for the node: prefer an explicit urn, else entityUrn."""
    for k in ("urn", "entityUrn", "dashEntityUrn", "backendUrn", "objectUrn"):
        v = node.get(k)
        if isinstance(v, str) and v:
            return v
    meta = node.get("updateMetadata")
    if isinstance(meta, dict):
        v = meta.get("urn") or meta.get("shareUrn")
        if isinstance(v, str) and v:
            return v
    return None


def _looks_like_post(d: dict) -> bool:
    """A feed post: has commentary text + an actor + a urn, and is not a comment."""
    if "commenter" in d or "commentV2" in d:
        return False
    has_commentary = "commentary" in d
    if not has_commentary:
        return False
    if not _actor_name(d):
        return False
    return _urn(d) is not None


def _looks_like_comment(d: dict) -> bool:
    """A comment node: has a commenter + comment text + an id."""
    if "commenter" not in d:
        return False
    if not _commenter_name(d):
        return False
    text = _comment_text(d)
    if not text:
        return False
    return _urn(d) is not None


def _commentary_text(d: dict) -> str:
    return _text(d.get("commentary"))


def _comment_text(d: dict) -> str:
    """Comment body across the ``comment`` / ``commentV2`` wrapper variants."""
    for key in ("commentV2", "comment"):
        t = _text(d.get(key))
        if t:
            return t
    return _text(d.get("value")) if "value" in d else ""


def looks_like_post_response(url: str, body: Any) -> bool:
    if not isinstance(body, (dict, list)):
        return False
    return any(_looks_like_post(d) for d in _walk(body))


def looks_like_comment_response(url: str, body: Any) -> bool:
    if not isinstance(body, (dict, list)):
        return False
    return any(_looks_like_comment(d) for d in _walk(body))


def parse_posts(body: Any) -> list[Reel]:
    """Extract feed posts from an intercepted Voyager response, de-duped by urn.

    Carousel/document/image OCR is a session-time vision pass (frames are captured
    from the opened post), so the parser only carries the copy + author here."""
    out: list[Reel] = []
    seen: set[str] = set()
    for d in _walk(body):
        if not _looks_like_post(d):
            continue
        rid = _urn(d)
        if rid is None or rid in seen:
            continue
        seen.add(rid)
        out.append(Reel(reel_id=rid, caption=_commentary_text(d),
                        author=_actor_name(d)))
    return out


def _forward_cursor(body: Any) -> Optional[str]:
    """Voyager paginates comments via ``paging {start, count, total}``. Return the
    next ``start`` offset when more remain, else None — tolerant of shape drift."""
    for d in _walk(body):
        paging = d.get("paging")
        if isinstance(paging, dict):
            try:
                start = int(paging.get("start", 0))
                count = int(paging.get("count", 0))
                total = int(paging.get("total", 0))
            except (TypeError, ValueError):
                continue
            nxt = start + count
            if total and nxt < total:
                return str(nxt)
            return None
    return None


def parse_comments(body: Any) -> tuple[list[Comment], Optional[str]]:
    """Extract comments + the pagination cursor for "new since last poll".

    Top-level comments are the v1 surface; reply expansion is a session-time
    refinement on matching comments (PRD §11)."""
    out: list[Comment] = []
    seen: set[str] = set()
    for d in _walk(body):
        if not _looks_like_comment(d):
            continue
        cid = _urn(d)
        if cid is None or cid in seen:
            continue
        seen.add(cid)
        out.append(Comment(
            comment_id=cid,
            username=_commenter_name(d),
            text=_comment_text(d),
            is_reply=bool(d.get("parentCommentUrn") or d.get("parentComment")),
        ))
    return out, _forward_cursor(body)
