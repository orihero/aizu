"""The Chrome-for-Testing path query and download (chrome_path.py) — all with fakes, no
real Playwright and no 356 MB download.

The contract this file pins is a WIRE contract: the desktop shell reads STDOUT ONLY and
trusts it only when the exit code is 0. So each test asserts on the two channels separately,
because collapsing them is precisely how the shell would end up launching a path that is
really a sentence, or treating Playwright's shutdown noise as a failure.

`--install` makes that split harder to keep, not easier, and that is what most of the
install tests are about: the bundled Playwright driver writes its download progress AND its
errors to its OWN STDOUT (verified live — a failed `install` put "Failed to install
browsers" on stdout and left stderr empty), so every byte of the driver stream has to be
moved onto our stderr before it can be mistaken for a browser path.

One `@pytest.mark.slow` test spawns a REAL subprocess, mirroring `test_job_child.py`: the
stdout/stderr split only means anything across a genuine process boundary.
"""
from __future__ import annotations

import subprocess
import sys

import pytest

from aizu.worker import chrome_path

# What Playwright hands back on a box that never ran `playwright install chromium`: a
# well-formed path to a file that does not exist (it is a dict read, not a stat).
_PHANTOM = ("/Users/x/Library/Caches/ms-playwright/chromium-1234/chrome-mac-arm64/"
            "Google Chrome for Testing.app/Contents/MacOS/Google Chrome for Testing")


class _FakeDriver:
    """The bundled Playwright Node driver, as `install` actually uses it: a line-iterable
    stdout (both progress and errors arrive there) plus an exit code."""

    def __init__(self, lines=(), code=0):
        self.stdout = iter(lines)
        self._code = code

    def wait(self) -> int:
        return self._code


def _spawner(driver, calls: list):
    """A `subprocess.Popen` stand-in that records how it was called."""
    def spawn(argv, **kwargs):
        calls.append((list(argv), kwargs))
        return driver
    return spawn


def _install(driver, *, calls=None, resolved=("/pw/cft", None), progress=None,
             invocation=None):
    """`install` with every effect faked. Returns whatever install returns."""
    return chrome_path.install(
        driver_invocation=invocation or (lambda: (["node", "cli.js", "install"], {"E": "1"})),
        spawn=_spawner(driver, calls if calls is not None else []),
        progress=progress if progress is not None else (lambda line: None),
        resolve_installed=lambda: resolved)


# --- resolution -------------------------------------------------------------------------

def test_resolve_returns_an_existing_binary(tmp_path):
    cft = tmp_path / "Google Chrome for Testing"
    cft.write_text("#!/bin/sh\n", encoding="utf-8")
    assert chrome_path.resolve(executable_path=lambda: str(cft)) == (str(cft), None)


def test_resolve_rejects_a_path_playwright_never_downloaded():
    """THE trap: `pw.chromium.executable_path` returns this string without complaint on a
    box with an empty ms-playwright cache, and Playwright only notices at launch time.
    Forwarding it would hand the shell a dead binary — the same stuck-wizard dead end the
    system-Chrome fallthrough already produces, just by a new route."""
    path, remedy = chrome_path.resolve(executable_path=lambda: _PHANTOM,
                                       path_exists=lambda p: False)
    assert path is None
    assert _PHANTOM in remedy


def test_the_not_installed_remedy_names_the_in_app_action_not_a_pip_world_command():
    """This line used to say "Run `playwright install chromium`" — unreachable advice on the
    ONE box that ever sees it, which has no interpreter and no pip (that is why this module
    exists at all). It must name the action the operator can actually click, and the size,
    since `--install` is a 356 MB commitment."""
    _, remedy = chrome_path.resolve(executable_path=lambda: _PHANTOM,
                                    path_exists=lambda p: False)
    assert "playwright install chromium" not in remedy
    assert "Download browser" in remedy
    assert "356 MB" in remedy
    assert remedy.count("\n") == 0          # one line, always


def test_resolve_reports_a_missing_playwright():
    """`_default_playwright_executable_path` swallows every failure into None, so this one
    branch covers both 'not importable' and 'the driver refused to start'."""
    path, remedy = chrome_path.resolve(executable_path=lambda: None)
    assert path is None and "Playwright is unavailable" in remedy


def test_resolve_treats_an_empty_string_as_unresolved():
    assert chrome_path.resolve(executable_path=lambda: "")[0] is None


# --- install ----------------------------------------------------------------------------

def test_install_returns_the_reresolved_path_not_the_drivers_own_claim():
    """The driver prints its own "downloaded to <dir>" line, and we deliberately ignore it:
    the resolved path has exactly one owner (`resolve`), and only that owner stats it."""
    lines = ["Chromium downloaded to /somewhere/else"]
    assert _install(_FakeDriver(lines), resolved=("/pw/cft", None)) == ("/pw/cft", None)


def test_install_moves_every_driver_line_onto_the_progress_sink():
    """The driver's progress goes to its stdout; ours is reserved for the path. If this
    forwarding ever stops, a progress bar becomes a candidate Chrome binary path."""
    seen = []
    _install(_FakeDriver(["Downloading Chromium\n", "| 50% of 356 MiB\n"]), progress=seen.append)
    assert "Downloading Chromium" in seen
    assert "| 50% of 356 MiB" in seen


def test_install_folds_the_driver_stderr_into_the_stream_it_reads():
    """A failed install puts its diagnosis on the driver's STDOUT, but anything that ever
    lands on its stderr must reach the operator too — not a pipe nobody drains (which is
    also how a 356 MB download deadlocks on a full pipe buffer)."""
    calls = []
    _install(_FakeDriver(), calls=calls)
    _, kwargs = calls[0]
    assert kwargs["stderr"] is subprocess.STDOUT
    assert kwargs["stdout"] is subprocess.PIPE


def test_install_decodes_the_driver_stream_without_trusting_the_locale():
    """The progress bar is made of U+2588 blocks. A bare `text=True` decodes with the box's
    locale, so a minimal Linux/CI image whose locale is POSIX would raise UnicodeDecodeError
    halfway through a 356 MB download — a lost download to save a mangled bar."""
    calls = []
    _install(_FakeDriver(), calls=calls)
    _, kwargs = calls[0]
    assert kwargs["encoding"] == "utf-8" and kwargs["errors"] == "replace"


def test_install_turns_a_broken_driver_stream_into_a_remedy_line():
    """`install` promises never to raise: a traceback out of the packaged app's Download
    button is unreadable, and it would land on the same stderr the wizard renders as
    progress."""
    class _DyingDriver:
        stdout = iter(["| 10% of 356 MiB\n"])

        def __iter__(self):
            raise OSError("read failed")

        def wait(self):
            raise AssertionError("unreachable")

    driver = _DyingDriver()
    driver.stdout = driver
    path, remedy = _install(driver)
    assert path is None and "OSError" not in remedy and remedy.count("\n") == 0
    assert "downloading Chrome for Testing failed" in remedy


def test_install_strips_the_ansi_escapes_the_driver_emits():
    """The driver dims its "Downloading X from <url>" line with an SGR pair. The wizard
    renders these as TEXT, not in a terminal, so the escapes would show up as literal
    `[2m` in the operator's progress log."""
    seen = []
    _install(_FakeDriver(["Downloading \x1b[2mfrom https://cdn.playwright.dev\x1b[22m\n"]),
             progress=seen.append)
    assert "Downloading from https://cdn.playwright.dev" in seen
    assert not any("\x1b" in line for line in seen)


def test_install_announces_itself_before_the_driver_says_anything():
    """On a box that ALREADY has the browser the driver exits 0 in ~0.1s having printed
    nothing at all (verified). Without a framing line of our own the wizard would show an
    empty progress log and no evidence the button did anything."""
    seen = []
    _install(_FakeDriver(), progress=seen.append)
    assert "356 MB" in seen[0]


def test_install_reports_a_nonzero_driver_exit_with_no_path():
    path, remedy = _install(_FakeDriver(["Failed to install browsers"], code=1))
    assert path is None
    assert "exited 1" in remedy and remedy.count("\n") == 0


def test_install_reports_a_driver_it_cannot_locate():
    """`compute_driver_executable` raising (a renamed/absent bundled driver) must be a
    remedy line, not a traceback out of a packaged app's install button."""
    def boom():
        raise RuntimeError("no driver")
    path, remedy = chrome_path.install(driver_invocation=boom, progress=lambda line: None)
    assert path is None and "bundled browser driver is unavailable" in remedy


def test_install_reports_a_spawn_that_fails():
    def spawn(argv, **kwargs):
        raise OSError("Exec format error")
    path, remedy = chrome_path.install(
        driver_invocation=lambda: (["node"], {}), spawn=spawn, progress=lambda line: None,
        resolve_installed=lambda: ("/pw/cft", None))
    assert path is None and "bundled browser driver is unavailable" in remedy


def test_install_that_succeeds_but_still_does_not_resolve_blames_the_cache_not_the_operator():
    """Telling the operator "not installed — click Download browser" right after they clicked
    Download browser and it reported success is a loop. The real disagreement is over
    PLAYWRIGHT_BROWSERS_PATH, so the remedy has to say that."""
    path, remedy = _install(_FakeDriver(), resolved=(None, "not installed, click Download"))
    assert path is None
    assert "PLAYWRIGHT_BROWSERS_PATH" in remedy and "Download browser" not in remedy


def test_install_defaults_to_the_module_level_resolve_so_it_can_be_stubbed(monkeypatch):
    """`install` reads the `resolve` global at CALL time — the same seam `main` uses, and
    the reason the rest of this file can stub one function and cover both entry points."""
    monkeypatch.setattr(chrome_path, "resolve", lambda: ("/stubbed/cft", None))
    path, _ = chrome_path.install(
        driver_invocation=lambda: (["node"], {}),
        spawn=_spawner(_FakeDriver(), []), progress=lambda line: None)
    assert path == "/stubbed/cft"


def test_the_default_invocation_drives_the_bundled_driver_for_headed_chrome():
    """Asked of Playwright itself (`compute_driver_executable`), which under PyInstaller
    resolves to `_internal/playwright/driver/{node,package/cli.js}` — the driver the freeze
    already ships and the only one a box with no interpreter can run.

    `--no-shell` is load-bearing: `install chromium` also pulls the headless shell, measured
    at another 196 MB on top of the 356 MB the wizard advertises, and this repo never
    launches it — it attaches over CDP to a HEADED Chrome for Testing."""
    argv, env = chrome_path._default_driver_invocation()
    assert argv[0].endswith(("node", "node.exe"))
    assert argv[1].endswith("cli.js")
    assert argv[2:] == ["install", "chromium", "--no-shell"]
    # get_driver_env() is os.environ.copy() + PW_LANG_*; it must NOT carry _transport's
    # frozen `PLAYWRIGHT_BROWSERS_PATH="0"` forcing, which is what points the driver at the
    # bundle's non-existent .local-browsers/ dir.
    assert env.get("PLAYWRIGHT_BROWSERS_PATH") != "0"


# --- the wire contract ------------------------------------------------------------------

def test_main_prints_only_the_path_on_stdout(monkeypatch, capsys):
    monkeypatch.setattr(chrome_path, "resolve", lambda: ("/pw/cft", None))
    assert chrome_path.main([]) == 0
    out = capsys.readouterr()
    assert out.out == "/pw/cft\n"      # nothing else on the channel the shell parses
    assert out.err == ""


def test_main_keeps_stdout_empty_when_unresolved(monkeypatch, capsys):
    """A remedy line on stdout would be read by the shell as a Chrome binary path."""
    monkeypatch.setattr(chrome_path, "resolve", lambda: (None, "nope, do X"))
    assert chrome_path.main([]) != 0
    out = capsys.readouterr()
    assert out.out == ""
    assert out.err.strip() == "nope, do X"


def test_main_install_answers_in_exactly_the_same_shape(monkeypatch, capsys):
    """`--install` and a bare query are interchangeable to a caller: path on stdout, rc 0.
    That is what lets the Rust side use one parser for both."""
    monkeypatch.setattr(chrome_path, "install", lambda: ("/pw/cft", None))
    assert chrome_path.main(["--install"]) == 0
    out = capsys.readouterr()
    assert out.out == "/pw/cft\n" and out.err == ""


def test_main_install_keeps_stdout_empty_when_the_download_fails(monkeypatch, capsys):
    monkeypatch.setattr(chrome_path, "install", lambda: (None, "download died"))
    assert chrome_path.main(["--install"]) != 0
    out = capsys.readouterr()
    assert out.out == "" and out.err.strip() == "download died"


def test_main_never_downloads_without_the_flag(monkeypatch, capsys):
    """The shell calls the bare form on its BOOT path, inside a 30s Chrome grace. If that
    ever started a 356 MB download the app would hang at launch."""
    monkeypatch.setattr(chrome_path, "resolve", lambda: ("/pw/cft", None))
    monkeypatch.setattr(chrome_path, "install",
                        lambda: pytest.fail("bare invocation must never install"))
    assert chrome_path.main([]) == 0


def test_main_rejects_an_unknown_flag(monkeypatch):
    """argparse exits 2 rather than ignoring the flag and answering with a path anyway —
    a future caller's typo must be visible, not silently successful."""
    monkeypatch.setattr(chrome_path, "resolve", lambda: ("/pw/cft", None))
    with pytest.raises(SystemExit):
        chrome_path.main(["--browser=firefox"])


def test_importing_chrome_path_does_not_import_playwright():
    """This module runs on the desktop shell's BOOT path, under a 30s Chrome grace, so its
    import must stay as cheap as `chrome_manager`'s: Playwright is imported lazily inside
    the resolver. A fresh interpreter is the only honest probe — pytest has already dragged
    Playwright into this process (same reason as `test_config_import_cost.py`)."""
    code = ("import sys; __import__('aizu.worker.chrome_path'); "
            "print('playwright' if 'playwright' in sys.modules else 'clean')")
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True,
                         timeout=90, check=True)
    assert out.stdout.strip() == "clean"


@pytest.mark.slow
def test_real_subprocess_prints_the_path_and_nothing_else(tmp_path):
    """Across a genuine process boundary: stdout is exactly the path, and stderr noise
    (which Playwright's driver teardown really does emit here) never changes the verdict.
    The resolver is stubbed via a test-only module so this stays offline and browser-free."""
    cft = tmp_path / "Google Chrome for Testing"
    cft.write_text("#!/bin/sh\n", encoding="utf-8")
    stub = tmp_path / "stub.py"
    stub.write_text(
        "import sys\n"
        "from aizu.worker import chrome_path\n"
        "chrome_path.resolve = lambda: (%r, None)\n"
        "print('driver teardown noise', file=sys.stderr)\n"
        "sys.exit(chrome_path.main([]))\n" % str(cft),
        encoding="utf-8")
    proc = subprocess.run([sys.executable, str(stub)], capture_output=True, text=True,
                          timeout=60)
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout == str(cft) + "\n"
    assert "noise" in proc.stderr        # present, and deliberately not fatal


@pytest.mark.slow
def test_real_subprocess_install_keeps_the_progress_off_stdout(tmp_path):
    """The install split, across a real process boundary and a real pipe: a driver that
    writes only to its stdout (as the bundled one does) must leave OUR stdout carrying the
    path alone. The driver is a fake script, so nothing is downloaded.

    Note the `functools.partial` rather than a `chrome_path._default_driver_invocation = ...`
    assignment: `install`'s seams are bound as DEFAULT ARGUMENTS (the idiom `resolve` and
    `chrome_manager` already use), so rebinding the module attribute would silently leave the
    REAL bundled driver in play — i.e. a unit test that starts a 356 MB download."""
    cft = tmp_path / "Google Chrome for Testing"
    cft.write_text("#!/bin/sh\n", encoding="utf-8")
    fake_driver = tmp_path / "driver.py"
    fake_driver.write_text(
        "print('| 50% of 356 MiB')\n"
        "print('Chromium downloaded to /wherever')\n",
        encoding="utf-8")
    stub = tmp_path / "stub.py"
    stub.write_text(
        "import functools, sys\n"
        "from aizu.worker import chrome_path\n"
        "chrome_path.install = functools.partial(\n"
        "    chrome_path.install,\n"
        "    driver_invocation=lambda: ([%r, %r], None),\n"
        "    resolve_installed=lambda: (%r, None))\n"
        "sys.exit(chrome_path.main(['--install']))\n"
        % (sys.executable, str(fake_driver), str(cft)),
        encoding="utf-8")
    proc = subprocess.run([sys.executable, str(stub)], capture_output=True, text=True,
                          timeout=60)
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout == str(cft) + "\n"
    assert "50% of 356 MiB" in proc.stderr


# --------------------------------------------------------------- half-installed browsers
#
# A killed download leaves a COMPLETE-LOOKING tree: the launcher exists and stats fine, but
# Playwright itself treats the revision as unusable and re-downloads it. Observed live — a
# killed install left 322 MB on disk with the 52 KB `Google Chrome for Testing` launcher
# present. Accepting that path launches a browser that never opens its CDP port, and the
# operator gets an unrelated remedy about port conflicts.

def _cft_tree(cache, *, complete: bool):
    """Build Playwright's real macOS layout under `cache`: the marker lives in the REVISION
    dir, five levels above the executable."""
    rev = cache / "chromium-1234"
    exe = (rev / "chrome-mac-arm64" / "Google Chrome for Testing.app"
           / "Contents" / "MacOS" / "Google Chrome for Testing")
    exe.parent.mkdir(parents=True)
    exe.write_text("#!/bin/sh\n", encoding="utf-8")
    if complete:
        (rev / "INSTALLATION_COMPLETE").write_text("", encoding="utf-8")
    return exe


def test_a_completed_install_resolves(tmp_path):
    exe = _cft_tree(tmp_path, complete=True)
    path, remedy = chrome_path.resolve(executable_path=lambda: str(exe))
    assert path == str(exe)
    assert remedy is None


def test_a_half_installed_browser_is_refused_with_the_download_remedy(tmp_path):
    exe = _cft_tree(tmp_path, complete=False)
    path, remedy = chrome_path.resolve(executable_path=lambda: str(exe))
    assert path is None
    assert "half-installed" in remedy
    # It must name the ONE action that fixes it, not a shell command a packaged box cannot run.
    assert "Download browser" in remedy


def test_an_unrecognised_layout_is_accepted_rather_than_failed(tmp_path):
    """The asymmetry that matters: we reject only what we can PROVE is half-written. A path
    with no Playwright revision dir above it — an operator's own CHROME_BIN, a future
    Playwright that moves the marker — is accepted. A false red on a healthy box is the
    worst outcome this module has."""
    exe = tmp_path / "some-vendor-chrome" / "chrome"
    exe.parent.mkdir(parents=True)
    exe.write_text("#!/bin/sh\n", encoding="utf-8")
    path, remedy = chrome_path.resolve(executable_path=lambda: str(exe))
    assert path == str(exe)
    assert remedy is None


def test_an_ancestor_named_like_a_revision_dir_does_not_fail_a_good_browser(tmp_path):
    """The first cut of this check matched `<anything>-<digits>` and declared a healthy
    browser half-installed because pytest's own tmpdir (`pytest-188`) was an ancestor. Only
    Playwright's known cache names count."""
    cache = tmp_path / "pytest-188" / "build-42"
    cache.mkdir(parents=True)
    exe = _cft_tree(cache, complete=True)
    path, remedy = chrome_path.resolve(executable_path=lambda: str(exe))
    assert path == str(exe)
    assert remedy is None
