"""A real wall-clock deadline for Playwright calls, WITHOUT ever moving the call.

Why this module exists (read this before touching it — ledger D6)
----------------------------------------------------------------
Playwright's SYNC API is not thread-hostile, it is thread-**AFFINE**. Calling
``sync_playwright().start()`` spins up an asyncio loop inside a *MainGreenlet*
pinned to the calling thread; every subsequent sync call switches into that
greenlet to pump the loop. A greenlet can only be switched to from the thread
that created it, so touching ANY Playwright sync object from another thread
raises ``greenlet.error: Cannot switch to a different thread`` — 100% of the
time, not intermittently.

``core/bounded.py``'s ``call_bounded`` gets a hard deadline by running the
callable on a daemon thread. That is exactly the forbidden move. When
``CDPFeedBase._call_bounded`` routed Playwright calls through it, every wheel
notch AND its JS fallback raised ``greenlet.error``, was swallowed by the
surrounding ``except Exception``, and Instagram/X/LinkedIn silently harvested
NOTHING while the whole test suite stayed green.

The inversion here is the whole trick: **the call never changes threads — only
the WAIT does.** One daemon thread per feed owns Playwright: ``attach()`` itself
runs there, so the driver, the browser, the context, every page and every
ElementHandle are *born* on that thread and it IS their owning thread. Callers
submit a closure and block on an ``Event`` with a timeout; on expiry the caller
walks away and the owner keeps grinding (Python cannot kill a thread
mid-syscall — same honest tradeoff ``core/bounded.py`` documents).

Precedent already in the tree: ``engines/instagram/readiness.py`` uses
``call_bounded`` **safely** precisely because it calls ``sync_playwright()``
INSIDE the daemon thread. Affinity, not identity, is the rule.

Two things here are load-bearing and easy to delete by accident:

  1. ``OwnedPW`` — an auto-dispatching proxy. A hand-migration of the ~35 direct
     Playwright touch sites fails SILENTLY: every missed site raises
     ``greenlet.error`` under an ``except Exception: return False/None``, i.e.
     the original catastrophe with the sign flipped. The proxy makes the
     migration zero sites wide.
  2. ``PlaywrightOwner.call``'s reentrancy guard. Playwright dispatches
     ``page.on("response", ...)`` handlers ON the owner thread, so
     ``_on_response`` → ``_classify`` → ``instagram/cdp.py``'s
     ``response.frame.page.url`` executes there; without the guard any proxied
     touch inside a handler would enqueue behind the owner's own in-flight item
     and block until the deadline.

And ``PlaywrightThreadAffinityError`` deliberately subclasses ``BaseException``
so that none of the ~20 ``except Exception:`` degrade paths in ``core/cdp.py``,
``core/human.py`` and ``engines/*/cdp.py`` can swallow it. That single rule is
what turns this class of bug from six months of silent zero-harvest into an
immediate, obvious crash.
"""
from __future__ import annotations

import queue
import threading
from typing import Any, Callable, Optional, TypeVar

from .logsetup import get_logger

log = get_logger(__name__)

T = TypeVar("T")

# Optional at import time, exactly like Playwright itself in core/cdp.py. The
# empty tuple makes ``isinstance(e, _GreenletError)`` a cheap, always-False
# no-op when greenlet is absent, so the escalation degrades to "re-raise as-is"
# rather than exploding at import.
try:  # pragma: no cover - greenlet ships with playwright; absent only in odd envs
    from greenlet import error as _GreenletError  # type: ignore
except Exception:  # pragma: no cover
    _GreenletError = ()  # type: ignore[assignment]

# The CANONICAL timeout type for this codebase, defined here (not in core/cdp.py)
# so ``core/human.py`` can raise the exact same class without importing cdp.py
# and creating an import cycle. ``core.cdp`` re-exports it, so
# ``from aizu.core.cdp import PlaywrightTimeout`` keeps working and every
# ``except PlaywrightTimeout:`` in the engines still catches an owner-thread
# deadline expiry.
try:
    from playwright.sync_api import TimeoutError as PlaywrightTimeout  # type: ignore
except Exception:  # pragma: no cover - playwright optional at import time

    class PlaywrightTimeout(Exception):  # type: ignore[no-redef]
        """Fallback when Playwright is not importable. The real
        ``playwright.sync_api.TimeoutError`` subclasses ``Exception``, so an
        explicit ``except PlaywrightTimeout`` behaves identically to the generic
        ``except Exception`` fallback that already guards these blocks — the
        named catch is load-bearing for LOG VISIBILITY (a silent CDP hang was the
        confirmed root cause of a wedged harvest) and for future-proofing."""


class PlaywrightThreadAffinityError(BaseException):
    """A Playwright sync call was made from a thread that does not own it.

    Deliberately NOT an ``Exception`` subclass. Every CDP degrade path in this
    codebase is written as ``except Exception: return False/None`` — that is the
    correct posture for a flaky browser, and the exact reason the previous
    thread-affinity break was invisible: ``greenlet.error`` is an ``Exception``,
    so a 100%-failing call read as "the page is being slow" and every harvest
    quietly returned zero leads with a fully green test suite.

    Raising a ``BaseException`` means this failure mode can only ever be a loud
    crash. If you see it, some Playwright object escaped ``OwnedPW`` and is
    being touched off its owning thread — find the raw reference, do not widen
    an except clause.
    """


# Retire-the-thread sentinel. Not an _Item: it carries no work and must never be
# mistaken for one.
_SHUTDOWN = object()


class _Item:
    """One unit of work handed to the owner thread."""
    __slots__ = ("fn", "value", "error", "done", "abandoned")

    def __init__(self, fn: Callable[[], Any]) -> None:
        self.fn = fn
        self.value: Any = None
        self.error: Optional[BaseException] = None
        self.done = threading.Event()
        # Written ONLY by the submitting caller, read by the owner. Never a
        # shared latch — see PlaywrightOwner.call step 4.
        self.abandoned = threading.Event()


class PlaywrightOwner:
    """Owns one Playwright world on one daemon thread; hands out deadlines.

    Create one per feed. The thread starts lazily on the first bounded ``call``
    so a feed that never attaches never spawns anything.
    """

    def __init__(self) -> None:
        self._q: "queue.SimpleQueue[_Item]" = queue.SimpleQueue()
        # Written ONLY by the owner thread, read by callers. Using the ITEM as
        # the wedge signal (rather than a shared Event latch) makes the signal
        # self-clearing by construction: when the wedged call finally returns,
        # the owner sets _inflight = None and the next caller stops fast-failing
        # with no reset step to forget. A shared latch permanently bricks the
        # feed after a recoverable freeze.
        self._inflight: Optional[_Item] = None
        self._lock = threading.Lock()
        self._thread: Optional[threading.Thread] = None
        self._ident: Optional[int] = None
        # LATCH: once the owner thread has failed to start we NEVER try again.
        # Without it the degradation is not "unbounded", it is "unbounded, then
        # a different thread" — `_ensure()` retries on the next call, succeeds,
        # and dispatches to a brand-new thread that owns nothing, while every
        # Playwright object was born inline on the caller's thread during
        # `attach()`. That is the forbidden move (greenlet.error on 100% of
        # calls, ledger D6) arriving mid-session instead of up front.
        self._inline_only = False
        # Wedge telemetry, read by CDPFeedBase.walk(). A deadline expiry (or a
        # fast-fail behind one) means a Playwright call is never coming back;
        # WITHOUT this counter every later call degrades quietly and a dead
        # browser is reported as a clean, successful, zero-lead run — the same
        # silent-zero-harvest signature this module exists to prevent, just with
        # a different cause. Any call that completes within its deadline proves
        # the owner is alive again and resets the streak.
        self.wedge_streak = 0
        self.wedge_total = 0

    def is_wedged(self) -> bool:
        """True while the owner is still stuck inside a call a caller abandoned.
        Self-clearing: the owner sets ``_inflight = None`` if it ever returns."""
        cur = self._inflight
        return cur is not None and cur.abandoned.is_set()

    # ---- lifecycle ----
    def _ensure(self) -> bool:
        """Start the owner thread if needed. Returns False when it could not be
        started at all — the caller then runs ``fn`` inline with NO deadline,
        i.e. exactly today's behaviour. Degrading to "unbounded" is the only
        acceptable failure here: degrading to "raises" would be swallowed by the
        CDP guards and become a silently skipped scroll, which is the
        harvest-killing outcome this whole module exists to prevent."""
        with self._lock:
            if self._thread is not None:
                return True
            if self._inline_only:
                # A previous start failed, so `attach()` (and everything after
                # it) ran inline on the caller's thread and that thread OWNS the
                # Playwright world. Starting a thread now would move the calls
                # off their owning thread — see `_inline_only` in __init__.
                return False
            # daemon=True is load-bearing, and a ThreadPoolExecutor is FORBIDDEN
            # here: CPython's process-wide ``concurrent.futures.thread._python_exit``
            # atexit hook joins EVERY thread any pool ever spawned, so a
            # permanently wedged Playwright call would stop the process exiting
            # at all. See core/bounded.py's module docstring for the full write-up.
            t = threading.Thread(target=self._loop, daemon=True, name="pw-owner")
            try:
                t.start()
            except Exception:  # noqa: BLE001 — e.g. "can't start new thread"
                # LATCHED, not retried: see `_inline_only`. Retrying is how a
                # transient "can't start new thread" turns into a permanent
                # thread-affinity violation half a session later.
                self._inline_only = True
                log.warning("Playwright owner thread could not start — running "
                            "unbounded (no deadline) on the caller's thread for "
                            "the REST of this feed's life (latched, so a later "
                            "call can never migrate off its owning thread)")
                return False
            # Set the ident HERE, not inside _loop: doing it in the thread body
            # races the very first call()'s reentrancy check.
            self._ident = t.ident
            self._thread = t
            return True

    def shutdown(self) -> None:
        """Retire the owner thread after a clean ``close()``.

        A multi-session run builds one feed per session in ONE process
        (``cli.py::_close_feed``), so without this each finished session would
        leak an idle thread parked on ``q.get()`` for the life of the process.
        Best-effort by construction: a WEDGED owner never reaches the sentinel,
        and that thread (plus its node driver) stays leaked — unavoidable,
        Python cannot kill a thread mid-syscall, and ``daemon=True`` means it
        still cannot block process exit. ``_ident`` is deliberately NOT cleared:
        a response handler still running on the retiring thread must keep
        hitting the reentrancy guard rather than enqueueing to nobody."""
        with self._lock:
            t, self._thread = self._thread, None
        if t is not None:
            self._q.put(_SHUTDOWN)  # type: ignore[arg-type]

    def _loop(self) -> None:
        while True:
            item = self._q.get()
            if item is _SHUTDOWN:
                return
            if item.abandoned.is_set():
                # MANDATORY, not a nicety. A wedge clears, the owner drains its
                # backlog, and a stale `mouse.click` fires against a completely
                # different page state — on a live, logged-in account.
                continue
            self._inflight = item
            try:
                item.value = item.fn()
            except BaseException as e:  # noqa: BLE001 — re-raised on the caller's thread
                item.error = e
            finally:
                self._inflight = None
                item.done.set()

    # ---- the one entry point ----
    def call(self, fn: Callable[[], T], timeout_s: Optional[float],
             timeout_exc: type[BaseException]) -> T:
        """Run ``fn()`` on the owner thread and return its result, raising
        ``timeout_exc`` instead of blocking past ``timeout_s`` seconds.

        ``timeout_s is None`` means "no deadline" and runs ``fn`` straight
        through on the caller's thread — today's behaviour, preserved so an
        unexpected environment degrades to "unbounded" rather than to "raises"
        or "silently skips the call".
        """
        if timeout_s is None:
            return fn()
        if not self._ensure():
            return fn()   # no owner available → unbounded, i.e. today's behaviour
        # REENTRANCY GUARD. Playwright dispatches page.on("response") handlers on
        # the owner thread; anything they touch is already on the right thread,
        # and enqueueing from here would deadlock the owner behind its own
        # in-flight item until the deadline expires.
        if threading.get_ident() == self._ident:
            return fn()
        # FAST-FAIL while wedged: don't queue behind a call that is never coming
        # back. Self-clearing — only the owner writes _inflight.
        cur = self._inflight
        if cur is not None and cur.abandoned.is_set():
            # A fast-fail counts toward the streak too. It must: once a call is
            # abandoned, EVERY later call takes this branch, so a streak that
            # only counted real expiries would sit at 1 forever and no caller
            # could ever tell a permanent wedge from one slow call.
            self.wedge_streak += 1
            raise timeout_exc(
                "playwright owner is wedged on an abandoned call — fast-failing")
        item = _Item(fn)
        self._q.put(item)
        if not item.done.wait(timeout_s):
            item.abandoned.set()
            self.wedge_streak += 1
            self.wedge_total += 1
            raise timeout_exc(
                f"call exceeded {timeout_s:.3f}s on the playwright owner")
        # Completed within the deadline — errors included: the owner came back,
        # so whatever wedge there was has cleared.
        self.wedge_streak = 0
        if item.error is not None:
            if _GreenletError and isinstance(item.error, _GreenletError):
                raise PlaywrightThreadAffinityError(
                    "a Playwright sync call was made off its owning thread "
                    "(greenlet.error) — a raw Playwright object escaped OwnedPW; "
                    "see aizu/core/pw_owner.py and ledger D6"
                ) from item.error
            raise item.error
        return item.value  # type: ignore[return-value]


def _is_pw_object(v: Any) -> bool:
    """True for a Playwright *sync* object. Every one of them is a ``SyncBase``
    carrying an ``_impl_obj`` back-reference; nothing else in the data we hand
    around (dicts, str, bytes, cookie lists) has that attribute. Verified live
    against playwright 1.62 for Page/BrowserContext/Browser/Mouse/ElementHandle."""
    try:
        return hasattr(v, "_impl_obj")
    except Exception:  # noqa: BLE001 — an exotic __getattr__ must not break wrapping
        return False


class OwnedPW:
    """Transparent proxy that routes every attribute read and every call on a
    Playwright object to its owning thread, with a deadline.

    This exists so the ~35 direct Playwright touch sites across ``core/cdp.py``
    and ``engines/*/cdp.py`` need **zero** edits. A hand migration is the wrong
    tool: a missed site raises ``greenlet.error`` inside an ``except Exception:
    return False/None`` and degrades to a silent no-op with a green suite —
    precisely the failure this whole module exists to make impossible.

    Deliberately does NOT define ``__bool__``/``__len__``/``__eq__``: call sites
    like ``el.screenshot(...) if el else page.screenshot(...)`` and the many
    ``if self._page is None`` guards rely on default object truthiness and on
    ``None`` staying ``None`` (``_wrap`` passes ``None`` straight through).
    """
    __slots__ = ("_owner", "_real", "_deadlines", "_timeout_exc")

    # Calls that carry Playwright's OWN navigation timeout internally. Their
    # queue deadline must be strictly greater than that, or we manufacture the
    # very wedges we then "recover" from: _goto_once passes timeout=20000ms
    # while the default queue bound is js_timeout_ms (15000ms), so a nav
    # Playwright would have survived would trip the queue first and then
    # fast-fail everything behind it. Deadline inversion is real.
    _NAV_CALLS = frozenset({"goto", "wait_for_load_state", "wait_for_url", "reload"})

    def __init__(self, owner: PlaywrightOwner, real: Any,
                 deadlines: dict, timeout_exc: type[BaseException]) -> None:
        object.__setattr__(self, "_owner", owner)
        object.__setattr__(self, "_real", real)
        object.__setattr__(self, "_deadlines", deadlines)
        object.__setattr__(self, "_timeout_exc", timeout_exc)

    # ---- internals ----
    def _deadline_for(self, name: str) -> Optional[float]:
        d = object.__getattribute__(self, "_deadlines")
        if name in OwnedPW._NAV_CALLS:
            return d.get("nav")
        return d.get("default")

    def _wrap(self, v: Any) -> Any:
        """Re-wrap a Playwright object coming back out, so it can never be
        touched raw. Everything else (None, scalars, dicts, bytes, cookie
        lists) passes straight through — ``None`` MUST stay ``None``, every
        call site guards on ``is None``."""
        owner = object.__getattribute__(self, "_owner")
        deadlines = object.__getattribute__(self, "_deadlines")
        texc = object.__getattribute__(self, "_timeout_exc")
        if _is_pw_object(v):
            return OwnedPW(owner, v, deadlines, texc)
        if isinstance(v, list) and v and _is_pw_object(v[0]):
            # ctx.pages / browser.contexts / query_selector_all
            return [OwnedPW(owner, e, deadlines, texc) if _is_pw_object(e) else e
                    for e in v]
        return v

    @staticmethod
    def _unwrap(v: Any) -> Any:
        return object.__getattribute__(v, "_real") if isinstance(v, OwnedPW) else v

    # ---- the proxy surface ----
    def __getattr__(self, name: str) -> Any:
        owner = object.__getattribute__(self, "_owner")
        real = object.__getattribute__(self, "_real")
        texc = object.__getattribute__(self, "_timeout_exc")
        deadline = OwnedPW._deadline_for(self, name)
        val = owner.call(lambda: getattr(real, name), deadline, texc)
        if callable(val):
            def _proxied(*args: Any, **kwargs: Any) -> Any:
                a = tuple(OwnedPW._unwrap(x) for x in args)
                k = {kk: OwnedPW._unwrap(vv) for kk, vv in kwargs.items()}
                out = owner.call(lambda: val(*a, **k), deadline, texc)
                return OwnedPW._wrap(self, out)
            _proxied.__name__ = name
            return _proxied
        return OwnedPW._wrap(self, val)

    def __repr__(self) -> str:  # pragma: no cover - debugging aid only
        return f"<OwnedPW {object.__getattribute__(self, '_real')!r}>"
