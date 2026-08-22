"""The Playwright owner thread: a real deadline that never moves the call.

These lock the four behaviours that are easy to break and expensive to break
(ledger D6): reentrancy must not deadlock, an abandoned call must never run
late, a wedged owner must fast-fail instead of queueing, and the wedge signal
must self-clear. Plus the loud-failure rule: a ``greenlet.error`` may NEVER be
swallowed by an ``except Exception``.
"""
import threading
import time

import pytest

from aizu.core.pw_owner import (OwnedPW, PlaywrightOwner,
                                PlaywrightThreadAffinityError)


class _Boom(Exception):
    """Stand-in for PlaywrightTimeout so these tests need no playwright import."""


def test_none_timeout_runs_straight_through_on_the_callers_thread():
    # The degrade-to-today's-behaviour seam: no deadline, no thread spawned.
    owner = PlaywrightOwner()
    here = threading.get_ident()
    assert owner.call(lambda: threading.get_ident(), None, _Boom) == here
    assert owner._thread is None


def test_degrades_to_unbounded_when_the_owner_thread_cannot_start(monkeypatch):
    # An exotic environment must degrade to "no deadline" (today's behaviour),
    # NEVER to "raises" — a raise here is eaten by the CDP degrade guards and
    # becomes a silently skipped scroll, i.e. a zero-lead harvest.
    owner = PlaywrightOwner()

    class _DeadThread:
        def __init__(self, *a, **k):
            pass

        def start(self):
            raise RuntimeError("can't start new thread")

    monkeypatch.setattr(threading, "Thread", _DeadThread)
    here = threading.get_ident()
    assert owner.call(lambda: threading.get_ident(), 5.0, _Boom) == here


def test_a_failed_thread_start_is_latched_so_a_later_call_cannot_migrate():
    """The forbidden move, arriving mid-session instead of up front.

    A TRANSIENT ``can't start new thread`` (worker box under thread pressure —
    the sidecar already spawns threads per job) used to leave ``_thread is None``
    with nothing recording it. ``attach()`` therefore ran inline and every
    Playwright object was born on the CALLER's thread; the next call retried
    ``_ensure()``, succeeded, and dispatched to a brand-new thread that owns
    nothing — ``greenlet.error`` on every call from then on, escalated to a
    ``BaseException`` that kills the run. Both calls must stay on one thread.
    """
    owner = PlaywrightOwner()
    real_start = threading.Thread.start
    failed_once = []

    def _flaky_start(self):
        if not failed_once and self.name == "pw-owner":
            failed_once.append(1)
            raise RuntimeError("can't start new thread")
        return real_start(self)

    threading.Thread.start = _flaky_start          # type: ignore[method-assign]
    try:
        first = owner.call(lambda: threading.get_ident(), 5.0, _Boom)
        second = owner.call(lambda: threading.get_ident(), 5.0, _Boom)
    finally:
        threading.Thread.start = real_start        # type: ignore[method-assign]

    assert failed_once, "the flaky start never fired — the test proves nothing"
    assert first == threading.get_ident()          # ran inline, caller owns Playwright
    assert second == first, "a later call migrated off the owning thread"
    assert owner._thread is None


def test_wedge_streak_counts_expiries_and_fast_fails_and_resets_on_success():
    # walk() halts on this streak, so its arithmetic is load-bearing: a fast-fail
    # must count (once a call is abandoned EVERY later call takes that branch, so
    # a streak of expiries alone would sit at 1 forever and never reach a limit),
    # and any completed call must clear it.
    owner = PlaywrightOwner()
    ok = owner.call(lambda: "fine", 5.0, _Boom)
    assert ok == "fine" and owner.wedge_streak == 0

    blocker = threading.Event()
    with pytest.raises(_Boom):
        owner.call(blocker.wait, 0.05, _Boom)      # deadline expiry
    assert (owner.wedge_streak, owner.wedge_total) == (1, 1)
    with pytest.raises(_Boom):
        owner.call(lambda: "never runs", 5.0, _Boom)   # fast-fail behind the wedge
    assert (owner.wedge_streak, owner.wedge_total) == (2, 1)

    blocker.set()                                   # the wedge clears
    for _ in range(50):
        if not owner.is_wedged():
            break
        time.sleep(0.02)
    assert owner.call(lambda: "back", 5.0, _Boom) == "back"
    assert owner.wedge_streak == 0


def test_bounded_call_runs_on_the_owner_thread_not_the_callers():
    owner = PlaywrightOwner()
    here = threading.get_ident()
    ran_on = owner.call(lambda: threading.get_ident(), 5.0, _Boom)
    assert ran_on != here
    assert ran_on == owner._ident


def test_result_and_exception_both_cross_back_to_the_caller():
    owner = PlaywrightOwner()
    assert owner.call(lambda: 41 + 1, 5.0, _Boom) == 42

    def _raise():
        raise ValueError("from the owner")

    with pytest.raises(ValueError, match="from the owner"):
        owner.call(_raise, 5.0, _Boom)


def test_timeout_raises_the_supplied_exception_and_does_not_hang():
    owner = PlaywrightOwner()
    release = threading.Event()
    t0 = time.monotonic()
    with pytest.raises(_Boom):
        owner.call(lambda: release.wait(), 0.1, _Boom)
    assert time.monotonic() - t0 < 2.0
    release.set()


def test_call_from_the_owner_thread_runs_inline_instead_of_deadlocking():
    # Playwright dispatches page.on("response") handlers ON the owner thread, so
    # _on_response -> _classify -> `response.frame.page.url` re-enters here. Without
    # the reentrancy guard the inner call queues behind the outer one, which is
    # still in flight, and the owner deadlocks on itself until the deadline.
    owner = PlaywrightOwner()

    def outer():
        inner_ident = owner.call(lambda: threading.get_ident(), 5.0, _Boom)
        return inner_ident, threading.get_ident()

    t0 = time.monotonic()
    inner_ident, outer_ident = owner.call(outer, 2.0, _Boom)
    assert time.monotonic() - t0 < 1.0          # would be ~2.0s if it deadlocked
    assert inner_ident == outer_ident == owner._ident


def test_an_abandoned_item_is_dropped_not_run_late():
    # Running a timed-out item after the wedge clears means a stale mouse.click
    # firing against a completely different page state, on a live account.
    owner = PlaywrightOwner()
    release = threading.Event()
    side_effects: list[str] = []
    started = threading.Event()

    def _blocker():
        started.set()
        release.wait()
        return "blocker"

    # Occupy the owner from another thread so the main thread's submission queues.
    holder = threading.Thread(
        target=lambda: owner.call(_blocker, 10.0, _Boom), daemon=True)
    holder.start()
    assert started.wait(2.0)

    with pytest.raises(_Boom):
        owner.call(lambda: side_effects.append("phantom-click"), 0.1, _Boom)

    release.set()
    holder.join(2.0)
    time.sleep(0.2)      # give the owner time to drain the queue
    assert side_effects == [], "an abandoned item must never run late"


def test_fast_fails_while_wedged_then_self_clears_when_the_call_returns():
    owner = PlaywrightOwner()
    release = threading.Event()

    with pytest.raises(_Boom):
        owner.call(lambda: release.wait(), 0.1, _Boom)

    # Wedged: the next call must return immediately rather than queue behind a
    # call that may never come back.
    t0 = time.monotonic()
    with pytest.raises(_Boom):
        owner.call(lambda: "unreachable", 5.0, _Boom)
    assert time.monotonic() - t0 < 0.5

    # ...and the latch must clear itself once the wedged call finally returns.
    # (A shared Event latch here permanently bricks the feed after recovery.)
    release.set()
    deadline = time.monotonic() + 2.0
    while owner._inflight is not None and time.monotonic() < deadline:
        time.sleep(0.01)
    assert owner.call(lambda: "recovered", 5.0, _Boom) == "recovered"


def test_shutdown_retires_a_healthy_owner_thread_and_a_later_call_restarts_it():
    # One feed per session in a multi-session run (cli.py::_close_feed), so a
    # parked thread per finished session would accumulate.
    owner = PlaywrightOwner()
    owner.call(lambda: None, 5.0, _Boom)
    t = owner._thread
    owner.shutdown()
    t.join(2.0)
    assert not t.is_alive()
    assert owner.call(lambda: "again", 5.0, _Boom) == "again"
    assert owner._thread is not t


def test_greenlet_error_escalates_past_every_except_exception_guard():
    # THE regression test for the historical catastrophe. greenlet.error is an
    # ordinary Exception, so when calls were routed to a daemon thread every one
    # of them was swallowed by the CDP degrade guards and Instagram/X/LinkedIn
    # harvested nothing with a fully green suite. PlaywrightThreadAffinityError
    # subclasses BaseException so that can never happen quietly again.
    greenlet = pytest.importorskip("greenlet")
    owner = PlaywrightOwner()

    def _affinity_break():
        raise greenlet.error("Cannot switch to a different thread")

    escaped = None
    try:
        try:
            owner.call(_affinity_break, 5.0, _Boom)
        except Exception as e:  # noqa: BLE001 — deliberately the wrong guard
            pytest.fail(f"greenlet.error was swallowed by `except Exception`: {e!r}")
    except PlaywrightThreadAffinityError as e:
        escaped = e
    assert escaped is not None
    assert isinstance(escaped.__cause__, greenlet.error)


# ---- OwnedPW: the proxy that makes the migration zero sites wide -------------

class _Fake:
    """Carries Playwright's `_impl_obj` marker so OwnedPW treats it as a real
    sync object (that attribute is the reliable discriminator — nothing else we
    pass around, dicts/bytes/cookie lists, has it)."""
    _impl_obj = object()


class _FakeHandle(_Fake):
    def screenshot(self, **kw):
        return b"jpeg-bytes"


class _FakePage(_Fake):
    def __init__(self):
        self.url = "https://example.test/feed"
        self.idents: list[int] = []

    def evaluate(self, script):
        self.idents.append(threading.get_ident())
        return {"w": 1280}

    def query_selector(self, sel):
        return _FakeHandle() if sel == "video" else None

    def cookies(self):
        return [{"name": "sid", "value": "1"}]


def _proxy(page):
    owner = PlaywrightOwner()
    return owner, OwnedPW(owner, page, {"default": 5.0, "nav": 25.0}, _Boom)


def test_proxy_routes_calls_and_attribute_reads_to_the_owner_thread():
    page = _FakePage()
    owner, p = _proxy(page)
    assert p.evaluate("() => 1") == {"w": 1280}
    assert p.url == "https://example.test/feed"
    assert page.idents == [owner._ident] != [threading.get_ident()]


def test_proxy_rewraps_playwright_objects_but_passes_plain_values_through():
    page = _FakePage()
    _, p = _proxy(page)
    el = p.query_selector("video")
    assert isinstance(el, OwnedPW)                 # element handles stay owned
    assert el.screenshot(type="jpeg") == b"jpeg-bytes"   # ...and still callable
    # `None` MUST stay None — every call site guards on `is None`.
    assert p.query_selector("nope") is None
    # Cookie lists / dicts / bytes are data, not Playwright objects.
    assert p.cookies() == [{"name": "sid", "value": "1"}]


def test_proxy_wraps_lists_of_playwright_objects():
    class _Ctx(_Fake):
        pass

    class _Browser(_Fake):
        contexts = [_Ctx(), _Ctx()]

    _, b = _proxy(_Browser())
    ctxs = b.contexts
    assert len(ctxs) == 2 and all(isinstance(c, OwnedPW) for c in ctxs)


def test_proxy_is_truthy_so_the_shoot_fallback_still_picks_the_element():
    # `el.screenshot(...) if el else page.screenshot(...)` in _shoot depends on
    # default object truthiness; defining __bool__/__len__ here would silently
    # swap the branch.
    _, el = _proxy(_FakeHandle())
    assert bool(el) is True


def test_proxy_gives_navigation_calls_a_deadline_larger_than_playwrights_own():
    # Deadline inversion: _goto_once passes timeout=nav_timeout_ms (20s) while the
    # default queue bound is js_timeout_ms (15s). Without a bigger nav bound the
    # queue trips first and manufactures the wedges it then recovers from.
    _, p = _proxy(_FakePage())
    assert OwnedPW._deadline_for(p, "goto") == 25.0
    assert OwnedPW._deadline_for(p, "evaluate") == 5.0
