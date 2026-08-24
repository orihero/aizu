"""call_bounded (core/bounded.py) — the real wall-clock deadline standing in for
Playwright's own timeout config, which does not actually bound every call it
looks like it should (mouse.*, and evaluate once the CDP pipe itself has gone
quiet — see the module docstring). These tests use a genuinely-hanging callable
(``threading.Event().wait()`` with no timeout) to model that exact failure mode,
not just a callable that raises quickly."""
from __future__ import annotations

import threading
import time

import pytest

from aizu.core.bounded import call_bounded


class _Boom(Exception):
    pass


def test_call_bounded_returns_a_fast_calls_result():
    assert call_bounded(lambda: 42, timeout_s=1.0, timeout_exc=_Boom) == 42


def test_call_bounded_raises_timeout_exc_on_a_call_that_never_returns():
    started = threading.Event()

    def _hang():
        started.set()
        threading.Event().wait()   # never set → blocks forever, models a frozen CDP call

    t0 = time.monotonic()
    with pytest.raises(_Boom):
        call_bounded(_hang, timeout_s=0.2, timeout_exc=_Boom)
    elapsed = time.monotonic() - t0
    assert started.wait(1.0), "the hung callable should have started"
    # The whole point: control returns promptly despite the still-hung worker.
    assert elapsed < 2.0


def test_call_bounded_propagates_the_wrapped_functions_own_exception():
    def _raises():
        raise ValueError("boom")

    with pytest.raises(ValueError, match="boom"):
        call_bounded(_raises, timeout_s=1.0, timeout_exc=_Boom)


def test_call_bounded_does_not_block_on_the_abandoned_worker_thread():
    # A second bounded call right after a timeout must not be slowed down by the
    # first call's still-blocked worker (the "fresh executor per call, never
    # shared" tradeoff the module docstring calls out).
    call_bounded(lambda: threading.Event().wait(), timeout_s=0.1, timeout_exc=_Boom) \
        if False else None  # (placeholder kept out of the timing measurement below)
    t0 = time.monotonic()
    with pytest.raises(_Boom):
        call_bounded(lambda: threading.Event().wait(), timeout_s=0.1, timeout_exc=_Boom)
    with pytest.raises(_Boom):
        call_bounded(lambda: threading.Event().wait(), timeout_s=0.1, timeout_exc=_Boom)
    assert time.monotonic() - t0 < 1.0


def test_the_abandoned_worker_thread_names_itself():
    """F-7 was diagnosed from a process listing on a box nobody can attach a debugger
    to: an abandoned bounded call outlives its caller, and the only trace it leaves is a
    thread. A bare "Thread-47" says nothing about which subsystem is stuck."""
    running = threading.Event()

    def _hang():
        running.set()
        threading.Event().wait()

    with pytest.raises(_Boom):
        call_bounded(_hang, timeout_s=0.2, timeout_exc=_Boom)
    assert running.wait(1.0)
    assert any(t.name == "bounded-call" and t.is_alive()
               for t in threading.enumerate()), \
        "the abandoned thread must be identifiable by name in a stack dump"
