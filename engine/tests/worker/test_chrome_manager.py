"""Managed Chrome lifecycle (chrome_manager.py) — all with fakes, no real Chrome."""
from __future__ import annotations

import re
from pathlib import Path

import pytest

from aizu.worker.chrome_manager import (BRAND_CHROME, BRAND_CHROME_FOR_TESTING,
                                             LEGACY_PROFILE_DIR_NAME,
                                             ChromeBinaryNotFoundError,
                                             ChromeLaunchError, ChromeManager,
                                             ChromeManagerConfig, ChromeUnhealthyError,
                                             _build_launch_args, brand_of_binary,
                                             config_from_env, has_legacy_profile,
                                             legacy_profile_note, profile_dir_for,
                                             resolve_chrome_binary)


class FakeHandle:
    def __init__(self):
        self.terminated = False
        self.killed = False
        self._alive = True

    @property
    def pid(self):
        return 999

    def poll(self):
        return None if self._alive else 0

    def terminate(self):
        self.terminated = True
        self._alive = False

    def kill(self):
        self.killed = True
        self._alive = False


def _cfg(tmp_path, **kw) -> ChromeManagerConfig:
    base = dict(cdp_url="http://127.0.0.1:9333", profile_base_dir=tmp_path / "base")
    base.update(kw)
    return ChromeManagerConfig(**base)


def _mgr(cfg, *, http, cdp_seq, **kw) -> tuple:
    """cdp_seq is a list of successive cdp_prober return values."""
    launched = {"argv": None, "handle": None}
    seq = list(cdp_seq)

    def prober(url, timeout):
        return seq.pop(0) if seq else (cdp_seq[-1] if cdp_seq else False)

    def launcher(argv):
        launched["argv"] = argv
        launched["handle"] = FakeHandle()
        return launched["handle"]

    mgr = ChromeManager(cfg, launcher=launcher, cdp_prober=prober,
                        http_probe=lambda u, t: http, sleep=lambda s: None,
                        path_exists=lambda p: True, **kw)
    return mgr, launched


def test_reconnects_when_cdp_already_attaches(tmp_path):
    mgr, launched = _mgr(_cfg(tmp_path), http=True, cdp_seq=[True])
    status = mgr.ensure_running()
    assert status.state == "attached_existing"
    assert status.launched_by_us is False
    assert launched["argv"] is None            # never launched a second Chrome


def test_launches_when_nothing_listening(tmp_path):
    # http probe fails → launch → cdp attaches after one poll.
    mgr, launched = _mgr(_cfg(tmp_path, chrome_binary="/fake/chrome"),
                         http=False, cdp_seq=[False, True])
    status = mgr.ensure_running()
    assert status.state == "launched" and status.launched_by_us is True
    assert "--remote-debugging-port=9333" in launched["argv"]
    assert any("--user-data-dir=" in a for a in launched["argv"])


def test_stale_unhealthy_chrome_raises_and_never_launches(tmp_path):
    mgr, launched = _mgr(_cfg(tmp_path), http=True, cdp_seq=[False])
    with pytest.raises(ChromeUnhealthyError):
        mgr.ensure_running()
    assert launched["argv"] is None            # must not kill/relaunch someone else's


def test_launch_timeout_raises(tmp_path):
    mgr, _ = _mgr(_cfg(tmp_path, chrome_binary="/fake/chrome", launch_timeout_sec=1.0,
                       launch_poll_interval_sec=0.5),
                  http=False, cdp_seq=[False])
    with pytest.raises(ChromeLaunchError):
        mgr.ensure_running()


def test_stop_is_noop_when_only_attached(tmp_path):
    mgr, _ = _mgr(_cfg(tmp_path), http=True, cdp_seq=[True])
    mgr.ensure_running()
    mgr.stop()  # must not raise; nothing we launched


def test_stop_terminates_process_we_launched(tmp_path):
    mgr, launched = _mgr(_cfg(tmp_path, chrome_binary="/fake/chrome"),
                         http=False, cdp_seq=[True])
    mgr.ensure_running()
    mgr.stop()
    assert launched["handle"].terminated is True


def test_config_rejects_empty_profile(tmp_path):
    with pytest.raises(ValueError):
        ChromeManagerConfig(cdp_url="http://127.0.0.1:9333", profile_base_dir=Path(""))


def test_config_rejects_unparseable_port():
    with pytest.raises(ValueError):
        ChromeManagerConfig(cdp_url="not-a-url", profile_base_dir=Path("/tmp/p"))


def test_build_launch_args_includes_flags_and_extras(tmp_path):
    cfg = _cfg(tmp_path, extra_flags=("--proxy-server=http://p:8080",))
    args = _build_launch_args(cfg, "/bin/chrome")
    assert args[0] == "/bin/chrome"
    assert "--no-first-run" in args and "--no-default-browser-check" in args
    assert args[-1] == "--proxy-server=http://p:8080"


def test_resolve_binary_override_wins(tmp_path):
    cfg = _cfg(tmp_path, chrome_binary="/opt/chrome")
    assert resolve_chrome_binary(cfg, os_name="Linux",
                                 path_exists=lambda p: True) == "/opt/chrome"


def test_resolve_binary_prefers_playwright_cft(tmp_path):
    cfg = _cfg(tmp_path)
    got = resolve_chrome_binary(cfg, os_name="Darwin", path_exists=lambda p: True,
                                playwright_executable_path=lambda: "/pw/cft")
    assert got == "/pw/cft"


def test_resolve_binary_falls_back_to_system(tmp_path):
    cfg = _cfg(tmp_path)
    got = resolve_chrome_binary(cfg, os_name="Darwin", path_exists=lambda p: True,
                                playwright_executable_path=lambda: None)
    assert "Google Chrome" in got


def test_resolve_binary_raises_when_nothing_exists(tmp_path):
    cfg = _cfg(tmp_path)
    with pytest.raises(ChromeBinaryNotFoundError):
        resolve_chrome_binary(cfg, os_name="Linux", path_exists=lambda p: False)


def test_focus_dispatches_by_os(tmp_path):
    calls = {"mac": 0}

    def mac():
        calls["mac"] += 1
        return True
    mgr = ChromeManager(_cfg(tmp_path), os_name=lambda: "Darwin", focus_macos=mac)
    assert mgr.focus_window() is True and calls["mac"] == 1


def test_focus_unsupported_os_returns_false(tmp_path):
    mgr = ChromeManager(_cfg(tmp_path), os_name=lambda: "Plan9")
    assert mgr.focus_window() is False   # logged no-op, never raises




# ---- the profile directory is DERIVED from the brand (cross-brand launches DELETE) ----
#
# Proven live on a CLONE of a warmed profile: pointing Chrome for Testing at a profile
# system Google Chrome warmed took it from 18 cookies to 0 (Instagram `sessionid`
# included), because the two brands use different macOS keychain items and Chrome deletes
# what it cannot decrypt. `resolve_chrome_binary` PREFERS Chrome for Testing, so the
# helpful default is exactly the one that wipes an operator's sessions.
#
# Three rounds tried to let two brands share one directory safely — a marker file, a
# decision table, a refusal, an operator declaration — and each fixed the last one's hole
# and opened a new one. These tests pin the shape that replaced all of it: the DIRECTORY is
# a function of the brand (`<base>/<brand>`), so there is nothing left to get wrong. Shared
# contract with desktop/src-tauri/src/chrome_manager.rs and scripts/warm_chrome.sh.

CFT_BINARY = ("/pw/chromium-1234/chrome-mac-arm64/Google Chrome for Testing.app"
              "/Contents/MacOS/Google Chrome for Testing")
SYSTEM_BINARY = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"


def _profile_arg(argv: list) -> str:
    """The --user-data-dir the launch actually used. Read off the real argv, never
    recomputed — a test that recomputes it cannot catch a call site that passed the base
    straight through."""
    return [a for a in argv if a.startswith("--user-data-dir=")][0].split("=", 1)[1]


def _legacy(base: Path) -> Path:
    """A profile from before the split: a `Default` directory sitting DIRECTLY in the base,
    with a file in it that nothing in this module may ever touch."""
    (base / LEGACY_PROFILE_DIR_NAME).mkdir(parents=True)
    (base / LEGACY_PROFILE_DIR_NAME / "Cookies").write_bytes(b"warmed-by-someone")
    return base


def test_brand_token_names_chrome_for_testing_by_binary_path():
    assert brand_of_binary(CFT_BINARY) == "chrome-for-testing"
    assert brand_of_binary("/x/CHROME FOR TESTING/chrome") == "chrome-for-testing"


def test_brand_token_names_everything_else_plain_chrome():
    assert brand_of_binary(SYSTEM_BINARY) == "chrome"


def test_distro_chromium_is_its_own_brand_not_a_spelling_of_chrome():
    """Debian/Ubuntu chromium seals cookies under its OWN keyring entry, so it is a third
    brand. Filing it under `chrome` — as the first two-token rule did — hands
    /usr/bin/chromium and /usr/bin/google-chrome the same directory, and whichever warmed it
    first loses every login the moment the other one opens it. Both are on the Linux
    fallback list, so it is not a hypothetical pairing."""
    assert brand_of_binary("/usr/bin/chromium") == "chromium"
    assert brand_of_binary("/usr/bin/chromium-browser") == "chromium"
    assert brand_of_binary("/usr/lib/chromium/chromium") == "chromium"
    assert brand_of_binary(r"C:\tools\chromium.exe") == "chromium"
    # …and it must not swallow Google Chrome, whose leaf is `chrome`/`google-chrome`.
    assert brand_of_binary("/usr/bin/google-chrome") == "chrome"
    assert brand_of_binary("/usr/bin/google-chrome-stable") == "chrome"
    # Playwright's cache still wins: rule 2 runs first, so a CfT build whose leaf happens to
    # be `chrome` inside chromium-1234/ is Chrome for Testing, not chromium.
    assert brand_of_binary(
        "/x/ms-playwright/chromium-1234/chrome-linux64/chrome") == "chrome-for-testing"


# The REAL layouts Playwright installs Chrome for Testing into, taken from the installed
# driver's own EXECUTABLE_PATHS table (playwright/driver/package/lib/coreBundle.js) rather
# than guessed:
#
#   linux-x64    chrome-linux64/chrome
#   linux-arm64  chrome-linux/chrome                        <- "non-cft build", per the table
#   mac-x64      chrome-mac-x64/Google Chrome for Testing.app/Contents/MacOS/...
#   mac-arm64    chrome-mac-arm64/Google Chrome for Testing.app/Contents/MacOS/...
#   win-x64      chrome-win64/chrome.exe
#
# ...under `<browsers cache>/<name>-<revision>` (registry.ts builds that directory as the
# browser name with "-" replaced by "_", then the revision — hence `chromium-1234` and
# `chromium_headless_shell-1234`, both present in this machine's ~/Library/Caches/ms-playwright).
#
# The friendly name "Google Chrome for Testing" exists ONLY in the two macOS bundles. On
# Linux and Windows the binary is a bare `chrome`/`chrome.exe`, so a name-only rule labels
# Chrome for Testing "chrome" there — and it would then be launched into the directory
# system Chrome owns, which is the loss. These are the cases that catch that.

PW_LINUX_X64 = "/root/.cache/ms-playwright/chromium-1234/chrome-linux64/chrome"
PW_LINUX_ARM64 = "/root/.cache/ms-playwright/chromium-1234/chrome-linux/chrome"
PW_WIN_X64 = (r"C:\Users\op\AppData\Local\ms-playwright\chromium-1234"
              r"\chrome-win64\chrome.exe")
PW_MAC_X64 = ("/Users/op/Library/Caches/ms-playwright/chromium-1234/chrome-mac-x64"
              "/Google Chrome for Testing.app/Contents/MacOS/Google Chrome for Testing")
PW_HEADLESS_SHELL = ("/root/.cache/ms-playwright/chromium_headless_shell-1234"
                     "/chrome-headless-shell-linux64/chrome-headless-shell")


@pytest.mark.parametrize("binary", [PW_LINUX_X64, PW_LINUX_ARM64, PW_WIN_X64,
                                    PW_MAC_X64, PW_HEADLESS_SHELL, CFT_BINARY])
def test_playwrights_own_install_layout_is_chrome_for_testing_on_every_platform(binary):
    """Rule 2 of the shared contract. This is the DEFECT the name-only rule shipped with:
    on linux-x64, linux-arm64 and win-x64 the CfT binary is called `chrome`, so a Linux box
    would send Chrome for Testing into `<base>/chrome` — the directory system Chrome owns —
    and empty it."""
    assert brand_of_binary(binary) == "chrome-for-testing"


@pytest.mark.parametrize("binary", [
    "/usr/bin/google-chrome",
    "/usr/bin/google-chrome-stable",
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    "/opt/chromium-1234-beta/chrome",      # segment does not END at the digits
    "/opt/mychromium-1234/chrome",         # ...nor START at "chromium"
    "/opt/chromium-nightly/chrome",        # no revision number at all
    "/opt/chromium_headless_shell/chrome",  # ditto, headless-shell spelling
    "", None,
])
def test_anything_outside_that_layout_is_the_system_browser(binary):
    """The Chrome fallback, and the anchoring that makes rule 2 safe. These paths carry a
    `chromium`-ish string but none of them is Playwright's `chromium-<digits>` cache
    directory, so an unanchored substring match would split one browser's profile across two
    directories and lose its logins that way instead.
    (Distro chromium itself is NOT here: it has its own brand — see
    test_distro_chromium_is_its_own_brand_not_a_spelling_of_chrome. Every leaf below is a
    `chrome`/`chrome.exe`/`Google Chrome`.)"""
    assert brand_of_binary(binary) == "chrome"


def test_windows_paths_are_matched_with_either_separator():
    """The contract normalises backslashes before matching, so a Windows path parsed on a
    POSIX box (a log line, a config file copied between machines) reads identically. All
    three components do this, so they agree on the directory."""
    forward = PW_WIN_X64.replace("\\", "/")
    assert brand_of_binary(forward) == brand_of_binary(PW_WIN_X64) == "chrome-for-testing"


def test_brand_detection_is_case_insensitive_on_both_rules():
    assert brand_of_binary(CFT_BINARY.upper()) == "chrome-for-testing"
    assert brand_of_binary(PW_LINUX_X64.upper()) == "chrome-for-testing"


def test_a_symlink_to_chrome_for_testing_resolves_before_the_rules_run(tmp_path):
    """A wrapper symlink is how a Chrome-for-Testing binary reaches rule 3: `/usr/local/bin/
    chrome` -> Playwright's build looks like a system Chrome by name, and would be launched
    into `<base>/chrome`, on top of the profile the real system Chrome warmed. Resolve
    first, judge after."""
    real = tmp_path / "chromium-1234" / "chrome-linux64"
    real.mkdir(parents=True)
    (real / "chrome").write_text("#!/bin/sh\n")
    link = tmp_path / "chrome"
    link.symlink_to(real / "chrome")
    assert brand_of_binary(str(link)) == "chrome-for-testing"


def test_a_path_that_is_not_on_disk_is_judged_exactly_as_written(tmp_path, monkeypatch):
    """Only an EXISTING path is resolved. `realpath` on a missing or foreign-platform path
    prepends the CURRENT WORKING DIRECTORY, so an unconditional resolve would let the cwd
    decide the brand — here a cwd that merely looks like Playwright's cache would send
    system Chrome into the Chrome-for-Testing directory."""
    cwd = tmp_path / "chromium-9999"
    cwd.mkdir()
    monkeypatch.chdir(cwd)
    assert brand_of_binary("/nowhere/google-chrome") == "chrome"
    assert brand_of_binary(r"C:\Program Files\Google\Chrome\chrome.exe") == "chrome"


def test_profile_dir_is_the_base_plus_the_brand():
    """The whole redesign in one assertion. The path IS the ownership record: two brands
    cannot reach one directory, so there is no marker to read and no question to answer."""
    assert profile_dir_for("/data/prof", BRAND_CHROME) == Path("/data/prof/chrome")
    assert profile_dir_for(Path("/data/prof"), BRAND_CHROME_FOR_TESTING) == \
        Path("/data/prof/chrome-for-testing")


def test_launch_opens_the_directory_that_belongs_to_the_binarys_brand(tmp_path):
    base = tmp_path / "base"
    mgr, launched = _mgr(_cfg(tmp_path, chrome_binary=SYSTEM_BINARY, profile_base_dir=base),
                         http=False, cdp_seq=[True])
    assert mgr.ensure_running().state == "launched"
    assert _profile_arg(launched["argv"]) == str(base / "chrome")


def test_the_two_brands_never_land_in_one_directory(tmp_path):
    """THE property. One base, both browsers, and the thing that destroyed a real profile
    — two brands opening one directory — is not reachable from here."""
    base = tmp_path / "base"
    seen = set()
    for binary in (SYSTEM_BINARY, CFT_BINARY, PW_LINUX_X64, PW_WIN_X64):
        args = _build_launch_args(_cfg(tmp_path, profile_base_dir=base), binary)
        seen.add(_profile_arg(args))
    assert seen == {str(base / "chrome"), str(base / "chrome-for-testing")}


def test_a_launch_never_points_chrome_at_the_bare_base(tmp_path):
    """The regression that would silently reinstate the old failure: a call site handing
    `profile_base_dir` to --user-data-dir puts both brands back in one directory, and
    nothing else in this module would notice."""
    base = tmp_path / "base"
    for binary in (SYSTEM_BINARY, CFT_BINARY):
        args = _build_launch_args(_cfg(tmp_path, profile_base_dir=base), binary)
        assert _profile_arg(args) != str(base)
        assert Path(_profile_arg(args)).parent == base


def test_attaching_to_a_running_chrome_picks_no_directory_at_all(tmp_path):
    """Attaching opens nothing, so it cannot cross brands and must never be gated."""
    base = _legacy(tmp_path / "base")
    mgr, launched = _mgr(_cfg(tmp_path, chrome_binary=CFT_BINARY, profile_base_dir=base),
                         http=True, cdp_seq=[True])
    assert mgr.ensure_running().state == "attached_existing"
    assert launched["argv"] is None


# ---- the legacy profile: reported once, never touched ----

def test_a_legacy_profile_is_recognised_only_at_the_base(tmp_path):
    """`Default` directly in the base is a pre-split profile. The same folder one level
    down is the NEW layout working correctly, and calling that legacy would nag every
    healthy box forever."""
    base = tmp_path / "base"
    assert has_legacy_profile(base) is False          # missing entirely
    base.mkdir()
    assert has_legacy_profile(base) is False          # fresh, never launched
    (base / "chrome" / LEGACY_PROFILE_DIR_NAME).mkdir(parents=True)
    assert has_legacy_profile(base) is False          # the new layout, warmed
    _legacy(base)
    assert has_legacy_profile(base) is True


def test_a_legacy_profile_never_blocks_a_launch_and_is_left_exactly_as_it_was(tmp_path):
    """It is inert: the launch goes into `<base>/<brand>` beside it. Nothing here moves,
    copies, renames, deletes or opens it — the cookies in it are the operator's, and three
    rounds of guessing at them is what got us here."""
    base = _legacy(tmp_path / "base")
    before = sorted(p.name for p in base.iterdir())
    mgr, launched = _mgr(_cfg(tmp_path, chrome_binary=CFT_BINARY, profile_base_dir=base),
                         http=False, cdp_seq=[True])
    assert mgr.ensure_running().state == "launched"
    assert _profile_arg(launched["argv"]) == str(base / "chrome-for-testing")
    assert sorted(p.name for p in base.iterdir()) == before
    assert (base / LEGACY_PROFILE_DIR_NAME / "Cookies").read_bytes() == b"warmed-by-someone"


def test_the_legacy_note_names_both_destinations_and_guesses_neither(tmp_path):
    """The operator has to pick, because we cannot: naming one brand as the likely owner is
    the mistake that emptied a real profile. Both candidate paths are spelled in full so
    the move is a copy-paste.

    Note the boundary on the `<base>/chrome` match. `<base>/chrome-for-testing` CONTAINS
    `<base>/chrome`, so a plain `in` check is satisfied by a note that offers only the
    Chrome-for-Testing destination — i.e. by exactly the guess this test exists to forbid."""
    base = tmp_path / "base"
    note = legacy_profile_note(base)
    assert str(base / "chrome-for-testing") in note
    assert re.search(re.escape(str(base / "chrome")) + r"(?![-\w])", note)
    # ...and both destinations are named as a browser a human can recognise, not as a token.
    assert "Chrome for Testing" in note and "system Google Chrome" in note
    assert "DELETES" in note and "login" in note
    assert "left EXACTLY as it was" in note
    assert "only you can know" in note


def test_the_legacy_note_is_logged_on_the_launch_that_steps_around_it(tmp_path, caplog):
    """A worker box has no wizard and nobody can SSH into it (F12). The launch log is one
    of the two places this can reach a human; the preflight row is the other."""
    base = _legacy(tmp_path / "base")
    mgr, _ = _mgr(_cfg(tmp_path, chrome_binary=SYSTEM_BINARY, profile_base_dir=base),
                  http=False, cdp_seq=[True])
    with caplog.at_level("WARNING", logger="aizu.worker.chrome_manager"):
        mgr.ensure_running()
    assert any(str(base / "chrome-for-testing") in r.message for r in caplog.records)


def test_a_clean_base_says_nothing_about_legacy_profiles(tmp_path, caplog):
    base = tmp_path / "base"
    mgr, _ = _mgr(_cfg(tmp_path, chrome_binary=SYSTEM_BINARY, profile_base_dir=base),
                  http=False, cdp_seq=[True])
    with caplog.at_level("WARNING", logger="aizu.worker.chrome_manager"):
        mgr.ensure_running()
    assert not [r for r in caplog.records if "pre-split" in r.message]


# ---- one env-var spelling, one default, repo-wide ----

def test_config_from_env_reads_the_names_the_shipped_launcher_writes(monkeypatch):
    """The split this replaces: `chrome_manager` read AIZU_CHROME_PROFILE_DIR defaulting to
    ~/.aizu-chrome-profile, while `scripts/warm_chrome.sh` — the launcher the docs tell an
    operator to run — warmed AIZU_CHROME_PROFILE defaulting to ~/.aizu-cft-profile. So the
    preflight's profile row inspected a directory nothing on the box ever warmed. One name,
    one default; the shell script and the desktop shell read the same two."""
    monkeypatch.delenv("AIZU_CHROME_PROFILE", raising=False)
    monkeypatch.delenv("CHROME_BIN", raising=False)
    cfg = config_from_env("http://127.0.0.1:9333")
    assert cfg.profile_base_dir == Path.home() / ".aizu-cft-profile"
    assert cfg.chrome_binary is None

    monkeypatch.setenv("AIZU_CHROME_PROFILE", "/data/prof")
    monkeypatch.setenv("CHROME_BIN", "/opt/chrome")
    cfg = config_from_env("http://127.0.0.1:9333")
    assert cfg.profile_base_dir == Path("/data/prof")
    assert cfg.chrome_binary == "/opt/chrome"


def test_the_retired_profile_spelling_is_gone_not_merely_unread(monkeypatch):
    """AIZU_CHROME_PROFILE_DIR is dead, not deprecated: no component reads it. An alias for
    the profile base that only ONE of the three launchers honours is how an operator ends
    up with two profiles and no idea which one their logins are in."""
    monkeypatch.delenv("AIZU_CHROME_PROFILE", raising=False)
    monkeypatch.setenv("AIZU_CHROME_PROFILE_DIR", "/data/retired")
    assert config_from_env("http://127.0.0.1:9333").profile_base_dir == \
        Path.home() / ".aizu-cft-profile"


def test_both_binary_spellings_are_honoured_because_the_other_launchers_honour_both(
        monkeypatch):
    """Two names, one knob — the same pair, in the same precedence, as warm_chrome.sh and
    the desktop shell. The binary now chooses the profile DIRECTORY, so a box where the
    launcher obeys an override and the worker ignores it warms one directory and harvests
    another; that is the whole failure, wearing a different hat."""
    monkeypatch.delenv("CHROME_BIN", raising=False)
    monkeypatch.setenv("AIZU_CHROME_BINARY", "/opt/aizu-spelling")
    assert config_from_env("http://127.0.0.1:9333").chrome_binary == "/opt/aizu-spelling"

    # AIZU_CHROME_BINARY wins when both are set: it is ours and unambiguous, while
    # CHROME_BIN is a generic name other tooling on the box may set for its own reasons.
    # The ORDER is the contract — bash used to read AIZU_CHROME_BINARY first while this and
    # the Rust read CHROME_BIN first, so a box with both set warmed <base>/chrome-for-testing
    # and harvested <base>/chrome.
    monkeypatch.setenv("CHROME_BIN", "/opt/legacy-spelling")
    assert config_from_env("http://127.0.0.1:9333").chrome_binary == "/opt/aizu-spelling"

    monkeypatch.delenv("AIZU_CHROME_BINARY")
    assert config_from_env("http://127.0.0.1:9333").chrome_binary == "/opt/legacy-spelling"


def test_the_marker_guard_is_deleted_not_deprecated():
    """Three rounds of marker/decision-table/refusal/declaration are GONE, and a leftover
    helper is how the next round rebuilds them: the directory is the only record now."""
    import aizu.worker.chrome_manager as cm
    for name in ("BRAND_MARKER_FILENAME", "read_profile_brand", "write_profile_brand",
                 "guard_profile_brand", "profile_looks_used",
                 "ChromeProfileBrandMismatchError"):
        assert not hasattr(cm, name), f"{name} survived the redesign"
