"""Pure parsers for intercepted Instagram JSON (PRD §3, §13).

Read the page's own internal API responses; never craft API calls. Because the
exact endpoint shapes drift, detection is **by shape, not by hardcoded IDs**:
we recursively find dicts that look like a media (reel) or a comment and pull
the few fields we need, tolerating missing/renamed wrappers.

These functions are deterministic and side-effect free — they are the part of
the CDP layer that can be unit-tested against captured fixtures without a
browser. `cdp.py` feeds real intercepted bodies through them.

Confirm endpoint URL substrings in DevTools (they drift); see DEFAULT_*_HINTS.
"""
from __future__ import annotations

from typing import Any, Iterator, Optional

from .feed import Comment, Reel

# URL substrings that *hint* a response is the feed/comment payload. Used only
# as a cheap pre-filter; the shape check below is the real gate, so a drifted
# URL still parses as long as the body shape is recognizable.
DEFAULT_REEL_URL_HINTS = ("/clips/", "/reels_media", "/feed/", "graphql")
DEFAULT_COMMENT_URL_HINTS = ("/comments/", "/comments?", "graphql")


# Subtrees that contain comment-SHAPED dicts which are NOT comments of the
# response's media: a real caption node carries pk + text + user (exactly a
# comment's shape), and preview_comments belong to whichever media embeds them
# (often a *different* reel in the same feed batch). Hunting comments inside
# them is how leads end up attributed to the wrong reel/author.
COMMENT_SKIP_KEYS = ("caption", "edge_media_to_caption", "preview_comments")


def _walk(obj: Any, skip_keys: tuple[str, ...] = ()) -> Iterator[dict]:
    """Yield every dict nested anywhere in a JSON structure.

    skip_keys: dict keys whose subtrees are not descended into."""
    if isinstance(obj, dict):
        yield obj
        for k, v in obj.items():
            if k in skip_keys:
                continue
            yield from _walk(v, skip_keys)
    elif isinstance(obj, list):
        for v in obj:
            yield from _walk(v, skip_keys)


def _first_str(d: dict, *keys: str) -> Optional[str]:
    for k in keys:
        v = d.get(k)
        if isinstance(v, str) and v:
            return v
    return None


def _username(node: dict) -> Optional[str]:
    user = node.get("user") or node.get("owner")
    if isinstance(user, dict):
        return _first_str(user, "username", "handle")
    return _first_str(node, "username")


def _caption_text(node: dict) -> str:
    cap = node.get("caption")
    if isinstance(cap, dict):
        return _first_str(cap, "text") or ""
    if isinstance(cap, str):
        return cap
    # GraphQL edge shape: edge_media_to_caption.edges[0].node.text
    edge = node.get("edge_media_to_caption")
    if isinstance(edge, dict):
        edges = edge.get("edges") or []
        if edges and isinstance(edges[0], dict):
            n = edges[0].get("node") or {}
            return _first_str(n, "text") or ""
    return ""


def _looks_like_media(d: dict) -> bool:
    """A reel/media node: has a stable id (code/pk/shortcode) and an author."""
    has_id = any(isinstance(d.get(k), (str, int)) and d.get(k) not in (None, "")
                 for k in ("code", "shortcode", "pk", "id"))
    if not has_id:
        return False
    # Must look like media, not a comment: media carries caption/clips/video
    # signals or an explicit media_type, and is not a bare comment node.
    media_signal = any(k in d for k in
                       ("caption", "edge_media_to_caption", "clips_metadata",
                        "video_versions", "media_type", "image_versions2",
                        "play_count", "view_count"))
    return media_signal and _username(d) is not None


def _looks_like_comment(d: dict) -> bool:
    """A comment node: has text + author + an id, and is NOT a media node."""
    if _first_str(d, "text") is None:
        return False
    if _username(d) is None:
        return False
    has_id = any(d.get(k) not in (None, "") for k in ("pk", "id", "comment_id"))
    if not has_id:
        return False
    # Exclude media nodes that happen to have text fields.
    media_ish = any(k in d for k in
                    ("caption", "clips_metadata", "video_versions",
                     "image_versions2", "media_type"))
    return not media_ish


def _media_code(d: dict) -> Optional[str]:
    """The media's shortcode — the ONLY id usable as a /reel/<code>/ permalink.

    pk / numeric id are deliberately NOT a fallback: a pk-based reel_id makes a
    permalink that 404s, and everything downstream (open_reel, comment fetch,
    the lead's reel link) silently runs against the wrong page."""
    return _first_str(d, "code", "shortcode")


def _comment_id(d: dict) -> str:
    return str(d.get("pk") or d.get("id") or d.get("comment_id"))


def looks_like_reel_response(url: str, body: Any) -> bool:
    if not isinstance(body, (dict, list)):
        return False
    # Skip comment-ish subtrees defensively so a drifted comment shape can
    # never make a comments payload classify as a media payload.
    return any(_looks_like_media(d) for d in _walk(body, COMMENT_SKIP_KEYS))


def looks_like_comment_response(url: str, body: Any) -> bool:
    if not isinstance(body, (dict, list)):
        return False
    return any(_looks_like_comment(d) for d in _walk(body, COMMENT_SKIP_KEYS))


def parse_reels(body: Any) -> list[Reel]:
    """Extract reels from an intercepted feed/clips response, de-duped by code.

    Media without a shortcode are skipped — without a code there is no valid
    permalink, so the reel could never be opened or verified."""
    out: list[Reel] = []
    seen: set[str] = set()
    for d in _walk(body):
        if not _looks_like_media(d):
            continue
        rid = _media_code(d)
        if rid is None or rid in seen:
            continue
        seen.add(rid)
        out.append(Reel(
            reel_id=rid,
            caption=_caption_text(d),
            author=_username(d) or "",
        ))
    return out


def media_pk_codes(body: Any) -> dict[str, str]:
    """Map every media node's pk/id → its shortcode.

    The comment endpoint identifies its media by numeric pk (in the REST path
    or the GraphQL `media_id` variable), while the engine keys reels by
    shortcode — this mapping is how an intercepted comment response is
    attributed to the RIGHT reel instead of a guessed 'active' one."""
    out: dict[str, str] = {}
    for d in _walk(body):
        if not _looks_like_media(d):
            continue
        code = _media_code(d)
        if code is None:
            continue
        for k in ("pk", "id"):
            v = d.get(k)
            if v is None or isinstance(v, (dict, list)):
                continue
            s = str(v)
            if s:
                out[s] = code
                # ids sometimes arrive as "<media_pk>_<owner_pk>"
                if "_" in s:
                    out[s.split("_", 1)[0]] = code
    return out


def _media_url(d: dict) -> Optional[str]:
    """Best-effort CDN mp4 URL from a reel-shaped node.

    Instagram's REST shape is video_versions: [{url, width, height, type}, ...]
    — empirically the first entry is assumed highest quality, but this is
    UNVERIFIED against live traffic (no captured fixture confirms the value,
    only that the key exists — see _looks_like_media's media_signal check).
    Falls back to a top-level video_url key for the GraphQL-shaped variant.
    Never raises; returns None on any shape mismatch."""
    vv = d.get("video_versions")
    if isinstance(vv, list) and vv:
        url = vv[0].get("url") if isinstance(vv[0], dict) else None
        if isinstance(url, str) and url:
            return url
    vu = d.get("video_url")
    return vu if isinstance(vu, str) and vu else None


def media_urls_by_pk(body: Any) -> dict[str, str]:
    """Map every media node's pk/id → its best-effort CDN video URL.

    Mirrors media_pk_codes above (same pk/"<pk>_<owner>" split handling), but
    carries the video URL instead of the shortcode — this is how CDPFeed's
    capture_audio (engines/instagram/cdp.py) finds the source clip to re-fetch
    for STT without adding a media_url field to the shared Reel dataclass.
    UNVERIFIED against live traffic: see _media_url's caveat above."""
    out: dict[str, str] = {}
    for d in _walk(body):
        if not _looks_like_media(d):
            continue
        url = _media_url(d)
        if url is None:
            continue
        for k in ("pk", "id"):
            v = d.get(k)
            if v is None or isinstance(v, (dict, list)):
                continue
            s = str(v)
            if s:
                out[s] = url
                # ids sometimes arrive as "<media_pk>_<owner_pk>"
                if "_" in s:
                    out[s.split("_", 1)[0]] = url
    return out


def _forward_cursor(body: Any) -> Optional[str]:
    """The forward pagination cursor, tolerant of shape drift.

    Modern GraphQL nests it as page_info.end_cursor (+ has_next_page); older
    REST shapes use next_max_id / next_min_id at the top level. Return the
    forward cursor only when more pages exist, else None (no more to fetch).
    """
    for d in _walk(body):
        pi = d.get("page_info")
        if isinstance(pi, dict) and pi.get("end_cursor"):
            if pi.get("has_next_page") is False:
                return None
            return str(pi["end_cursor"])
    if isinstance(body, dict):
        return _first_str(body, "next_max_id", "next_min_id", "next_cursor")
    return None


def parse_comments(body: Any) -> tuple[list[Comment], Optional[str]]:
    """Extract top-level comments + the pagination cursor for "new since last poll".

    Cursor: prefer GraphQL page_info.end_cursor, fall back to next_max_id /
    next_min_id, so a drifted shape still resumes (PRD §13).
    """
    out: list[Comment] = []
    seen: set[str] = set()
    for d in _walk(body, COMMENT_SKIP_KEYS):
        if not _looks_like_comment(d):
            continue
        cid = _comment_id(d)
        if cid in seen or cid in ("", "None"):
            continue
        seen.add(cid)
        out.append(Comment(
            comment_id=cid,
            username=_username(d) or "",
            text=_first_str(d, "text") or "",
            lang=_first_str(d, "lang", "language"),
            is_reply=bool(d.get("parent_comment_id") or d.get("parent_id")),
        ))
    return out, _forward_cursor(body)
