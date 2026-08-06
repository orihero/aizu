"""Atomic JSON writes for the worker's on-disk rendezvous files (spec/result).

One implementation shared by the supervisor (job_runner) and the child (job_child) so the
tmp-then-os.replace + 0600 pattern lives in exactly one place. Kept dependency-free (no
cli/engine imports) so job_child can use it on its earliest failure path without paying the
heavy engine import cost.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

# Spec files carry soul_text + local paths; result files carry a summary. Neither is a
# credential, but there is no reason to expose them beyond the owner, so mirror the 0600
# discipline FernetFileBackend uses for the token.
_FILE_MODE = 0o600


def atomic_write_json(path: Path, data: dict) -> None:
    """Write ``data`` as JSON to ``path`` atomically at mode 0600: a sibling tmp file is
    opened 0600, written, then os.replace'd onto the final path (atomic on POSIX). A
    reader therefore never sees a torn or world-readable file, even if the process is
    killed mid-write."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    blob = json.dumps(data, ensure_ascii=False).encode("utf-8")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, _FILE_MODE)
    try:
        os.write(fd, blob)
    finally:
        os.close(fd)
    os.replace(tmp, path)


def pid_alive(pid: int, *, unknown_is_alive: bool) -> bool:
    """Cross-platform: True if ``pid`` names a live process on this box.

    POSIX probes with ``os.kill(pid, 0)`` (sends no signal — pure existence +
    permission check). Windows has no such probe (``os.kill(pid, 0)`` there is a
    console-control event, not a liveness check), so we ``OpenProcess`` instead.

    ``unknown_is_alive`` is the answer when the OS cannot give a definitive yes/no
    (an unexpected error, or a non-positive pid): callers that must never
    false-reclaim a still-live owner pass ``True``; callers that must never kill an
    unconfirmed pid pass ``False``. The definitive POSIX outcomes (ESRCH → dead,
    EPERM → alive) are unchanged.
    """
    if pid <= 0:
        return unknown_is_alive
    if sys.platform == "win32":
        return _win_pid_alive(pid, unknown_is_alive)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False  # ESRCH — definitively dead
    except PermissionError:
        return True  # EPERM — alive, just owned by another user
    except OSError:
        return unknown_is_alive
    return True


def _win_pid_alive(pid: int, unknown_is_alive: bool) -> bool:
    """Windows liveness via OpenProcess. ``ctypes`` is stdlib and imported lazily so
    this module stays dependency-free on the POSIX child-failure path."""
    import ctypes

    PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    STILL_ACTIVE = 259
    ERROR_INVALID_PARAMETER = 87  # no process with that id → dead
    ERROR_ACCESS_DENIED = 5  # exists but owned by another user → alive
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.OpenProcess.restype = ctypes.c_void_p  # HANDLE (avoid 64-bit truncation)
    handle = k32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, int(pid))
    if handle:
        code = ctypes.c_ulong()
        got = k32.GetExitCodeProcess(ctypes.c_void_p(handle), ctypes.byref(code))
        k32.CloseHandle(ctypes.c_void_p(handle))
        return bool(got) and code.value == STILL_ACTIVE
    err = ctypes.get_last_error()
    if err == ERROR_INVALID_PARAMETER:
        return False
    if err == ERROR_ACCESS_DENIED:
        return True
    return unknown_is_alive
