"""CDPFeed — Instagram's Playwright-over-CDP feed (PRD §3, §13).

The generic CDP plumbing (attach, interception template, scroll, screenshots,
canary, seed-source walk) lives in ``reelradar.core.cdp.CDPFeedBase``; this module
is only the Instagram-specific surface around it:

  - which sources to walk (home reels + hashtag/account grids),
  - shape-based reel-vs-comment classification + the lead-attribution guard,
  - opening a reel full-screen so comments + engagement controls exist,
  - paginating the comment dialog,
  - opt-in like/follow engagement.

This is the one component that touches live Instagram, so it cannot run in CI;
its *parsing* core lives in `core/parsers.py` and is unit-tested against fixtures.

Endpoint URL hints (DEFAULT_*_HINTS in parsers) are a cheap pre-filter only —
detection is by response *shape*, so a drifted URL still parses. Confirm the
current endpoints in DevTools once and expect drift (PRD §12, §13).
"""
from __future__ import annotations

import re
import time
from dataclasses import dataclass
from typing import Optional
from urllib.parse import unquote_plus

from ...core.cdp import CDPBaseConfig, CDPFeedBase, PlaywrightTimeout
from ...core.feed import Reel
from ...core.logsetup import get_logger
from ...core.parsers import (DEFAULT_COMMENT_URL_HINTS, DEFAULT_REEL_URL_HINTS,
                      looks_like_comment_response, looks_like_reel_response,
                      media_pk_codes, parse_comments, parse_reels)

log = get_logger(__name__)

REELS_URL = "https://www.instagram.com/reels/"
TAG_URL = "https://www.instagram.com/explore/tags/{tag}/"
ACCOUNT_REELS_URL = "https://www.instagram.com/{account}/reels/"
REEL_PERMALINK = "https://www.instagram.com/reel/{code}/"

# Attribution: the comment endpoint names its media by numeric pk in the REST
# path or the GraphQL `media_id` variable; the opened permalink names it by
# shortcode in the page URL. NEVER attribute by "whatever reel is active" —
# that is how leads end up under the wrong reel/author.
_MEDIA_PK_IN_URL = re.compile(r"/media/(\d+)/comments")
_MEDIA_ID_IN_POST = re.compile(r'"media_id"\s*:\s*"?(\d+)')
# Shortcodes are ≥5 chars in practice; the bound keeps bare section paths
# (e.g. /reels/, /p/) and short locale segments from matching.
_CODE_IN_PAGE_URL = re.compile(r"/(?:reel|reels|p)/([A-Za-z0-9_-]{5,})(?:/|$|\?)")


@dataclass
class CDPConfig(CDPBaseConfig):
    reel_url_hints: tuple[str, ...] = DEFAULT_REEL_URL_HINTS
    comment_url_hints: tuple[str, ...] = DEFAULT_COMMENT_URL_HINTS


class CDPFeed(CDPFeedBase):
    def __init__(self, cfg: Optional[CDPConfig] = None):
        super().__init__(cfg or CDPConfig())
        self._pk_to_code: dict[str, str] = {}   # media pk/id → shortcode

    # ---- interception hooks ----
    def _url_hints(self) -> tuple[str, ...]:
        return tuple(self.cfg.reel_url_hints) + tuple(self.cfg.comment_url_hints)

    def _classify(self, url: str, body, response) -> None:
        # Classification is EXCLUSIVE. A media-bearing payload (feed batch,
        # permalink preload) also contains comment-shaped nodes — captions and
        # preview comments of OTHER reels — so reading comments out of it
        # attributes leads to the wrong reel/author. Comments are only read
        # from media-free payloads (the dedicated comments endpoint).
        if looks_like_reel_response(url, body):
            self._pk_to_code.update(media_pk_codes(body))
            for reel in parse_reels(body):
                self._enqueue_reel(reel)
            return

        if looks_like_comment_response(url, body):
            rid = self._attribute_reel(response)
            if rid is None:
                return  # cannot positively identify the media — drop, never guess
            comments, cursor = parse_comments(body)
            if comments:
                bucket = self._comments_by_reel.setdefault(rid, [])
                known = {c.comment_id for c in bucket}
                bucket.extend(c for c in comments if c.comment_id not in known)
                self._comment_cursor_by_reel[rid] = cursor

    def _attribute_reel(self, response) -> Optional[str]:
        """The shortcode of the reel a comment response belongs to, from the
        response itself — the request's media pk, else the URL of the page the
        response was served to. Returns None when neither identifies a reel."""
        pk: Optional[str] = None
        m = _MEDIA_PK_IN_URL.search(response.url)
        if m:
            pk = m.group(1)
        if pk is None:
            try:
                post = unquote_plus(response.request.post_data or "")
            except Exception:
                post = ""
            m = _MEDIA_ID_IN_POST.search(post)
            if m:
                pk = m.group(1)
        if pk is not None and pk in self._pk_to_code:
            return self._pk_to_code[pk]
        try:
            page_url = response.frame.page.url
        except Exception:
            page_url = ""
        m = _CODE_IN_PAGE_URL.search(page_url)
        return m.group(1) if m else None

    # ---- discovery sources ----
    def _sources(self) -> list[str]:
        """URLs to walk this session: home feed + campaign-defined hashtags +
        seed accounts' reels. Same interception path applies to every source."""
        urls: list[str] = []
        if self.cfg.include_home_feed:
            urls.append(REELS_URL)
        for tag in self.cfg.seed_hashtags:
            urls.append(TAG_URL.format(tag=str(tag).lstrip("#")))
        for acct in self.cfg.seed_accounts:
            urls.append(ACCOUNT_REELS_URL.format(account=str(acct).lstrip("@")))
        return urls or [REELS_URL]

    def _source_redirected(self, requested_url: str, landed_url: str) -> bool:
        """Instagram sometimes 302-redirects a tag/reels source to its newer
        keyword-search UI (``/explore/search/keyword/?q=%23<tag>``), which renders
        essentially empty (no reels grid). Detect that landing and skip the source
        fast. Only treat it as a redirect when we did NOT request a search URL — a
        genuinely-requested search page should keep normal behavior."""
        if "/explore/search/" in requested_url:
            return False
        return "/explore/search/" in (landed_url or "")

    # ---- open full-screen ----
    def open_reel(self, reel: Reel) -> bool:
        """Open the reel full-screen via its permalink so comments + Like/Follow
        controls exist (they don't on hashtag/account GRID pages). Read-only
        navigation — never crafts an API call.

        Returns False when the permalink does not actually resolve to the reel
        (deleted/unavailable) — proceeding on such a page would scrape whatever
        Instagram shows instead and attribute it to this reel."""
        page = self._ensure_ipage()
        if page is None:
            return False
        target = REEL_PERMALINK.format(code=reel.reel_id)
        try:
            # Already there (e.g. capture_frames opened it for the vision
            # gate) → don't pay a second navigation.
            if not str(page.url).startswith(target.rstrip("/")):
                page.goto(target, wait_until="domcontentloaded",
                          timeout=self.cfg.nav_timeout_ms)
                time.sleep(self.cfg.nav_settle_seconds)
        except PlaywrightTimeout:
            # A hung permalink nav must skip this reel, not wedge the run.
            log.warning("CDP open_reel nav timed out · reel=%s", reel.reel_id)
            return False
        except Exception:
            return False
        # A reel discovered on a swipeable /reels/ surface: opening its /reel/<code>/
        # permalink can bounce back INTO the swipeable feed, whose URL drops the code.
        # Reading comments there would hang on that busy SPA (root-caused live 2026-07-06)
        # and attribute an ADJACENT reel's comments to this one. page.url is a cached
        # property (no protocol call — it can't wedge), so this check is a safe short-circuit
        # BEFORE the blocking _page_unavailable evaluate: if the landed URL no longer names
        # this reel, skip it. Conservative — it only fires when the code is absent, so a
        # valid single-reel view (which keeps the code) is never falsely skipped.
        if reel.reel_id not in str(page.url):
            log.warning("CDP open_reel bounced off the permalink (swipeable surface?) · "
                        "reel=%s landed=%s", reel.reel_id, page.url)
            return False
        return not self._page_unavailable(page)

    @staticmethod
    def _page_unavailable(page) -> bool:
        """True when Instagram is showing its not-available / not-found screen."""
        try:
            return bool(page.evaluate(
                """() => /page isn't available|page not found|link you followed may be broken/i
                     .test(document.body ? document.body.innerText : '')"""))
        except PlaywrightTimeout:
            log.warning("CDP _page_unavailable check timed out — treating as available")
            return False
        except Exception:
            return False

    # ---- comments ----
    def fetch_comments(self, reel_id: str, since_cursor: Optional[str]
                       ) -> tuple[list, Optional[str]]:
        # Reset the canary; if no comment response arrives, healthy() flips and
        # the Session's empty-interception canary can halt the run.
        self._saw_data = False
        self._open_comments_and_paginate()

        all_comments = self._comments_by_reel.get(reel_id, [])
        # Count watermark = robust "new since last poll" that survives re-scrapes.
        start = int(since_cursor) if (since_cursor and since_cursor.isdigit()) else 0
        new = all_comments[start:]
        log.debug("CDP fetched comments · reel=%s new=%d total=%d",
                  reel_id, len(new), len(all_comments))
        return new, str(len(all_comments))

    def _open_comments_and_paginate(self) -> None:
        """Open the comment view of the centered reel (a read action) and scroll
        the comment dialog so the comment endpoint paginates. Selectors are the
        likeliest DevTools-confirmation point if Instagram's DOM changes.

        On the reels surface the comment icon's click handler lives on a
        clickable *ancestor* of the SVG, not the SVG itself, and the comment
        list scrolls inside a dialog container — not the page. Both confirmed
        against live Instagram; the comment payload arrives as
        `xdt_api__v1__media__media_id__comments__connection` and is parsed by
        shape, so a renamed endpoint still works (PRD §13)."""
        page = self._ipage
        if page is None:
            return  # no reel opened (open_reel not called / failed) → nothing to read
        self._open_comment_dialog(page)
        for _ in range(self.cfg.max_comment_scrolls):
            if not self._scroll_comment_dialog(page):
                self._scroll(page)  # fallback: inline comments, scroll the page
            time.sleep(self.cfg.settle_seconds)

    def _open_comment_dialog(self, page) -> None:
        """Click the clickable ancestor of the comment icon by its centre point."""
        try:
            box = page.evaluate(
                """() => {
                  const svg = document.querySelector(
                    "svg[aria-label='Comment'], svg[aria-label='Comments']");
                  if (!svg) return null;
                  const btn = svg.closest(
                    "[role=button], button, a[role=link], div[tabindex='0']")
                    || svg.parentElement;
                  const r = btn.getBoundingClientRect();
                  if (r.width === 0 || r.height === 0) return null;
                  return {x: r.x + r.width / 2, y: r.y + r.height / 2};
                }""")
            if box:
                page.mouse.click(box["x"], box["y"])
                time.sleep(self.cfg.settle_seconds)
        except PlaywrightTimeout:
            # Opening the dialog is best-effort — comments may already be inline and
            # interception still fires. A timeout here must not wedge the poll.
            log.warning("CDP open comment dialog timed out — skipping")
        except Exception:
            pass  # comments may already be inline; interception still fires

    def _scroll_comment_dialog(self, page) -> bool:
        """Scroll the tallest scrollable container inside the open dialog.

        Returns True if a dialog scroll container was found and scrolled."""
        try:
            return bool(page.evaluate(
                """() => {
                  const dlg = document.querySelector("div[role=dialog]");
                  if (!dlg) return false;
                  const scrollers = [...dlg.querySelectorAll('*')].filter(e =>
                    e.scrollHeight > e.clientHeight + 40 &&
                    /auto|scroll/.test(getComputedStyle(e).overflowY));
                  const t = scrollers.sort(
                    (a, b) => b.scrollHeight - a.scrollHeight)[0];
                  if (!t) return false;
                  t.scrollTop = t.scrollHeight;
                  return true;
                }"""))
        except PlaywrightTimeout:
            log.warning("CDP comment-dialog scroll timed out — falling back")
            return False
        except Exception:
            return False

    # ---- engagement (opt-in; Session enforces caps + halts on action-block) ----
    def like_reel(self, reel: Reel) -> bool:
        """Like the opened reel via its Like control; skip if already liked.
        Requires open_reel() first (controls only exist on the full-screen view)."""
        # The clickable ancestor of the Like SVG (skip when it reads 'Unlike').
        return self._click_centermost(
            self._ipage,
            """() => [...document.querySelectorAll("svg[aria-label='Like']")]
                 .map(s => s.closest("[role=button], button, div[tabindex='0']") || s)""")

    def follow_author(self, reel: Reel) -> bool:
        """Click the Follow button on the opened reel; skip if already following."""
        return self._click_centermost(
            self._ipage,
            """() => [...document.querySelectorAll("button, [role=button]")]
                 .filter(b => /^follow$/i.test((b.textContent || '').trim()))""")

    def save_reel(self, reel: Reel) -> bool:
        """Save/bookmark the opened reel via its Save control; skip if already
        saved. Requires open_reel() first (controls only exist on the full-screen
        view). Mirrors like_reel: a single centred click on the Save SVG's
        clickable ancestor. Low risk — no social footprint, no recipient.

        TODO(live-DOM): re-verify the Save/Add-to-Favorites aria-labels against
        live Instagram before launch; aria-label text drifts across IG versions
        and there is no observable success signal (PRD §4.3)."""
        return self._click_centermost(
            self._ipage,
            """() => [...document.querySelectorAll(
                   "svg[aria-label='Save'], svg[aria-label='Add to Favorites']")]
                 .map(s => s.closest("[role=button], button, div[tabindex='0']") || s)""")

    def share_reel(self, reel: Reel, target: Optional[str] = None) -> bool:
        """Share the opened reel via DM. Recipient is resolved at call time:
        if ``target`` (an IG handle) is set, search+pick that username; else pick
        the FIRST contact in IG's native share list (positional fallback, per
        O-share-target). Requires open_reel() first.

        Returns True only when Send was clicked and no action-block toast
        followed — there is NO interception confirmation for a DM send, so a
        clean click + clean detect_action_block() is the only success signal.

        TODO(live-DOM): every selector below is DOM-only with no interception
        fallback and is the likeliest break point on an IG redesign — re-verify
        the Share SVG, the recipient-search input, the contact option, and the
        Send button against live Instagram before launch (PRD §4.3, VERY HIGH
        fragility)."""
        page = self._ipage
        if page is None:
            return False
        try:
            # Step 1 — open the share sheet.
            if not self._click_centermost(
                    page,
                    """() => [...document.querySelectorAll("svg[aria-label='Share']")]
                         .map(s => s.closest("[role=button], button, div[tabindex='0']") || s)"""):
                return False
            time.sleep(self.cfg.settle_seconds)
            # Step 2 — resolve the recipient.
            if target:
                box = page.query_selector(
                    "input[placeholder*='Search'], input[type='text']")
                if not box:
                    return False                # can't reach the named recipient
                self._human_pause()
                box.type(target)
                time.sleep(self.cfg.settle_seconds)
            # First contact (positional) when no target, or the searched hit.
            recipient = page.query_selector(
                "[role=option], [role=button][tabindex='0']")
            if not recipient:
                return False
            self._human_pause()
            recipient.click()
            time.sleep(self.cfg.settle_seconds)
            # Step 3 — send.
            send = page.query_selector(
                "button:has-text('Send'), [role=button]:has-text('Send')")
            if not send:
                return False
            self._human_pause()
            send.click()
            time.sleep(self.cfg.settle_seconds)
            # No send confirmation exists; success = clean click + no block toast.
            return not self.detect_action_block()
        except PlaywrightTimeout:
            # Any frozen step in the DM-share flow → treat the share as failed, never
            # hang. The Session logs the failed action; no send is confirmed anyway.
            log.warning("CDP share_reel timed out · reel=%s", reel.reel_id)
            return False
        except Exception:
            return False

    def detect_action_block(self) -> bool:
        """True if Instagram is showing an action-block / try-again-later screen.

        Two surfaces share this detector: the feed-action block (like/save/follow)
        and the DM-share block, which IG rate-limits independently (PRD §4.3).

        TODO(O-dm-regex): the DM-share block surfaces with its own copy
        (a "couldn't send / spam" style toast) that we have NOT yet sampled live.
        Add those phrases to the `_DM_SHARE_BLOCK` alternation below once a real
        DM-block screen is captured — do NOT guess the wording (a wrong phrase
        either never fires or false-halts every share). The structural hook is
        wired now so landing the regex later is a one-line, test-covered change."""
        page = self._ipage or self._page
        if page is None:
            return False
        # Feed-action block phrases (live-verified). The DM-share alternation is
        # intentionally empty until O-dm-regex lands its live-sampled phrases;
        # `(?:)` matches nothing, so it is an inert structural placeholder.
        _DM_SHARE_BLOCK = ""  # TODO(O-dm-regex): e.g. "couldn't send|spam" once sampled
        feed_block = ("action blocked|try again later|"
                      "we restrict certain activity|temporarily blocked")
        pattern = feed_block + (("|" + _DM_SHARE_BLOCK) if _DM_SHARE_BLOCK else "")
        try:
            return bool(page.evaluate(
                """(re) => {
                  const t = (document.body.innerText || '').toLowerCase();
                  return new RegExp(re).test(t);
                }""", pattern))
        except PlaywrightTimeout:
            # Fail-OPEN (return False = "no block"): a timed-out probe must not
            # false-halt the run, and it must not wedge it either. Safe per PRD.
            log.warning("CDP detect_action_block probe timed out — assuming no block")
            return False
        except Exception:
            return False
