"""XFeed — X's (Twitter) Playwright-over-CDP feed (PRD §3, §5, §13).

The generic CDP plumbing (attach, interception template, scroll, screenshots,
canary, seed-source walk) lives in ``reelradar.core.cdp.CDPFeedBase``; this module
is only the X-specific surface around it:

  - which sources to walk (For You feed + Search + Lists),
  - shape-based classification of intercepted GraphQL tweets, routed by the current
    read MODE (discover / reply / quote),
  - opening a tweet full-screen so its conversation loads,
  - the TWO match surfaces — the reply tree (TweetDetail) AND the Quotes timeline —
    merged behind the single ``fetch_comments`` interface with a composite cursor.

Comments are read one post at a time, attributed to the post currently opened
full-screen (``_current_post_id``) — never a guessed "active" tweet.

This touches live X, so it cannot run in CI; its parsing core is in ``parsers.py``
and unit-tested against fixtures. URL hints are a cheap pre-filter; detection is by
response shape, so a rotated ``doc_id`` still parses (PRD §13).
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Optional

from ...core.cdp import CDPBaseConfig, CDPFeedBase, PlaywrightTimeout
from ...core.feed import Comment, Reel
from ...core.logsetup import get_logger
from .parsers import (DEFAULT_DETAIL_URL_HINTS, DEFAULT_TIMELINE_URL_HINTS,
                      looks_like_timeline_response, pack_cursor, parse_posts,
                      parse_quotes, parse_replies, unpack_cursor)

log = get_logger(__name__)

FOR_YOU_URL = "https://x.com/home"
SEARCH_URL = "https://x.com/search?q={q}&f=live"
PROFILE_URL = "https://x.com/{handle}"
STATUS_PERMALINK = "https://x.com/i/status/{rest_id}"
QUOTES_URL = "https://x.com/i/status/{rest_id}/quotes"


@dataclass
class XCDPConfig(CDPBaseConfig):
    timeline_url_hints: tuple[str, ...] = DEFAULT_TIMELINE_URL_HINTS
    detail_url_hints: tuple[str, ...] = DEFAULT_DETAIL_URL_HINTS


class XFeed(CDPFeedBase):
    def __init__(self, cfg: Optional[XCDPConfig] = None):
        super().__init__(cfg or XCDPConfig())
        # Read mode steers _classify: discovery enqueues parent posts; reply/quote
        # accumulate into the two match-surface buckets for the open post.
        self._mode = "discover"
        self._current_post_id: Optional[str] = None
        self._replies_by_reel: dict[str, list[Comment]] = {}
        self._quotes_by_reel: dict[str, list[Comment]] = {}

    # ---- interception hooks ----
    def _url_hints(self) -> tuple[str, ...]:
        return tuple(self.cfg.timeline_url_hints) + tuple(self.cfg.detail_url_hints)

    def _classify(self, url: str, body, response) -> None:
        if not looks_like_timeline_response(url, body):
            return
        if self._mode == "discover":
            for reel in parse_posts(body):
                self._enqueue_reel(reel)
            return
        rid = self._current_post_id
        if rid is None:
            return  # tweets arrived with no post open → cannot attribute, drop
        if self._mode == "reply":
            self._merge(self._replies_by_reel, rid, parse_replies(body, rid))
        elif self._mode == "quote":
            self._merge(self._quotes_by_reel, rid, parse_quotes(body, rid))

    @staticmethod
    def _merge(bucket_by_reel: dict, rid: str, comments: list[Comment]) -> None:
        bucket = bucket_by_reel.setdefault(rid, [])
        known = {c.comment_id for c in bucket}
        bucket.extend(c for c in comments if c.comment_id not in known)

    # ---- discovery sources ----
    def _sources(self) -> list[str]:
        urls: list[str] = []
        if self.cfg.include_home_feed:
            urls.append(FOR_YOU_URL)                       # the For You feed
        for q in self.cfg.seed_hashtags:                   # saved searches / hashtags
            urls.append(SEARCH_URL.format(q=str(q).lstrip("#")))
        for handle in self.cfg.seed_accounts:              # followed accounts / List members
            urls.append(PROFILE_URL.format(handle=str(handle).lstrip("@")))
        return urls or [FOR_YOU_URL]

    # ---- open full-screen ----
    def open_reel(self, reel: Reel) -> bool:
        """Open the tweet full-screen via its status permalink so the conversation
        (reply tree) loads. Records it as the current post so streamed replies/quotes
        attribute correctly. Read-only navigation — never crafts an API call."""
        page = self._ensure_ipage()
        if page is None:
            return False
        self._current_post_id = reel.reel_id
        target = STATUS_PERMALINK.format(rest_id=reel.reel_id)
        try:
            if not str(page.url).startswith(target):
                page.goto(target, wait_until="domcontentloaded",
                          timeout=self.cfg.nav_timeout_ms)
                time.sleep(self.cfg.nav_settle_seconds)
        except PlaywrightTimeout:
            log.warning("CDP open_reel nav timed out · %s", target)
            return False
        except Exception:
            return False
        return not self._page_unavailable(page)

    @staticmethod
    def _page_unavailable(page) -> bool:
        """True when X is showing its "doesn't exist" / removed screen."""
        try:
            return bool(page.evaluate(
                """() => /this post (is unavailable|was deleted|doesn.t exist)|page does.?n.?t exist|hmm.{0,3}this page/i
                     .test(document.body ? document.body.innerText : '')"""))
        except Exception:
            return False

    # ---- comments: replies + quotes merged behind one interface (PRD §5) ----
    def fetch_comments(self, reel_id: str, since_cursor: Optional[str]
                       ) -> tuple[list[Comment], Optional[str]]:
        # Reset the canary; if no tweet response arrives, healthy() flips and the
        # session's empty-interception canary can halt the run.
        self._saw_data = False
        self._load_replies()
        self._load_quotes(reel_id)
        self._mode = "discover"   # restore so the discovery walk keeps intercepting

        replies = self._replies_by_reel.get(reel_id, [])
        quotes = self._quotes_by_reel.get(reel_id, [])
        # One composite cursor in the single slot: "<reply count>|<quote count>".
        rw, qw = unpack_cursor(since_cursor)
        new = replies[rw:] + quotes[qw:]
        new_cursor = pack_cursor(len(replies), len(quotes))
        log.debug("X fetched comments · post=%s replies+%d quotes+%d (cursor %s→%s)",
                  reel_id, len(replies) - rw, len(quotes) - qw, since_cursor, new_cursor)
        return new, new_cursor

    def _load_replies(self) -> None:
        """The conversation/reply tree streams from TweetDetail on the open status
        page; scroll it to paginate. Selectors are the likeliest DevTools
        confirmation point; payloads are parsed by shape (PRD §13)."""
        page = self._ipage
        if page is None:
            return
        self._mode = "reply"
        for _ in range(self.cfg.max_comment_scrolls):
            self._scroll(page)
            time.sleep(self.cfg.settle_seconds)

    def _load_quotes(self, reel_id: str) -> None:
        """Quote-posts are a SECOND surface on a different endpoint — navigate to the
        tweet's Quotes timeline and scroll it. Walked exhaustively only here, on the
        match-rich post being processed, to keep read volume down (PRD §5, §10)."""
        page = self._ipage
        if page is None:
            return
        self._mode = "quote"
        try:
            page.goto(QUOTES_URL.format(rest_id=reel_id), wait_until="domcontentloaded")
            time.sleep(self.cfg.nav_settle_seconds)
        except Exception:
            return
        for _ in range(self.cfg.max_comment_scrolls):
            self._scroll(page)
            time.sleep(self.cfg.settle_seconds)
