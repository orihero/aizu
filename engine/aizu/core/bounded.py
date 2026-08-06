"""A real wall-clock deadline for calls that Playwright's own timeout config
does not actually bound.

``page.set_default_timeout()`` reads as if it caps every page operation, and it
DOES cap several of them (``evaluate``, ``query_selector``, ``screenshot``,
``wait_for_selector``, ...) — but confirmed live against a wedged CDP session
(Playwright 1.61): ``page.mouse.move``/``mouse.wheel``/``mouse.click`` accept no
``timeout=`` argument and do not honor the page default either, and a CDP pipe
that goes quiet mid-command (e.g. the tab got redirected to a login wall) can
leave even an `evaluate` call hanging with no exception ever raised. The default
timeout is real for the ordinary "slow but alive" case; it just does not exist
for "the underlying CDP command will never come back." ``call_bounded`` is the
actual backstop: it runs the callable on a dedicated background thread and gives
up if it has not finished within ``timeout_s``, regardless of whether Playwright
itself would ever surface a timeout.

Tradeoff, spelled out because it's easy to miss AND easy to get wrong (a first
version of this got it wrong): Python has no safe way to kill a thread
mid-syscall, so on a genuine timeout the wrapped call is still running somewhere
in the background — it just no longer holds up the caller.

This is deliberately a raw ``threading.Thread(daemon=True)``, NOT a
``concurrent.futures.ThreadPoolExecutor``. That is not a style choice: CPython
registers a single process-wide ``atexit`` hook
(``concurrent.futures.thread._python_exit``) that unconditionally ``.join()``s
EVERY thread any ``ThreadPoolExecutor`` has ever spawned — regardless of
``shutdown(wait=False)`` and regardless of whether the pool is still reachable.
A worker stuck forever in a hung Playwright call would register there and turn
into exactly the same class of bug this module exists to fix, just moved from
"mid-call" to "the whole process can't exit even after a run finishes/halts
cleanly." A plain daemon thread has no such registry: interpreter shutdown
abandons daemon threads outright, by design, so one permanently-blocked
worker costs one leaked thread for the life of the process and nothing more —
it can never block that process's own exit.
"""
from __future__ import annotations

import threading
from typing import Callable, Optional, TypeVar

T = TypeVar("T")


class _Box:
    """Holds a bounded call's outcome across the thread boundary."""
    __slots__ = ("value", "error")

    def __init__(self) -> None:
        self.value: object = None
        self.error: Optional[BaseException] = None


def call_bounded(fn: Callable[[], T], timeout_s: float, *,
                 timeout_exc: type[BaseException]) -> T:
    """Run ``fn()`` and return its result, but raise ``timeout_exc`` instead of
    blocking past ``timeout_s`` seconds — even if ``fn`` itself would never
    raise or return. See the module docstring for why this is a raw daemon
    thread rather than a pooled executor."""
    box = _Box()
    done = threading.Event()

    def _runner() -> None:
        try:
            box.value = fn()
        except BaseException as e:  # noqa: BLE001 — re-raised on the caller's thread below
            box.error = e
        finally:
            done.set()

    # daemon=True is load-bearing (see module docstring): if fn() never returns,
    # this thread is abandoned forever, but that can NEVER block interpreter
    # shutdown the way a ThreadPoolExecutor worker would.
    threading.Thread(target=_runner, daemon=True).start()
    if not done.wait(timeout_s):
        raise timeout_exc(f"call exceeded {timeout_s:.3f}s hard deadline")
    if box.error is not None:
        raise box.error
    return box.value  # type: ignore[return-value]
