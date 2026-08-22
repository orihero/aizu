"""Frozen-binary entry shim for the aizu-worker sidecar.

PyInstaller must NOT run aizu/worker/sidecar.py directly as __main__ — that breaks its
package-relative imports (`from ..core ...`, `from . import ...`). Instead this shim imports
the sidecar as a PACKAGE module and calls the SAME main() the console_script uses, so the
frozen binary and `pip install -e engine && aizu-worker` are behaviorally identical.

It ALSO routes every `-m <module>` self-invocation the app makes of its OWN binary. Two exist
today: the Phase-6 supervisor spawns each job via
``[sys.executable, "-m", "aizu.worker.job_child", "--spec-file", X]``, and the desktop shell
asks this binary where Chrome for Testing lives (and, with ``--install``, tells it to download
Chrome for Testing) via ``-m aizu.worker.chrome_path`` — it has no interpreter of its own to
ask (ledger A4). Under a real interpreter `-m` runs the module; but in a FROZEN binary
``sys.executable`` is THIS binary and PyInstaller's bootloader IGNORES `-m` — so without this
dispatch each of those would boot a SECOND sidecar (competing register/control-surface bind)
and get killed (rc=-9), leaving the job stuck `queued` forever. That is ledger A2, and it is
why `_MODULE_DISPATCH` must gain a row BEFORE any new `-m` caller ships: an argv nobody here
recognises does not error, it launches the app.

Anything the table does not match still falls through to the sidecar, so the packaged binary's
normal (no-argv) boot is untouched.

This shim is also where PLAYWRIGHT_BROWSERS_PATH gets pinned — see
:func:`_pin_playwright_browsers_path`, which every entry below inherits because they all come
through :func:`_main`.
"""
import os
import sys


def _default_playwright_browsers_path() -> str:
    """Playwright's OWN documented per-user browser cache, computed the way its Node
    registry computes it (``driver/package/lib/coreBundle.js``: ``defaultCacheDirectory``
    + ``ms-playwright``). Mirrored rather than approximated on purpose — including
    ``XDG_CACHE_HOME``/``LOCALAPPDATA`` — because the whole point is that the frozen binary
    and a dev tree's ``playwright install chromium`` land in the SAME directory, and a box
    that sets either var would otherwise silently end up with two caches."""
    home = os.path.expanduser("~")
    if sys.platform == "darwin":
        base = os.path.join(home, "Library", "Caches")
    elif sys.platform == "win32":
        base = os.environ.get("LOCALAPPDATA") or os.path.join(home, "AppData", "Local")
    else:
        base = os.environ.get("XDG_CACHE_HOME") or os.path.join(home, ".cache")
    return os.path.join(base, "ms-playwright")


def _pin_playwright_browsers_path() -> None:
    """Pin PLAYWRIGHT_BROWSERS_PATH to the per-user cache, unless the box already set it.

    THE BUG THIS EXISTS FOR: ``playwright/_impl/_transport.py`` does, verbatim,

        if getattr(sys, "frozen", False) or globals().get("__compiled__"):
            env.setdefault("PLAYWRIGHT_BROWSERS_PATH", "0")

    and the Node driver reads "0" as "browsers live INSIDE the package", i.e.
    ``<bundle>/_internal/playwright/driver/package/.local-browsers/``. ``sidecar.spec`` ships
    the driver tree but no browsers, and that directory does not exist in the bundle — so on
    a packaged box the frozen binary could never resolve a browser, even on a machine whose
    ~/Library/Caches/ms-playwright was fully populated. Observed directly: ``-m
    aizu.worker.chrome_path`` returned rc=1 with an empty stdout and a phantom
    ``.local-browsers/...`` path in its remedy line, and the SAME binary returned rc=0 with
    the real path the moment PLAYWRIGHT_BROWSERS_PATH was exported by the parent.

    That ``env.setdefault`` is the hook: a value already in os.environ always wins. So we set
    one here, and we set it with ``os.environ.setdefault`` in turn, so an operator or an ops
    deployment that pins its own cache (a shared read-only browser volume, say) still beats us.

    Pinning it HERE rather than in the Rust shell covers both callers for free — the
    chrome_path probe/installer and the real sidecar both enter through :func:`_main` — and it
    is what makes the desktop app share one browser cache with the dev tree and with
    ``engine/scripts/warm_chrome.sh``. The browser is a ~356 MB download; it must not live
    inside the .app bundle, which on macOS sits in /Applications and is REPLACED wholesale on
    every update.
    """
    os.environ.setdefault("PLAYWRIGHT_BROWSERS_PATH", _default_playwright_browsers_path())


def _run_job_child(argv):
    """Ledger A2: the supervised job-child re-exec."""
    from aizu.worker.job_child import main
    return main(argv)


def _run_chrome_path(argv):
    """The desktop shell's Chrome-for-Testing query and `--install` download (stdout = path,
    exit code = verdict)."""
    from aizu.worker.chrome_path import main
    return main(argv)


# argv module name -> a callable taking the REMAINING argv. Values are thunks, not imported
# modules, so a frozen boot only ever imports the one module it was actually asked for.
_MODULE_DISPATCH = {
    "aizu.worker.job_child": _run_job_child,
    "aizu.worker.chrome_path": _run_chrome_path,
}


def _main() -> int:
    # FIRST, before any branch below imports Playwright (or spawns its Node driver, which is
    # where the env is actually read). Importing this module must stay side-effect-free, so
    # the pin lives here and not at module scope.
    _pin_playwright_browsers_path()
    argv = sys.argv[1:]
    # `<binary> -m <module> [args...]` — a self-invocation, not a sidecar boot.
    if argv[:1] == ["-m"] and len(argv) >= 2:
        entry = _MODULE_DISPATCH.get(argv[1])
        if entry is not None:
            return entry(argv[2:])
    from aizu.worker.sidecar import main
    return main()


if __name__ == "__main__":
    sys.exit(_main())
