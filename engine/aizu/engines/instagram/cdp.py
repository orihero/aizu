"""CDPFeed — Instagram's Playwright-over-CDP feed (PRD §3, §13).

The generic CDP plumbing (attach, interception template, scroll, screenshots,
canary, seed-source walk) lives in ``aizu.core.cdp.CDPFeedBase``; this module
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

import os
import re
import tempfile
import time
from dataclasses import dataclass
from typing import Optional
from urllib.parse import unquote_plus

from ...core.cdp import CDPBaseConfig, CDPFeedBase, PlaywrightTimeout
from ...core.feed import Reel
from ...core.logsetup import get_logger
from ...core.media import (MediaWorkdir, download_media, extract_frames,
                           frames_to_base64, sample_frames)
from ...core.parsers import (DEFAULT_COMMENT_URL_HINTS, DEFAULT_REEL_URL_HINTS,
                      looks_like_comment_response, looks_like_reel_response,
                      media_pk_codes, media_urls_by_pk, parse_comments, parse_reels)
from ...core.transcribe import extract_audio_wav

log = get_logger(__name__)


def _env_float(name: str, default: float) -> float:
    """A positive float env var; malformed/absent/<=0 → default."""
    try:
        v = float(os.environ.get(name, "").strip() or default)
    except ValueError:
        return default
    return v if v > 0 else default


def _env_int(name: str, default: int) -> int:
    """A positive int env var; malformed/absent/<=0 → default."""
    try:
        v = int(os.environ.get(name, "").strip() or default)
    except ValueError:
        return default
    return v if v > 0 else default

# Sanity floor on a re-fetched media response before paying for an ffmpeg
# subprocess call — rejects obviously-too-small bodies (an error page, an
# empty/expired-signature response, a redirect stub) cheaply. Not a proof of
# a valid video; ffmpeg still gets the final say via extract_audio_wav().
MIN_MEDIA_BYTES = 20_000

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

# Mid-run login/challenge detection (walk()'s _login_wall_reason hook): Instagram
# booting the CDP tab to either of these means the account's session cookie
# expired or Instagram is challenging it — nothing left to harvest, so the whole
# session halts rather than skip-and-continue (core/cdp.py CDPFeedBase.walk()).
_LOGIN_URL_MARKER = "/accounts/login/"
_CHALLENGE_URL_MARKER = "/challenge/"


@dataclass
class CDPConfig(CDPBaseConfig):
    reel_url_hints: tuple[str, ...] = DEFAULT_REEL_URL_HINTS
    comment_url_hints: tuple[str, ...] = DEFAULT_COMMENT_URL_HINTS


class CDPFeed(CDPFeedBase):
    def __init__(self, cfg: Optional[CDPConfig] = None):
        super().__init__(cfg or CDPConfig())
        self._pk_to_code: dict[str, str] = {}   # media pk/id → shortcode
        self._pk_to_media_url: dict[str, str] = {}   # media pk/id → best-effort CDN video url
        # code (== Reel.reel_id) → CDN video url, for capture_audio below. Built
        # alongside _pk_to_code/_pk_to_media_url in _classify since both are keyed
        # by pk and Reel.reel_id is the shortcode, not the pk.
        self._code_to_media_url: dict[str, str] = {}
        # Gap 3: capture_audio (STT tier) and capture_video_frames (video-analysis
        # tier) both need the same reel's CDN mp4 — without a cache each downloads
        # + never shares it, so one reel pays the download+ffmpeg-adjacent network
        # cost twice. This memoizes the mp4 for whichever reel was most recently
        # requested (both tiers fire back-to-back, synchronously, for the SAME reel
        # inside one gate_reel() call — never concurrently, so a plain memo keyed by
        # reel_id is sufficient; see cascade.py). Holds exactly one reel's workdir at
        # a time: requesting a DIFFERENT reel_id evicts+cleans the previous one, so
        # nothing accumulates across a session's reel walk. Final sweep on close().
        self._media_cache_reel_id: Optional[str] = None
        self._media_cache_workdir: Optional[MediaWorkdir] = None
        self._media_cache_mp4: Optional[str] = None  # None = download failed for this reel

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
            codes = media_pk_codes(body)
            urls = media_urls_by_pk(body)   # NEW — STT source-clip lookup (unverified live, see capture_audio)
            self._pk_to_code.update(codes)
            self._pk_to_media_url.update(urls)
            for pk, code in codes.items():
                u = urls.get(pk)
                if u:
                    self._code_to_media_url[code] = u
            for reel in parse_reels(body):
                self._enqueue_reel(reel)
            return

        if looks_like_comment_response(url, body):
            rid = self._attribute_reel(response)
            if rid is None:
                return  # cannot positively identify the media — drop, never guess
            comments, cursor = parse_comments(body)
            if comments:
                # Locked: this runs on the Playwright OWNER thread while
                # fetch_comments reads on the caller's (core/cdp.py's
                # _queue_lock). No nesting — the reel branch above already
                # returned, so _enqueue_reel's own acquisition is unreachable
                # from here.
                with self._queue_lock:
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

    # ---- media (STT + video-analysis source; Instagram-only, gated in cascade.py) ----
    def _reel_media_request(self, reel: Reel):
        """(url, headers) to re-fetch the reel's CDN mp4 — cookies/UA copied off
        the live Playwright context — or None when no CDN url is known / not
        attached. The single Instagram-specific bit both media captures share."""
        url = self._code_to_media_url.get(reel.reel_id)
        if not url or self._page is None:
            return None
        try:
            # context.cookies() is a CDP round-trip with no timeout= argument of
            # its own — _call_bounded gives it the same real deadline as the
            # mouse.*/evaluate call sites core/cdp.py wraps, so a frozen CDP pipe
            # degrades to "no cookies" instead of hanging this capture forever.
            cookies = self._call_bounded(lambda: self._page.context.cookies())
            cookie_header = "; ".join(f"{c['name']}={c['value']}" for c in cookies)
        except Exception:  # noqa: BLE001 — a cookieless GET may still work; try anyway
            cookie_header = ""
        try:
            user_agent = self._page.evaluate("navigator.userAgent")
        except Exception:  # noqa: BLE001 — best-effort; a missing UA is not fatal
            user_agent = ""
        return url, {
            "cookies": cookie_header,
            "user_agent": user_agent or "",
            "referer": "https://www.instagram.com/",
        }

    def _download_reel_mp4(self, reel: Reel, dest_path: str) -> bool:
        """Download the reel's CDN mp4 into ``dest_path`` via core.media. Never
        raises. UNVERIFIED against live Instagram (see the caveat below)."""
        req = self._reel_media_request(reel)
        if req is None:
            return False
        url, hdrs = req
        return download_media(
            url, dest_path=dest_path, cookies=hdrs["cookies"],
            user_agent=hdrs["user_agent"], referer=hdrs["referer"],
            timeout_s=self.cfg.js_timeout_ms / 1000, min_bytes=MIN_MEDIA_BYTES)

    def _clear_media_cache(self) -> None:
        """Evict+clean whatever reel's mp4 is currently cached (if any)."""
        if self._media_cache_workdir is not None:
            self._media_cache_workdir.__exit__(None, None, None)
        self._media_cache_workdir = None
        self._media_cache_mp4 = None
        self._media_cache_reel_id = None

    def _get_reel_mp4(self, reel: Reel) -> Optional[str]:
        """The reel's CDN mp4, downloaded at most ONCE per reel and shared by both
        capture_audio (STT) and capture_video_frames (video-analysis) — Gap 3: the
        two tiers used to independently download+ffmpeg the same reel's mp4. A
        different reel_id than what's cached evicts the previous entry first, so
        the cache never holds more than one reel's temp file at a time. Returns
        None when the download failed (also cached, so a second tier in the same
        gate_reel() call doesn't retry a doomed fetch)."""
        if reel.reel_id != self._media_cache_reel_id:
            self._clear_media_cache()
            self._media_cache_reel_id = reel.reel_id
            wd = MediaWorkdir()
            wd.__enter__()
            self._media_cache_workdir = wd
            mp4_path = os.path.join(wd.path, "video.mp4")
            self._media_cache_mp4 = (
                mp4_path if self._download_reel_mp4(reel, mp4_path) else None)
        return self._media_cache_mp4

    def close(self) -> None:
        """Sweep the mp4 cache (if any) in addition to the base attach/driver
        teardown, so a cached reel's temp file never outlives the session."""
        try:
            super().close()
        finally:
            self._clear_media_cache()

    def capture_audio(self, reel: Reel) -> Optional[str]:
        """Download the reel's CDN mp4 and extract a 16kHz mono WAV via ffmpeg for
        the STT tier. Overrides the FeedSource default (core/feed.py); not wired for
        any non-Instagram engine. Never raises — returns None on ANY failure, same
        never-wedge contract as every other capture_* method in this file. The
        returned WAV lives OUTSIDE the temp workdir so the caller
        (Session._transcribe_reel) owns its cleanup, as before.

        UNVERIFIED against live Instagram (plan's "UNCERTAIN / MUST VERIFY AT
        RUNTIME" #1-4, #7 — could not be checked without a live warmed session):
        whether `video_versions[].url` is actually populated in what Instagram
        currently serves; whether that url is signed/short-lived; whether Instagram
        serves reel video as one progressive file or fragmented segments (a single
        GET only decodes cleanly for the progressive case); and whether a
        script-driven CDN GET trips bot detection. Every one degrades to a soft
        None here, never a crash.

        The mp4 itself comes from `_get_reel_mp4`, shared with capture_video_frames
        (Gap 3) — this method no longer owns its own download/workdir; only the WAV
        extraction below is private to this call."""
        wav_path: Optional[str] = None
        ok = False
        try:
            mp4_path = self._get_reel_mp4(reel)
            if not mp4_path:
                return None
            # WAV must outlive the (cached) mp4's workdir (caller cleans it up), so
            # put it in a NamedTemporaryFile rather than alongside the mp4.
            with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as f:
                wav_path = f.name
            ok = extract_audio_wav(mp4_path, wav_path)
            return wav_path if ok else None
        except Exception as e:  # noqa: BLE001 — unverified acquisition path (see above)
            log.debug("CDP capture_audio failed · reel=%s err=%s", reel.reel_id, e)
            return None
        finally:
            if wav_path and not ok:
                try:
                    os.unlink(wav_path)
                except OSError:
                    pass

    def capture_video_frames(self, reel: Reel) -> list[str]:
        """Extract frames at ~1fps from the reel's CDN mp4, evenly sample up to N
        with timestamps, and return them base64-encoded (setting
        ``reel.frame_timestamps`` in parallel) for the video-analysis / fusion tier.

        The mp4 comes from `_get_reel_mp4`, shared with capture_audio (Gap 3) — a
        reel already downloaded for the STT tier is NOT re-fetched here. Shares the
        same UNVERIFIED-live caveat as capture_audio. Never raises — returns [] on
        ANY failure. Extracted PNGs live under their OWN short-lived MediaWorkdir
        (separate from the cached mp4's, which may outlive this call) and are swept
        on exit; the base64 frames are held in memory, so nothing is left for the
        caller to clean up."""
        try:
            mp4_path = self._get_reel_mp4(reel)
            if not mp4_path:
                return []
            with MediaWorkdir() as frames_wd:
                fps = _env_float("AIZU_VIDEO_FRAME_FPS", 1.0)
                max_frames = _env_int("AIZU_VIDEO_FRAMES", 8)
                max_seconds = _env_float("AIZU_VIDEO_MAX_SECONDS", 60.0)
                paths = extract_frames(mp4_path, frames_wd.path, fps=fps,
                                       max_seconds=max_seconds)
                sampled = sample_frames(paths, fps, max_frames=max_frames)
                b64 = frames_to_base64(sampled)
                reel.frame_timestamps = [f.time_ms for f in sampled][:len(b64)]
                return b64
        except Exception as e:  # noqa: BLE001 — unverified acquisition path
            log.debug("CDP capture_video_frames failed · reel=%s err=%s",
                      reel.reel_id, e)
            return []

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

    def _login_wall_reason(self, landed_url: str):
        """Instagram's own login/challenge signature (core/cdp.py's walk() calls
        this after every navigation). A session cookie that expires or a
        challenge triggered mid-harvest 302s the tab to one of these — there is
        no reels grid to recover from, so the whole session halts (PRD §9 tier
        3), instead of the pre-fix silent hang on the next mouse/evaluate call."""
        url = landed_url or ""
        if _LOGIN_URL_MARKER in url:
            return "instagram_login_required", "login"
        if _CHALLENGE_URL_MARKER in url:
            return "instagram_challenge_required", "checkpoint"
        return None

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

        # SNAPSHOT under the lock, then slice the copy. The returned comments and
        # the persisted watermark MUST describe the same list: interception now
        # runs on the Playwright owner thread, so reading the live list would let
        # it grow between `all_comments[start:]` and `len(all_comments)` and
        # persist a cursor past comments this call never returned — those are
        # never scored and never re-read on a later poll (silent lead loss).
        with self._queue_lock:
            all_comments = list(self._comments_by_reel.get(reel_id, []))
        total = len(all_comments)
        # Count watermark = robust "new since last poll" that survives re-scrapes.
        start = int(since_cursor) if (since_cursor and since_cursor.isdigit()) else 0
        new = all_comments[start:]
        log.debug("CDP fetched comments · reel=%s new=%d total=%d",
                  reel_id, len(new), total)
        return new, str(total)

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
