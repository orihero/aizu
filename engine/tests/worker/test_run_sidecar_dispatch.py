"""The frozen entry shim's argv dispatch (`desktop/pyinstaller/run_sidecar.py`).

Ledger A2 in one sentence: in a PyInstaller freeze the bootloader ignores `-m`, so an argv
this shim does not explicitly recognise does not error — it BOOTS A SECOND SIDECAR, which
competes on register/the control-surface bind and is killed at rc=-9, stranding the job
`queued` forever. That makes the dispatch table a gate, not a convenience, and it is why the
"unknown argv falls through" case below is asserted as loudly as the routed ones.

The shim is ALSO the one place PLAYWRIGHT_BROWSERS_PATH gets pinned, and that pin is a gate
of the same kind. Playwright's own `_impl/_transport.py` does, when frozen,
`env.setdefault("PLAYWRIGHT_BROWSERS_PATH", "0")`, and the Node driver reads "0" as "browsers
live inside the package" — a `.local-browsers/` directory `sidecar.spec` never ships. Verified
live on the built binary: `-m aizu.worker.chrome_path` returned rc=1 with a phantom
`.local-browsers/...` path even on a machine whose ~/Library/Caches/ms-playwright was full,
and the SAME binary returned rc=0 the moment the parent exported the var. So these tests pin
BOTH that the value is set and that it is set before any dispatched entry runs.

These tests load the SHIPPED file (the freeze embeds that exact script, and it belongs to no
importable package) and drive `_main()` in-process with every entry point stubbed — booting
the real sidecar would try to register against a dispatch URL.
"""
from __future__ import annotations

import importlib
import inspect
import os
import sys
import types
from pathlib import Path

import pytest

from aizu.worker import chrome_path, job_child, sidecar

RUN_SIDECAR = (Path(__file__).resolve().parents[3]
               / "desktop" / "pyinstaller" / "run_sidecar.py")

# Distinct per-entry return codes, so a test can prove WHICH one ran and that its exit code
# is propagated rather than swallowed.
_RC_JOB_CHILD = 7
_RC_CHROME_PATH = 3
_RC_SIDECAR = 5


@pytest.fixture
def shim():
    """The real file, compiled from SOURCE on every use — there is no `desktop.pyinstaller`
    package to import, and testing a re-typed copy of the logic would prove nothing about
    the freeze.

    Deliberately not `spec_from_file_location` + `exec_module`: that caches bytecode in
    `desktop/pyinstaller/__pycache__/`, keyed on (mtime-seconds, size). Edit this shim and
    revert it inside the same second with no size change — which is exactly what a
    revert-check on a one-word constant does — and the loader silently replays the STALE
    pyc, so the suite reports a failure the working tree does not contain. Observed while
    revert-checking the pin below. Reading the text is cheap and cannot go stale."""
    assert RUN_SIDECAR.exists(), f"frozen entry shim missing: {RUN_SIDECAR}"
    module = types.ModuleType("aizu_run_sidecar_shim")
    module.__file__ = str(RUN_SIDECAR)
    source = RUN_SIDECAR.read_text(encoding="utf-8")
    exec(compile(source, str(RUN_SIDECAR), "exec"), module.__dict__)  # noqa: S102
    return module


@pytest.fixture
def routed(monkeypatch) -> list:
    """Record which entry point the shim chose, instead of running it. The shim imports each
    main() at CALL time, so patching the attribute on the module is what the dispatch sees."""
    seen: list = []
    monkeypatch.setattr(job_child, "main",
                        lambda argv: seen.append(("job_child", list(argv))) or _RC_JOB_CHILD)
    monkeypatch.setattr(chrome_path, "main",
                        lambda argv: seen.append(("chrome_path", list(argv))) or _RC_CHROME_PATH)
    monkeypatch.setattr(sidecar, "main",
                        lambda *a, **kw: seen.append(("sidecar", None)) or _RC_SIDECAR)
    return seen


_BROWSERS_PATH = "PLAYWRIGHT_BROWSERS_PATH"


@pytest.fixture(autouse=True)
def _isolate_browsers_path(monkeypatch):
    """`_main()` mutates os.environ by design; nothing else in the suite may inherit it."""
    monkeypatch.delenv(_BROWSERS_PATH, raising=False)


def _run(shim, monkeypatch, *args) -> int:
    monkeypatch.setattr(sys, "argv", ["aizu-worker", *args])
    return shim._main()


def test_job_child_argv_still_routes_to_the_job_child(shim, monkeypatch, routed):
    """LEDGER A2 REGRESSION GATE. Generalising the single-module check into a table must not
    change this one byte: every leased job in the packaged app comes through here."""
    rc = _run(shim, monkeypatch, "-m", "aizu.worker.job_child", "--spec-file", "/tmp/spec.json")
    assert routed == [("job_child", ["--spec-file", "/tmp/spec.json"])]
    assert rc == _RC_JOB_CHILD


def test_chrome_path_argv_routes_to_chrome_path(shim, monkeypatch, routed):
    rc = _run(shim, monkeypatch, "-m", "aizu.worker.chrome_path")
    assert routed == [("chrome_path", [])]
    assert rc == _RC_CHROME_PATH


def test_unknown_module_still_falls_through_to_the_sidecar(shim, monkeypatch, routed):
    rc = _run(shim, monkeypatch, "-m", "aizu.worker.not_a_module", "--whatever")
    assert routed == [("sidecar", None)]
    assert rc == _RC_SIDECAR


def test_unknown_flag_still_falls_through_to_the_sidecar(shim, monkeypatch, routed):
    assert _run(shim, monkeypatch, "--serve") == _RC_SIDECAR
    assert routed == [("sidecar", None)]


def test_a_bare_dash_m_falls_through_instead_of_crashing(shim, monkeypatch, routed):
    """`-m` with no module name must not IndexError out of the entry point — a traceback
    here is a packaged app that dies before it can report anything."""
    assert _run(shim, monkeypatch, "-m") == _RC_SIDECAR
    assert routed == [("sidecar", None)]


def test_no_argv_boots_the_sidecar(shim, monkeypatch, routed):
    """The normal packaged boot: the app spawns the binary with no arguments at all."""
    assert _run(shim, monkeypatch) == _RC_SIDECAR
    assert routed == [("sidecar", None)]


def test_every_dispatched_module_exists_and_accepts_an_argv_list(shim):
    """A typo in a table KEY is invisible at runtime — it just falls through and boots a
    second sidecar (A2 again), so the keys are pinned against the real modules here."""
    assert set(shim._MODULE_DISPATCH) == {"aizu.worker.job_child", "aizu.worker.chrome_path"}
    for module_name in shim._MODULE_DISPATCH:
        entry = importlib.import_module(module_name).main
        params = list(inspect.signature(entry).parameters)
        assert params[:1] == ["argv"], f"{module_name}.main must take argv first"


# --- the PLAYWRIGHT_BROWSERS_PATH pin ----------------------------------------------------

def test_main_pins_the_browsers_path_to_the_per_user_cache(shim, monkeypatch, routed):
    """Without this the frozen binary resolves Chrome for Testing inside its OWN bundle
    (`_internal/playwright/driver/package/.local-browsers/`), which `sidecar.spec` never
    populates — so a packaged box can never find a browser, however full its real cache is."""
    _run(shim, monkeypatch)
    assert os.environ[_BROWSERS_PATH] == shim._default_playwright_browsers_path()


def test_an_operator_or_ops_pin_still_wins(shim, monkeypatch, routed):
    """`os.environ.setdefault`, not assignment: a deployment that points every box at a
    shared read-only browser volume must not have that quietly overwritten by ours."""
    monkeypatch.setenv(_BROWSERS_PATH, "/srv/shared/ms-playwright")
    _run(shim, monkeypatch)
    assert os.environ[_BROWSERS_PATH] == "/srv/shared/ms-playwright"


def test_the_pin_lands_before_the_dispatched_entry_runs(shim, monkeypatch):
    """ORDERING IS THE WHOLE POINT. `-m aizu.worker.chrome_path --install` spawns the
    Playwright driver, which reads this var; a pin applied after the entry point returns
    would be a no-op that still passes a "was it set?" assertion."""
    seen = {}
    monkeypatch.setattr(chrome_path, "main",
                        lambda argv: seen.setdefault("at_entry", os.environ.get(_BROWSERS_PATH)))
    _run(shim, monkeypatch, "-m", "aizu.worker.chrome_path", "--install")
    assert seen["at_entry"] == shim._default_playwright_browsers_path()


def test_the_sidecar_boot_gets_the_pin_too(shim, monkeypatch):
    """A leased harvest job drives a real browser through the same Playwright, so the plain
    no-argv boot needs the pin exactly as much as the chrome_path probe does."""
    seen = {}
    monkeypatch.setattr(sidecar, "main",
                        lambda *a, **kw: seen.setdefault("at_entry", os.environ.get(_BROWSERS_PATH)))
    _run(shim, monkeypatch)
    assert seen["at_entry"] == shim._default_playwright_browsers_path()


def test_importing_the_shim_does_not_touch_the_environment(shim):
    """The `shim` fixture has already exec'd the module. Import side effects would make this
    file's own isolation a lie, and would fire in any tool that merely inspects the script."""
    assert _BROWSERS_PATH not in os.environ


# `~` and os.sep resolved on the HOST, so these expectations are exact on macOS, Linux and a
# Windows dev box alike — a tail/`in` assertion would let a wrong base directory through.
_HOME = os.path.expanduser("~")


@pytest.mark.parametrize("platform,env,expected", [
    ("darwin", {}, os.path.join(_HOME, "Library", "Caches", "ms-playwright")),
    ("linux", {}, os.path.join(_HOME, ".cache", "ms-playwright")),
    ("linux", {"XDG_CACHE_HOME": os.path.join(os.sep, "xdg", "cache")},
     os.path.join(os.sep, "xdg", "cache", "ms-playwright")),
    ("win32", {}, os.path.join(_HOME, "AppData", "Local", "ms-playwright")),
    ("win32", {"LOCALAPPDATA": os.path.join(os.sep, "local", "appdata")},
     os.path.join(os.sep, "local", "appdata", "ms-playwright")),
])
def test_the_default_cache_dir_mirrors_playwrights_own_per_os_rule(
        shim, monkeypatch, platform, env, expected):
    """Mirrored from `driver/package/lib/coreBundle.js`'s `defaultCacheDirectory`, XDG_CACHE_HOME
    and LOCALAPPDATA included. Approximating it would put the frozen binary and a dev tree's
    `playwright install chromium` in DIFFERENT caches on exactly the boxes that set those vars —
    a 356 MB download the operator already paid for, invisible, and a red `cdp_attachable` at
    step 5 that no amount of re-downloading fixes."""
    monkeypatch.setattr(sys, "platform", platform)
    for key in ("XDG_CACHE_HOME", "LOCALAPPDATA"):
        monkeypatch.delenv(key, raising=False)
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    assert shim._default_playwright_browsers_path() == expected
