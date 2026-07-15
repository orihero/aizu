"""LinkedInFeed — LinkedIn's Playwright-over-CDP feed (PRD §3, §5, §13).

The generic CDP plumbing (attach, interception template, scroll, screenshots,
canary, seed-source walk) lives in ``reelradar.core.cdp.CDPFeedBase``; this module
is only the LinkedIn-specific surface around it:

  - which sources to walk (home feed + seeded hashtags + people/companies),
  - shape-based post-vs-comment classification of intercepted Voyager JSON,
  - opening one post full-screen so its comments load,
  - paginating the comment list.

Comments are read one post at a time: ``open_reel`` opens a post full-screen and
records it as the current post, and comment responses that stream in while that
post is open are attributed to it — the same "attribute by the page that received
it" guard Instagram uses, simplified because LinkedIn reads posts serially.

This touches live LinkedIn, so it cannot run in CI; its parsing core is in
``parsers.py`` and unit-tested against fixtures. URL hints are a cheap pre-filter;
detection is by response shape, so a drifted endpoint still parses (PRD §13).
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Optional

from ...core.cdp import CDPBaseConfig, CDPFeedBase, PlaywrightTimeout
from ...core.feed import Comment, Reel
from ...core.logsetup import get_logger
from .parsers import (DEFAULT_COMMENT_URL_HINTS, DEFAULT_POST_URL_HINTS,
                      looks_like_comment_response, looks_like_post_response,
                      parse_comments, parse_posts)

log = get_logger(__name__)

FEED_URL = "https://www.linkedin.com/feed/"
HASHTAG_URL = "https://www.linkedin.com/feed/hashtag/{tag}/"
ACTIVITY_URL = "https://www.linkedin.com/in/{slug}/recent-activity/all/"
# A post permalink is the activity urn dropped into the feed-update path.
POST_PERMALINK = "https://www.linkedin.com/feed/update/{urn}/"


@dataclass
class LinkedInCDPConfig(CDPBaseConfig):
    post_url_hints: tuple[str, ...] = DEFAULT_POST_URL_HINTS
    comment_url_hints: tuple[str, ...] = DEFAULT_COMMENT_URL_HINTS


class LinkedInFeed(CDPFeedBase):
    def __init__(self, cfg: Optional[LinkedInCDPConfig] = None):
        super().__init__(cfg or LinkedInCDPConfig())
        self._current_post_id: Optional[str] = None

    # ---- interception hooks ----
    def _url_hints(self) -> tuple[str, ...]:
        return tuple(self.cfg.post_url_hints) + tuple(self.cfg.comment_url_hints)

    def _classify(self, url: str, body, response) -> None:
        # Posts and comments arrive on different Voyager calls; classify by shape.
        # A comment payload is attributed to the post currently opened full-screen
        # (LinkedIn reads posts serially), never to a guessed "active" post.
        if looks_like_post_response(url, body):
            for reel in parse_posts(body):
                self._enqueue_reel(reel)
            return
        if looks_like_comment_response(url, body):
            rid = self._current_post_id
            if rid is None:
                return  # comments arrived with no post open → cannot attribute, drop
            comments, _cursor = parse_comments(body)
            if comments:
                bucket = self._comments_by_reel.setdefault(rid, [])
                known = {c.comment_id for c in bucket}
                bucket.extend(c for c in comments if c.comment_id not in known)

    # ---- discovery sources ----
    def _sources(self) -> list[str]:
        urls: list[str] = []
        if self.cfg.include_home_feed:
            urls.append(FEED_URL)
        for tag in self.cfg.seed_hashtags:
            urls.append(HASHTAG_URL.format(tag=str(tag).lstrip("#")))
        for acct in self.cfg.seed_accounts:
            urls.append(ACTIVITY_URL.format(slug=str(acct).lstrip("@").strip("/")))
        return urls or [FEED_URL]

    # ---- open full-screen ----
    def open_reel(self, reel: Reel) -> bool:
        """Open the post full-screen via its permalink so the comment thread loads.
        Records it as the current post so streamed comments attribute correctly.
        Read-only navigation — never crafts an API call."""
        page = self._ensure_ipage()
        if page is None:
            return False
        self._current_post_id = reel.reel_id
        target = POST_PERMALINK.format(urn=reel.reel_id)
        try:
            if not str(page.url).startswith(target.rstrip("/")):
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
        """True when LinkedIn is showing its not-available / removed screen."""
        try:
            return bool(page.evaluate(
                """() => /this content isn't available|page not found|no longer available/i
                     .test(document.body ? document.body.innerText : '')"""))
        except Exception:
            return False

    # ---- comments ----
    def fetch_comments(self, reel_id: str, since_cursor: Optional[str]
                       ) -> tuple[list[Comment], Optional[str]]:
        # Reset the canary; if no comment response arrives, healthy() flips and the
        # session's empty-interception canary can halt the run.
        self._saw_data = False
        self._open_comments_and_paginate()

        all_comments = self._comments_by_reel.get(reel_id, [])
        # Count watermark = robust "new since last poll" that survives re-scrapes.
        start = int(since_cursor) if (since_cursor and since_cursor.isdigit()) else 0
        new = all_comments[start:]
        log.debug("LinkedIn fetched comments · post=%s new=%d total=%d",
                  reel_id, len(new), len(all_comments))
        return new, str(len(all_comments))

    def _open_comments_and_paginate(self) -> None:
        """Click the post's comment control (loads the thread) and scroll/expand so
        the comments endpoint paginates. Selectors are the likeliest DevTools
        confirmation point if LinkedIn's DOM changes; the payload is parsed by shape
        so a renamed endpoint still works (PRD §13)."""
        page = self._ipage
        if page is None:
            return
        self._open_comment_thread(page)
        for _ in range(self.cfg.max_comment_scrolls):
            self._load_more_comments(page)
            self._scroll(page)
            time.sleep(self.cfg.settle_seconds)

    def _open_comment_thread(self, page) -> None:
        """Click the 'Comment' action so the thread expands and its Voyager call fires."""
        try:
            box = page.evaluate(
                """() => {
                  const btn = [...document.querySelectorAll(
                    "button[aria-label], button")].find(b =>
                    /comment/i.test(b.getAttribute('aria-label') || b.textContent || ''));
                  if (!btn) return null;
                  const r = btn.getBoundingClientRect();
                  if (r.width === 0 || r.height === 0) return null;
                  return {x: r.x + r.width / 2, y: r.y + r.height / 2};
                }""")
            if box:
                page.mouse.click(box["x"], box["y"])
                time.sleep(self.cfg.settle_seconds)
        except Exception:
            pass  # comments may already be inline; interception still fires

    def _load_more_comments(self, page) -> None:
        """Click a 'Load more comments' / 'Show more' control if present."""
        try:
            page.evaluate(
                """() => {
                  const btn = [...document.querySelectorAll("button")].find(b =>
                    /load more comments|show more (comments|results|replies)/i
                      .test(b.textContent || ''));
                  if (btn) btn.click();
                }""")
        except Exception:
            pass
