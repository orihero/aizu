"""Caller/owner threading-contract audit for the CDP harness.

Two questions, one file:

1.  CAN the leading "lock-order deadlock" hypothesis happen?  The shape is:
    the CALLER holds ``CDPFeedBase._queue_lock`` while submitting a bounded
    call, and the owner thread — which dispatches ``page.on("response")``
    handlers, i.e. ``_on_response`` -> ``_classify`` -> ``_enqueue_reel`` —
    blocks trying to take the SAME lock.  ``test_the_deadlock_shape_...``
    builds that deadlock on purpose and shows it produces the live signature
    exactly.  ``test_no_playwright_call_is_ever_submitted_under_the_queue_lock``
    then proves, mechanically over the real source, that no such site exists.

2.  What DOES produce the signature, then?  Any unbounded work executed ON the
    owner thread inside the in-flight bounded call — and a response handler is
    exactly that: ``PlaywrightOwner.call``'s reentrancy guard (pw_owner.py:267)
    hands every call made from a handler straight through with NO deadline, and
    ``_on_response``'s ``response.json()`` is an unbounded protocol round-trip.
    ``test_an_unbounded_response_handler_...`` reproduces the whole live log
    from that, with no browser: one call that does not return, then two FREE
    microsecond fast-fails behind it, then ``cdp_call_wedged``.
"""
import ast
import pathlib
import threading
import time

import pytest

from aizu.core.cdp import CDPBaseConfig, CDPFeedBase, PlaywrightTimeout
from aizu.core.feed import Reel
from aizu.engines.base import HaltSession

ENGINE_ROOT = pathlib.Path(__file__).resolve().parents[2] / "aizu"


class _Feed(CDPFeedBase):
    """Minimal concrete feed — no browser, no network."""

    def _url_hints(self):
        return ("/graphql",)

    def _classify(self, url, body, response):
        # Mirrors engines/instagram/cdp.py: the queue write happens under the
        # SAME lock walk()/fetch_comments take on the caller's thread.
        self._enqueue_reel(Reel(reel_id=str(body), author="a", caption=""))

    def _sources(self):
        return []

    def open_reel(self, reel):
        return True

    def fetch_comments(self, reel_id, since_cursor):
        return [], None


def _feed(**kw):
    cfg = CDPBaseConfig(js_timeout_ms=kw.pop("js_timeout_ms", 150),
                        max_consecutive_wedged_calls=kw.pop("wedge_limit", 3),
                        **kw)
    f = _Feed(cfg=cfg)
    f.human.cfg.enabled = False      # one wheel notch, no decorative mouse_move
    return f


class _HangingResponse:
    """A hinted JSON response whose body read never comes back.

    Stands in for ``playwright.Response.json()`` -> ``body()`` ->
    ``_channel.send("body")``, which carries NO timeout of any kind
    (site-packages/playwright/_impl/_network.py:881).
    """

    def __init__(self, release: threading.Event, url="https://x/graphql"):
        self.url = url
        self.headers = {"content-type": "application/json"}
        self._release = release
        self.entered = threading.Event()

    def json(self):
        self.entered.set()
        self._release.wait(30)       # generous; the test releases it explicitly
        return "reel-from-a-late-handler"


# --------------------------------------------------------------------------
# 1a. The deadlock SHAPE — built deliberately, to show what it looks like.
# --------------------------------------------------------------------------
def test_the_deadlock_shape_produces_the_live_signature_when_it_exists():
    """Caller holds _queue_lock -> submits -> owner blocks on _queue_lock.

    This is the hypothesis under audit, constructed by hand. It reproduces the
    live log exactly: the bounded call "does not return", the owner stays
    poisoned, and every later call fast-fails. Kept as the reference shape so a
    future edit that DOES hold the lock across a submission is recognisable.
    """
    feed = _feed(js_timeout_ms=150)
    owner = feed._owner
    started = threading.Event()

    def _handler_on_the_owner_thread():
        started.set()
        # This is _on_response -> _classify -> _enqueue_reel.
        feed._enqueue_reel(Reel(reel_id="r1", author="a", caption=""))
        return "ok"

    with feed._queue_lock:                       # <-- the forbidden pattern
        with pytest.raises(PlaywrightTimeout):
            feed._call_bounded(_handler_on_the_owner_thread)
        assert started.is_set(), "the owner did start the closure"
        # The owner is now blocked on a lock this thread holds: wedged.
        assert owner.is_wedged()
        assert owner.wedge_streak == 1 and owner.wedge_total == 1
        # …and every later call fast-fails without ever reaching the browser.
        with pytest.raises(PlaywrightTimeout):
            feed._call_bounded(lambda: "never runs")
        assert owner.wedge_streak == 2 and owner.wedge_total == 1

    # Releasing the lock un-wedges the owner — self-clearing, as designed.
    for _ in range(200):
        if not owner.is_wedged():
            break
        time.sleep(0.01)
    assert not owner.is_wedged()


# --------------------------------------------------------------------------
# 1b. …and the mechanical proof that the shape is ABSENT from the real code.
# --------------------------------------------------------------------------
_SUBMITTING_ROOTS = {
    "_page", "_ipage", "_browser", "_pw",       # OwnedPW proxies: every touch dispatches
    "_call_bounded", "_owner", "human",
    "_scroll", "_wheel_once", "_navigate", "_goto_once", "_shoot",
    "_ensure_ipage", "_landed_url", "_click_centermost", "_open_comment_dialog",
    "_scroll_comment_dialog", "_page_unavailable", "_source_unavailable",
}
_PAGELIKE_NAMES = {"page", "ipage", "self._page", "self._ipage"}

_CDP_SOURCES = [
    ENGINE_ROOT / "core" / "cdp.py",
    ENGINE_ROOT / "engines" / "instagram" / "cdp.py",
    ENGINE_ROOT / "engines" / "x" / "cdp.py",
    ENGINE_ROOT / "engines" / "linkedin" / "cdp.py",
]


def _queue_lock_blocks(tree):
    for node in ast.walk(tree):
        if not isinstance(node, ast.With):
            continue
        for item in node.items:
            ctx = item.context_expr
            if isinstance(ctx, ast.Attribute) and ctx.attr == "_queue_lock":
                yield node


def _reaches_the_owner(node):
    """Names inside `node` that would submit work to the Playwright owner."""
    hits = []
    for n in ast.walk(node):
        if isinstance(n, ast.Attribute):
            if isinstance(n.value, ast.Name) and n.value.id == "self" \
                    and n.attr in _SUBMITTING_ROOTS:
                hits.append(f"self.{n.attr} (line {n.lineno})")
            if isinstance(n.value, ast.Name) and n.value.id in _PAGELIKE_NAMES:
                hits.append(f"{n.value.id}.{n.attr} (line {n.lineno})")
    return hits


@pytest.mark.parametrize("path", _CDP_SOURCES, ids=lambda p: p.name)
def test_no_playwright_call_is_ever_submitted_under_the_queue_lock(path):
    """The deadlock above cannot happen: no `with self._queue_lock:` body in the
    CDP harness touches anything that dispatches to the owner thread.

    This is a REGRESSION GUARD, not decoration — the whole `_queue_lock`
    contract is "hold it for in-memory work only". One `page.` inside one of
    these blocks turns a narrow race guard into a hard cross-thread deadlock
    whose only symptom is `cdp_call_wedged`.
    """
    tree = ast.parse(path.read_text())
    offenders = []
    for block in _queue_lock_blocks(tree):
        hits = _reaches_the_owner(block)
        if hits:
            offenders.append(f"{path.name}:{block.lineno} -> {hits}")
    assert not offenders, (
        "a Playwright call is submitted to the owner thread while the caller "
        "holds _queue_lock — that is a deadlock, not a race: " + "; ".join(offenders))


def test_the_queue_lock_is_the_only_lock_shared_by_caller_and_owner():
    """Inventory guard. Two locks exist in the CDP world — CDPFeedBase's
    `_queue_lock` and PlaywrightOwner's `_lock` (lifecycle only, never held
    across `fn()`). A third shared lock reopens the deadlock question."""
    src = (ENGINE_ROOT / "core" / "cdp.py").read_text()
    assert src.count("threading.Lock()") == 1
    owner_src = (ENGINE_ROOT / "core" / "pw_owner.py").read_text()
    assert owner_src.count("threading.Lock()") == 1
    # PlaywrightOwner._lock is never held while `fn()` runs: _loop takes no lock.
    tree = ast.parse(owner_src)
    loop = next(n for n in ast.walk(tree)
                if isinstance(n, ast.FunctionDef) and n.name == "_loop")
    assert not [n for n in ast.walk(loop) if isinstance(n, ast.With)], \
        "PlaywrightOwner._loop must never hold a lock across item.fn()"


# --------------------------------------------------------------------------
# 2. What actually produces the signature: unbounded work ON the owner thread.
# --------------------------------------------------------------------------
def test_a_response_handler_runs_unbounded_on_the_owner_thread():
    """`PlaywrightOwner.call`'s reentrancy guard (pw_owner.py:267) means every
    call made from inside a `page.on("response")` handler gets NO deadline.

    That is correct for thread affinity and catastrophic for wall clock: the
    handler's own cost is charged to whatever bounded call happens to be
    pumping the dispatcher, and nothing bounds it."""
    feed = _feed(js_timeout_ms=150)
    seen = {}

    def _handler_body():
        # A "nested Playwright call" from inside a handler, i.e. response.json().
        t0 = time.monotonic()
        feed._call_bounded(lambda: time.sleep(0.45))
        seen["elapsed"] = time.monotonic() - t0
        return "done"

    # Submitted with a 5s bound so the OUTER call does not expire; the point is
    # that the INNER one is not bounded by js_timeout_ms=0.15s at all.
    feed._call_bounded(_handler_body, timeout_s=5.0)
    assert seen["elapsed"] > 0.4, (
        "the nested call was bounded — the reentrancy guard is supposed to run "
        "it inline with no deadline, which is exactly why a handler can wedge "
        "the owner")


def test_an_unbounded_response_handler_reproduces_the_whole_live_signature(caplog):
    """No browser, no locks, no mouse.wheel — and the live log comes out verbatim.

    Timeline reproduced:
      * one bounded call (`_scroll_comment_dialog`-shaped `page.evaluate`) does
        not return, because a hinted response fired ON the owner thread inside
        it and `response.json()` never came back;
      * `_wheel_once`'s `mouse.wheel` then logs "CDP scroll wheel timed out"
        WITHOUT EVER BEING CALLED — it is a free fast-fail;
      * the JS fallback logs "SKIPPED — the owner thread is still inside the
        hung wheel call", which is a mis-attribution: the owner is inside the
        response handler, and the wheel never ran;
      * three "consecutive bounded calls did not return" is really ONE call that
        did not return plus two microsecond fast-fails;
      * `_halt_if_owner_wedged` raises HaltSession("cdp_call_wedged").
    """
    feed = _feed(js_timeout_ms=150, wedge_limit=3)
    release = threading.Event()
    resp = _HangingResponse(release)
    calls = {"evaluate": 0, "wheel": 0}

    class _Mouse:
        def wheel(self, dx, dy):
            calls["wheel"] += 1

    class _Page:
        mouse = _Mouse()

        def evaluate(self, js, *a):
            calls["evaluate"] += 1
            # Playwright dispatches response handlers on the owner thread while
            # this call pumps the dispatcher (connection.py: EventGreenlet).
            feed._on_response(resp)
            return False

    page = _Page()
    owner = feed._owner

    caplog.set_level("DEBUG")
    t0 = time.monotonic()
    with pytest.raises(PlaywrightTimeout):
        feed._call_bounded(lambda: page.evaluate("/* comment dialog scroll */"))
    first_call_cost = time.monotonic() - t0

    assert resp.entered.is_set(), "the handler really did run on the owner thread"
    assert owner.wedge_streak == 1 and owner.wedge_total == 1
    assert first_call_cost >= 0.15

    # --- the wheel, which is what the live log blames ---
    t1 = time.monotonic()
    feed._scroll(page)          # arms the batch, then one _wheel_once notch
    wheel_cost = time.monotonic() - t1

    assert calls["wheel"] == 0, (
        "the wheel never reached the browser — 'CDP scroll wheel timed out' is "
        "emitted by a fast-fail, so no wheel experiment can ever reproduce it")
    assert calls["evaluate"] == 1, "the JS fallback never ran either"
    assert wheel_cost < 0.05, (
        "both wheel and fallback fast-failed for free; the 15s the operator "
        "reads as 'the wheel hung' was spent in the PREVIOUS call")
    msgs = [r.message for r in caplog.records]
    assert any("scroll wheel timed out" in m for m in msgs)
    assert any("fallback SKIPPED" in m and "hung wheel call" in m for m in msgs)
    assert any("scroll batch abandoned" in m for m in msgs)

    # --- the halt arithmetic: 1 real block + N free fast-fails ---
    assert owner.wedge_total == 1, "exactly ONE call actually failed to return"
    # 3 fast-fails now, not 2: `_wheel_once` calls `_focus(page)` (bring_to_front)
    # before the notch, since an unfocused tab never ACKs mouse input — that is the
    # fix for this very wedge. On an ALREADY-wedged owner the focus call fast-fails
    # like the rest, so the streak reaches the halt threshold one call sooner, which
    # is harmless: the owner is provably poisoned by then either way. `wedge_total`
    # is unchanged at 1 because a fast-fail costs no wall clock and blocks nothing.
    assert owner.wedge_streak == 4, "one real block plus three microsecond fast-fails"
    with pytest.raises(HaltSession) as ei:
        feed._halt_if_owner_wedged()
    assert ei.value.reason == "cdp_call_wedged"

    # --- and the browser was never broken: releasing the handler heals it ---
    release.set()
    for _ in range(300):
        if not owner.is_wedged():
            break
        time.sleep(0.01)
    assert not owner.is_wedged()
    assert feed._call_bounded(lambda: "alive") == "alive"
    assert owner.wedge_streak == 0


def test_one_slow_call_is_enough_to_trip_the_halt():
    """`max_consecutive_wedged_calls=3` does not mean "three calls hung".

    Because a fast-fail counts toward the streak (pw_owner.py:273) and costs
    microseconds, the halt threshold is reached ~instantly after the FIRST
    abandoned call. There is no confirmation WINDOW — contrast the worker's
    401 path, which requires both a count and 5 minutes of wall clock."""
    feed = _feed(js_timeout_ms=100, wedge_limit=3)
    release = threading.Event()
    with pytest.raises(PlaywrightTimeout):
        feed._call_bounded(lambda: release.wait(30))
    t0 = time.monotonic()
    for _ in range(2):
        with pytest.raises(PlaywrightTimeout):
            feed._call_bounded(lambda: "never runs")
    assert time.monotonic() - t0 < 0.05, "two free fast-fails"
    assert feed._owner.wedge_streak >= feed.cfg.max_consecutive_wedged_calls
    with pytest.raises(HaltSession):
        feed._halt_if_owner_wedged()
    release.set()


def test_enqueue_reel_on_the_owner_thread_blocks_only_for_the_lock_hold_time():
    """Audit answer for '_on_response -> _classify -> _enqueue_reel can block on
    a lock the caller holds — for how long?'  For exactly as long as the caller
    holds it, and every real hold site is O(1) in-memory work
    (cdp.py:594/614/692/707/720/745, instagram/cdp.py:151/432)."""
    feed = _feed(js_timeout_ms=2000)
    done = threading.Event()
    blocked_for = {}

    def _on_owner():
        t0 = time.monotonic()
        feed._enqueue_reel(Reel(reel_id="r1", author="a", caption=""))
        blocked_for["s"] = time.monotonic() - t0
        done.set()

    with feed._queue_lock:
        t = threading.Thread(target=lambda: feed._call_bounded(_on_owner), daemon=True)
        t.start()
        time.sleep(0.25)
    done.wait(5)
    assert done.is_set()
    assert 0.15 < blocked_for["s"] < 2.0
