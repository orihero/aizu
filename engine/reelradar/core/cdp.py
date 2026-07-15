"""Shared CDP harness — Playwright-over-CDP attach + interception plumbing.

Three platforms read their site's own internal JSON traffic over CDP — Instagram,
X, and LinkedIn (PRD §3, §5 of each). The generic mechanics are identical and live
here so each engine writes only what is platform-specific; engines still "share
only ``reelradar.core``" (the locked per-engine architecture), and ``core`` is that
shared layer.

This base owns the parts that never differ by platform:

  - attach to a warmed, logged-in Chrome over CDP (never launch a fresh browser),
  - listen to the page's own responses, hint-filter + JSON-guard them, set the
    empty-interception canary, and delegate the actual routing to ``_classify``,
  - drive human-like scrolling and walk campaign-defined seed sources,
  - screenshot a frame for the vision/OCR tier,
  - back the empty-interception canary via ``healthy()``.

A subclass supplies only: ``_url_hints()`` (cheap pre-filter substrings),
``_classify(url, body, response)`` (shape-based routing into the queues),
``_sources()`` (discovery URLs to walk), ``open_reel()`` (open one item full-screen
so its comments load), and ``fetch_comments()``.

URL hints are a cheap pre-filter only — detection is by response *shape* in each
engine's pure ``parsers`` module, so a drifted endpoint still parses. Confirm the
current endpoints in DevTools once and expect drift (every CDP PRD §12/§13).
"""
from __future__ import annotations

import base64
import random
import time
from dataclasses import dataclass
from typing import Callable, Iterator, Optional

from .feed import Comment, FeedSource, Reel
from .logsetup import get_logger

log = get_logger(__name__)

try:
    from playwright.sync_api import sync_playwright
    from playwright.sync_api import TimeoutError as PlaywrightTimeout
    PLAYWRIGHT_AVAILABLE = True
except Exception:  # pragma: no cover - playwright optional at import time
    sync_playwright = None  # type: ignore
    PLAYWRIGHT_AVAILABLE = False

    class PlaywrightTimeout(Exception):  # type: ignore
        """Fallback when Playwright is not importable. The real
        ``playwright.sync_api.TimeoutError`` subclasses ``Exception``, so an
        explicit ``except PlaywrightTimeout`` behaves identically to the generic
        ``except Exception`` fallback that already guards these blocks — the
        named catch is load-bearing for LOG VISIBILITY (a silent CDP hang was the
        confirmed root cause of a wedged harvest) and for future-proofing."""


@dataclass
class CDPBaseConfig:
    """Platform-agnostic CDP knobs. Engine configs subclass this to add their own
    URL hints (and any platform-specific tuning)."""
    cdp_url: str = "http://127.0.0.1:9222"
    scroll_delta: int = 900
    settle_seconds: float = 1.5          # wait for interception after a scroll
    empty_scrolls_before_stop: int = 4   # no new items after N scrolls => source end
    max_comment_scrolls: int = 3         # paginate the comment list this many times
    # Multi-source discovery (works on any account, not just a warmed feed).
    seed_hashtags: tuple[str, ...] = ()
    seed_accounts: tuple[str, ...] = ()
    include_home_feed: bool = True
    per_source_reels: int = 12           # items to pull from each source before moving on
    nav_settle_seconds: float = 2.5      # wait after navigating to a new source
    nav_timeout_ms: int = 20000          # cap page.goto so a hung nav can't wedge a source
    # Per-call Playwright timeouts. GENEROUS defaults tuned for a slow worker PC on
    # real Instagram — a frozen CDP command must degrade, not wedge forever, but a
    # merely-slow one must not false-timeout. js_timeout_ms is the PAGE default: it
    # governs evaluate/query_selector/screenshot AND mouse.wheel/mouse.click (mouse
    # ops accept NO timeout= arg, so they rely solely on this page default — which is
    # why attach()/_ensure_ipage() must call set_default_timeout with it).
    js_timeout_ms: int = 15000           # page-default cap: evaluate/screenshot/mouse
    per_reel_seconds: float = 90.0       # session-level per-reel wall-clock backstop
    # Hard per-source wall-clock ceiling: no single source can hold the walk longer
    # than this, regardless of scroll/interception behavior (the anti-wedge guarantee).
    max_source_seconds: float = 45.0
    # Engagement timing (human-like pause before a click). Read-only engines unused.
    action_delay_min: float = 0.8
    action_delay_max: float = 2.5


class CDPFeedBase(FeedSource):
    """Template-method base for a Playwright-over-CDP feed.

    Owns the browser lifecycle, the response-interception buffer, the seed-source
    walk, screenshots, and the canary. Subclasses implement the platform-specific
    hooks (``_url_hints``/``_classify``/``_sources``/``open_reel``/``fetch_comments``).
    """

    def __init__(self, cfg: Optional[CDPBaseConfig] = None,
                 clock: Callable[[], float] = time.monotonic):
        self.cfg = cfg or CDPBaseConfig()
        # Monotonic source of truth for the per-source budget; injectable so tests
        # can drive the wall-clock cap deterministically without real sleeping.
        self._clock = clock
        self._pw = None
        self._browser = None
        self._page = None
        # Discovery scrolls self._page; per-item interaction (comments, frames)
        # happens full-screen on a dedicated interaction page so it does not
        # disturb the discovery scroll position.
        self._ipage = None

        # Interception accumulators (subclass `_classify` fills these).
        self._reel_queue: list[Reel] = []
        self._seen_reel_ids: set[str] = set()
        self._comments_by_reel: dict[str, list[Comment]] = {}
        self._comment_cursor_by_reel: dict[str, Optional[str]] = {}
        # canary: did we parse any hinted response since the last health check?
        self._saw_data = False

    # ---- platform hooks (subclass MUST implement) ----
    def _url_hints(self) -> tuple[str, ...]:
        """URL substrings that hint a response is worth parsing (cheap pre-filter)."""
        raise NotImplementedError

    def _classify(self, url: str, body, response) -> None:
        """Route one intercepted (already JSON-parsed) response into the queues."""
        raise NotImplementedError

    def _sources(self) -> list[str]:
        """URLs to walk this session (home/feed + seed hashtags + seed accounts)."""
        raise NotImplementedError

    def _source_redirected(self, requested_url: str, landed_url: str) -> bool:
        """True when a source landed somewhere with no reels to harvest (e.g. IG's
        keyword-search fallback), so the walk should skip it FAST instead of
        burning the whole empty-scroll budget. Base default: never redirected;
        engines override with their platform-specific fallback signature."""
        return False

    # ---- attach / lifecycle ----
    def attach(self) -> None:
        if not PLAYWRIGHT_AVAILABLE:
            raise RuntimeError(
                "Playwright is not installed. `pip install playwright` (the "
                "engine attaches over CDP; it does not download a browser).")
        log.info("CDP attaching to warmed Chrome · url=%s", self.cfg.cdp_url)
        self._pw = sync_playwright().start()
        # Attach to the already-running, warmed Chrome. Never launch vanilla.
        # no_defaults=True makes Playwright SKIP its on-connect Browser.setDownloadBehavior
        # call (driver: acceptDownloads→"internal-browser-default"). That single CDP command
        # is what Chrome for Testing 148 rejects — "Browser context management is not
        # supported" — once the browser has been up a few minutes / a couple of
        # connect+disconnect cycles, killing every attach. We never download anything (this
        # is a read-only observer), so skipping it is free and sidesteps the failure entirely
        # — no kill/relaunch dance, works even against an already-"degraded" instance.
        # Verified live 2026-07-01. See microsoft/playwright#30383.
        self._browser = self._pw.chromium.connect_over_cdp(self.cfg.cdp_url, no_defaults=True)
        contexts = self._browser.contexts
        if not contexts:
            raise RuntimeError("No browser context over CDP — is the warmed "
                               "Chrome running with --remote-debugging-port?")
        ctx = contexts[0]
        self._page = ctx.pages[0] if ctx.pages else ctx.new_page()
        self._page.on("response", self._on_response)
        # Per-call timeout so a frozen CDP command degrades instead of blocking the
        # main greenlet forever (the confirmed silent-hang root cause). Applies to
        # every evaluate/query_selector/screenshot AND mouse.* (no timeout= arg).
        self._page.set_default_timeout(self.cfg.js_timeout_ms)
        self._page.set_default_navigation_timeout(self.cfg.nav_timeout_ms)
        log.success("CDP attached · %d context(s), interception wired", len(contexts))
        # walk() drives per-source navigation; don't hard-land on the home feed.

    def _ensure_ipage(self):
        """Lazily open the interaction page (a second tab) and wire interception
        so comment responses on the opened item are captured too."""
        if self._ipage is None and self._browser is not None:
            ctx = self._browser.contexts[0]
            self._ipage = ctx.new_page()
            self._ipage.on("response", self._on_response)
            # CRITICAL: the interaction page is where mouse.wheel/mouse.click run
            # (comment-dialog scroll, engagement clicks), and those take NO timeout=
            # argument — a per-page default is the ONLY cap on them. Without this a
            # frozen wheel/click on the interaction page hangs the run indefinitely.
            self._ipage.set_default_timeout(self.cfg.js_timeout_ms)
            self._ipage.set_default_navigation_timeout(self.cfg.nav_timeout_ms)
        return self._ipage

    def close(self) -> None:
        """Disconnect from the warmed Chrome and release the Playwright driver so the
        NEXT session can attach fresh in the same process. We deliberately do NOT call
        ``browser.close()``: the Chrome was launched and warmed externally
        (warm_chrome.sh) and must outlive the run — ``pw.stop()`` drops only our CDP
        connection and driver, leaving the browser running."""
        try:
            if self._pw is not None:
                self._pw.stop()
        finally:
            self._pw = None
            self._browser = None
            self._page = None
            self._ipage = None

    # ---- interception ----
    def _on_response(self, response) -> None:
        """Template method: hint-filter → JSON-guard → set canary → delegate.

        ``_saw_data`` is set the moment hinted JSON traffic flows, BEFORE
        classification — so an item whose comment section is simply empty does not
        trip the empty-interception canary."""
        try:
            url = response.url
        except Exception:  # noqa: BLE001 — a malformed response must never crash interception
            return
        if not any(h in url for h in self._url_hints()):
            return
        if not self._is_json_like(response):
            return  # a binary/HTML body under a hinted URL is never a data payload
        try:
            body = response.json()
        except Exception:  # not JSON / already consumed — ignore
            return
        self._saw_data = True
        self._classify(url, body, response)

    @staticmethod
    def _is_json_like(response) -> bool:
        """Defensive content-type pre-check before reading a body: only attempt the
        read when the header says JSON or JavaScript (Instagram serves graphql as
        `text/javascript`; comment/reel payloads are `text/javascript` or
        `application/json`). FAIL-OPEN: if the header is absent or unreadable, fall
        through to the parse — this must NOT change what currently classifies, only
        cheaply drop obvious binary/HTML bodies under a hinted URL."""
        try:
            headers = response.headers
        except Exception:  # noqa: BLE001 — no headers surface → let the parse decide
            return True
        if not headers:
            return True
        try:
            ctype = (headers.get("content-type") or headers.get("Content-Type") or "").lower()
        except Exception:  # noqa: BLE001 — not a mapping → let the parse decide
            return True
        if not ctype:
            return True
        return "json" in ctype or "javascript" in ctype

    def _enqueue_reel(self, reel: Reel) -> None:
        """Append a freshly intercepted item, deduped by id (subclass `_classify`)."""
        if reel.reel_id and reel.reel_id not in self._seen_reel_ids:
            self._seen_reel_ids.add(reel.reel_id)
            self._reel_queue.append(reel)

    # ---- discovery walk (across sources) ----
    def walk(self) -> Iterator[Reel]:
        for url in self._sources():
            log.info("CDP walking source · %s", url)
            self._navigate(url)
            # Fast skip: some sources 302 to a page with no reels grid (IG's
            # keyword-search fallback). Detect the landed URL and move on rather
            # than spending the whole empty-scroll budget on an empty page.
            landed = self._landed_url()
            if self._source_redirected(url, landed):
                log.info("CDP source redirected to keyword-search (no reels) · %s", url)
                continue
            source_start = self._clock()
            yielded = 0
            empty_scrolls = 0
            while yielded < self.cfg.per_source_reels:
                if self._reel_queue:
                    reel = self._reel_queue.pop(0)
                    empty_scrolls = 0
                    yielded += 1
                    yield reel
                    continue
                # Hard wall-clock ceiling: no source can wedge the walk (the
                # key anti-wedge guarantee — checked before every empty scroll).
                if self._clock() - source_start > self.cfg.max_source_seconds:
                    log.info("CDP source budget exhausted · %s", url)
                    break
                before = len(self._seen_reel_ids)
                self._scroll()
                time.sleep(self.cfg.settle_seconds)
                if len(self._seen_reel_ids) == before:
                    empty_scrolls += 1
                    if empty_scrolls >= self.cfg.empty_scrolls_before_stop:
                        break  # this source dry → move to the next
            log.debug("CDP source done · %s · yielded=%d", url, yielded)

    def _landed_url(self) -> str:
        """The URL the page actually landed on after `_navigate` (may differ from
        the requested source when the site 302-redirects)."""
        try:
            return str(self._page.url) if self._page is not None else ""
        except Exception:  # noqa: BLE001 — never let a URL read wedge the walk
            return ""

    def _navigate(self, url: str) -> None:
        if self._page is None:
            return
        try:
            self._page.goto(url, wait_until="domcontentloaded",
                            timeout=self.cfg.nav_timeout_ms)
        except Exception as e:  # noqa: BLE001
            log.debug("CDP nav failed · url=%s · %s", url, e)
            pass  # transient nav failure → next source/scroll still works
        time.sleep(self.cfg.nav_settle_seconds)

    def _scroll(self, page=None) -> None:
        page = page or self._page
        if page is None:
            return
        try:
            page.mouse.wheel(0, self.cfg.scroll_delta)
        except PlaywrightTimeout:
            # A frozen wheel is the exact silent-hang we defend against: the page
            # default now caps it, so this fires instead of blocking. Try the JS
            # fallback (also capped by the page default); if THAT times out too,
            # give up on this scroll rather than wedge the walk.
            log.warning("CDP scroll wheel timed out — trying JS fallback")
            try:
                page.evaluate(f"window.scrollBy(0, {self.cfg.scroll_delta})")
            except PlaywrightTimeout:
                log.warning("CDP scroll JS fallback timed out — skipping scroll")
        except Exception:
            # fallback for layouts that capture wheel events
            try:
                page.evaluate(f"window.scrollBy(0, {self.cfg.scroll_delta})")
            except PlaywrightTimeout:
                log.warning("CDP scroll JS fallback timed out — skipping scroll")

    # ---- vision frames ----
    def _shoot(self, page) -> Optional[str]:
        """One base64 JPEG of the page's video frame (or full page fallback)."""
        if page is None:
            return None
        try:
            el = page.query_selector("video")
            raw = (el.screenshot(type="jpeg") if el
                   else page.screenshot(type="jpeg"))
            return base64.b64encode(raw).decode("ascii")
        except PlaywrightTimeout:
            # A frozen screenshot/query_selector must degrade to "no frame" (the
            # vision tier tolerates a None) instead of hanging the run. Explicit
            # catch for log visibility; PlaywrightTimeout subclasses Exception so
            # behavior is unchanged vs. the generic fallback below.
            log.warning("CDP screenshot timed out — no frame captured")
            return None
        except Exception:
            return None

    def capture_frame(self, reel: Reel) -> Optional[str]:
        """Single representative frame (back-compat)."""
        frames = self.capture_frames(reel, n=1)
        return frames[0] if frames else None

    def capture_frames(self, reel: Reel, n: int = 3, interval: float = 0.6) -> list[str]:
        """Up to n frames across the dwell. Frames MUST come from the item being
        judged, so it is opened full-screen on the interaction page first."""
        if not self.open_reel(reel):
            return []
        out: list[str] = []
        for i in range(max(1, n)):
            shot = self._shoot(self._ipage)
            if shot:
                out.append(shot)
            if i < n - 1:
                time.sleep(interval)
        return out

    # ---- engagement click helpers (opt-in; read-only engines leave these unused) ----
    def _human_pause(self) -> None:
        time.sleep(random.uniform(self.cfg.action_delay_min, self.cfg.action_delay_max))

    def _click_centermost(self, page, selector_js: str) -> bool:
        """Click the element matching selector_js whose centre is nearest the
        viewport vertical centre. selector_js is a JS expression returning a
        NodeList-like array of candidate elements."""
        if page is None:
            return False
        try:
            box = page.evaluate(
                """(sel) => {
                  const els = sel();
                  const mid = window.innerHeight / 2;
                  let best = null, bestD = Infinity;
                  for (const e of els) {
                    const r = e.getBoundingClientRect();
                    if (r.width === 0 || r.height === 0) continue;
                    const d = Math.abs((r.y + r.height / 2) - mid);
                    if (d < bestD) { bestD = d; best = r; }
                  }
                  if (!best) return null;
                  return {x: best.x + best.width / 2, y: best.y + best.height / 2};
                }""", selector_js)
        except PlaywrightTimeout:
            log.warning("CDP click locate (evaluate) timed out — skipping click")
            return False
        except Exception:
            return False
        if not box:
            return False
        self._human_pause()
        try:
            page.mouse.click(box["x"], box["y"])
            time.sleep(self.cfg.settle_seconds)
            return True
        except PlaywrightTimeout:
            # mouse.click takes no timeout= arg — the interaction-page default
            # (set in _ensure_ipage) is what makes this fire instead of hanging.
            log.warning("CDP click timed out — treated as no-op")
            return False
        except Exception:
            return False

    # ---- canary ----
    def healthy(self) -> bool:
        return self._saw_data
