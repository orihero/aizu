"""`scripts/warm_chrome.sh`'s brand→directory derivation, driven directly — no browser.

The profile directory is a FUNCTION of the browser brand: `AIZU_CHROME_PROFILE` names a base
and the launch opens `<base>/chrome` or `<base>/chrome-for-testing`. That is the whole guard
against A9 (a cross-brand open DELETES every cookie in a profile), and it replaces the marker
file + decision table + refusal + operator declaration that rounds 1-3 shipped — see A12. So
what is worth testing here is no longer "does the refusal fire" but "does every path land in
the brand's own directory", plus the one thing that is still a judgement call: the legacy
profile a base may already hold, which we describe and never touch.

The script sources as a library — `AIZU_WARM_CHROME_LIB=1` makes it define its functions and
return before `main` — precisely so this can be exercised without launching anything. The
alternative (running it for real to see what it does to a profile) is the experiment that costs
an operator their logins, which is how A9 was measured and is not repeatable in a test.

`test_bash_and_python_agree_*` are the point of the file: bash and Python must agree
token-for-token, or two launch sites warm two different directories for the same browser and
the operator is asked to sign in again for no visible reason.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import textwrap
from pathlib import Path

import pytest

from aizu.worker.chrome_manager import (BRAND_CHROME, BRAND_CHROME_FOR_TESTING,
                                        brand_of_binary, profile_dir_for)

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "warm_chrome.sh"

pytestmark = pytest.mark.skipif(shutil.which("bash") is None,
                                reason="bash is required to drive warm_chrome.sh")

# Paths the derivation has to classify. Only the first three are visible to the "chrome for
# testing" substring — the rest are the same Playwright build on the platforms where it is
# installed as a bare `chrome`/`chrome.exe`, read off the driver's own EXECUTABLE_PATHS table.
CFT_PATHS = [
    "/Users/o/Library/Caches/ms-playwright/chromium-1234/chrome-mac-arm64/"
    "Google Chrome for Testing.app/Contents/MacOS/Google Chrome for Testing",
    "/opt/Google Chrome for Testing.app/Contents/MacOS/Google Chrome for Testing",
    "/OPT/GOOGLE CHROME FOR TESTING.APP/CONTENTS/MACOS/X",
    "/home/o/.cache/ms-playwright/chromium-1234/chrome-linux64/chrome",
    "/home/o/.cache/ms-playwright/chromium-1234/chrome-linux/chrome",
    r"C:\Users\o\AppData\Local\ms-playwright\chromium-1234\chrome-win64\chrome.exe",
    "/home/o/.cache/ms-playwright/chromium_headless_shell-1234/chrome-linux/headless_shell",
]
CHROME_PATHS = [
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    "/usr/bin/google-chrome-stable",
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    # Near-misses for the anchored segment rule.
    "/home/o/chromium-dev/chrome",
    "/home/o/ms-playwright/chromium-1234x/chrome-linux64/chrome",
    "",
]

CFT_BIN = CFT_PATHS[0]
SYS_BIN = CHROME_PATHS[0]


def _call(func: str, *args: str, cwd: Path | None = None) -> subprocess.CompletedProcess:
    """Source the script as a library and call one of its functions."""
    quoted = " ".join(f'"${{{i + 1}}}"' for i in range(len(args)))
    return subprocess.run(
        ["bash", "-c", f'. "$0"; {func} {quoted}', str(SCRIPT), *args],
        env={"AIZU_WARM_CHROME_LIB": "1", "PATH": "/usr/bin:/bin:/usr/sbin:/sbin",
             "HOME": "/nonexistent-home-for-tests"},
        cwd=str(cwd) if cwd else None,
        capture_output=True, text=True,
    )


def brand_of(binary: str) -> str:
    out = _call("brand_of", binary)
    assert out.returncode == 0, out.stderr
    return out.stdout.strip()


def sh_profile_dir_for(base: str, brand: str) -> str:
    out = _call("profile_dir_for", base, brand)
    assert out.returncode == 0, out.stderr
    return out.stdout.strip()


@pytest.mark.parametrize("path", CFT_PATHS)
def test_brand_of_recognises_chrome_for_testing(path):
    assert brand_of(path) == BRAND_CHROME_FOR_TESTING


@pytest.mark.parametrize("path", CHROME_PATHS)
def test_brand_of_leaves_everything_else_as_chrome(path):
    assert brand_of(path) == BRAND_CHROME


# `brand_of` RESOLVES SYMLINKS before it classifies, so a bare "/usr/bin/chromium" is not a
# fixed input — it is whatever that path happens to BE on the machine running the suite. Where
# distro Chromium is a snap or update-alternatives wrapper (GitHub's Ubuntu runner is one)
# readlink lands on a differently-named target and the leaf rule below never sees "chromium",
# so these cases failed in CI while passing on macOS, where the path is simply absent. Anchor
# them under a root that cannot exist: the rule under test is purely LEXICAL, and symlink
# resolution has its own test (test_brand_of_resolves_symlinks_before_classifying).
UNREAL_ROOT = "/aizu-no-such-root"


@pytest.mark.parametrize("path", [
    f"{UNREAL_ROOT}/usr/bin/chromium",
    f"{UNREAL_ROOT}/usr/bin/chromium-browser",
    f"{UNREAL_ROOT}/usr/lib/chromium-browser/chromium-browser",
])
def test_brand_of_gives_distro_chromium_its_own_directory(path):
    """Debian/Ubuntu Chromium is a THIRD brand, not a spelling of Chrome: it seals cookies
    under its own libsecret entry. The two-token rule filed it as `chrome`, which put
    /usr/bin/chromium and /usr/bin/google-chrome in one directory and wiped whichever warmed
    it first — both are on the Linux fallback list, so it is not a hypothetical pairing.
    Must match brand_of_binary (Python) and brand_of (Rust) exactly."""
    assert brand_of(path) == "chromium"


def test_brand_of_resolves_symlinks_before_classifying(tmp_path):
    """A symlink (or a wrapper reached through one) is how a Chrome-for-Testing binary gets
    past a path check. Classify the link and the launch lands in the system-Chrome dir, which
    to the operator looks like every session in it silently vanished."""
    real = tmp_path / "ms-playwright" / "chromium-1234" / "chrome-linux64" / "chrome"
    real.parent.mkdir(parents=True)
    real.write_text("#!/bin/sh\n")
    link = tmp_path / "google-chrome"          # a name that says nothing about the brand
    link.symlink_to(real)
    assert brand_of(str(link)) == BRAND_CHROME_FOR_TESTING


def test_profile_dir_is_the_brand_subdirectory():
    assert sh_profile_dir_for("/Users/o/.aizu-cft-profile", BRAND_CHROME) == \
        "/Users/o/.aizu-cft-profile/chrome"
    assert sh_profile_dir_for("/Users/o/.aizu-cft-profile", BRAND_CHROME_FOR_TESTING) == \
        "/Users/o/.aizu-cft-profile/chrome-for-testing"


def test_profile_dir_tolerates_a_trailing_slash():
    """`AIZU_CHROME_PROFILE=~/.aizu-cft-profile/` is what tab-completion hands an operator;
    a doubled separator would make it a different string in every log line and comparison."""
    assert sh_profile_dir_for("/Users/o/.aizu-cft-profile/", BRAND_CHROME) == \
        "/Users/o/.aizu-cft-profile/chrome"


@pytest.mark.parametrize("base", ["/Users/o/.aizu-cft-profile", "/tmp/p"])
def test_the_two_brands_can_never_reach_one_directory(base):
    """The entire invariant, and the reason there is nothing left to police."""
    assert sh_profile_dir_for(base, BRAND_CHROME) != \
        sh_profile_dir_for(base, BRAND_CHROME_FOR_TESTING)


def test_legacy_profile_is_described_and_left_alone(tmp_path):
    """A base that holds its own Default/ was warmed before the derivation existed, by a
    browser nobody recorded. We say so, print both destinations, and touch nothing — moving
    it ourselves would be the same guess that cost 18 cookies in A9."""
    (tmp_path / "Default").mkdir()
    (tmp_path / "Default" / "Cookies").write_bytes(b"not-a-real-db")
    derived = f"{tmp_path}/{BRAND_CHROME_FOR_TESTING}"
    out = _call("note_legacy_profile", str(tmp_path), derived)

    assert out.returncode == 0, out.stderr          # informational, never blocking
    assert "NOT been touched" in out.stdout
    # Both candidate destinations, so the move is a copy-paste and not a puzzle.
    assert f"{tmp_path}/{BRAND_CHROME}" in out.stdout
    assert f"{tmp_path}/{BRAND_CHROME_FOR_TESTING}" in out.stdout
    # ...and it never picks one for the operator.
    assert "If you KNOW which browser warmed" in out.stdout
    # Nothing moved, nothing created.
    assert (tmp_path / "Default" / "Cookies").read_bytes() == b"not-a-real-db"
    assert sorted(p.name for p in tmp_path.iterdir()) == ["Default"]


def test_no_legacy_note_on_a_base_that_only_holds_brand_dirs(tmp_path):
    """The steady state after this change: base/chrome/Default, base/chrome-for-testing/… .
    A notice here would fire on every launch forever and train the operator to ignore it."""
    (tmp_path / BRAND_CHROME / "Default").mkdir(parents=True)
    out = _call("note_legacy_profile", str(tmp_path), f"{tmp_path}/{BRAND_CHROME}")
    assert out.returncode == 0
    assert out.stdout == ""


def test_the_marker_guard_is_gone_not_merely_unused():
    """Rounds 1-3 policed a shared directory with `<profile>/.aizu-browser-brand`, a decision
    table and a refusal. Leaving any of it behind would give the next reader two contradictory
    designs to reconcile — and a dead code path that looks load-bearing (A12)."""
    text = SCRIPT.read_text()
    for gone in (".aizu-browser-brand", "guard_profile_brand", "read_profile_brand",
                 "write_profile_brand", "BRAND_MARKER_FILE"):
        assert gone not in text, f"{gone} survived the redesign"


@pytest.mark.parametrize("path", CFT_PATHS + CHROME_PATHS)
def test_bash_and_python_agree_on_every_path(path):
    assert brand_of(path) == brand_of_binary(path)


@pytest.mark.parametrize("brand", [BRAND_CHROME, BRAND_CHROME_FOR_TESTING])
def test_bash_and_python_agree_on_the_derived_directory(brand):
    """Drift here is silent: the shell warms one directory, the worker opens another, and the
    operator is asked to sign in again on a box that was already warmed."""
    base = "/Users/o/.aizu-cft-profile"
    assert sh_profile_dir_for(base, brand) == str(profile_dir_for(base, brand))


@pytest.mark.slow
def test_end_to_end_launch_uses_the_derived_directory(tmp_path):
    """The layer an operator actually reaches. A12 (and B4, E7, F-10a, F-10b before it) is the
    repo's recurring failure: a fix that is correct in a helper nobody's launch path calls. So
    run the script for real — with a fake browser on a scratch port, never 9333 — and read the
    argv the launch actually produced.

    The fake also proves the legacy notice reaches the terminal on a real invocation, and that
    it does not stop the launch."""
    fake = tmp_path / "bin" / "google-chrome"     # brand-neutral name => derives to "chrome"
    fake.parent.mkdir()
    fake.write_text(textwrap.dedent(f"""\
        #!/bin/sh
        printf '%s\\n' "$@" > "{tmp_path}/argv.txt"
        """))
    fake.chmod(0o755)
    base = tmp_path / "profile-base"
    (base / "Default").mkdir(parents=True)        # the legacy profile, untouched by us

    env = dict(os.environ)
    env.update({"AIZU_CHROME_BINARY": str(fake), "AIZU_CHROME_PROFILE": str(base),
                # A scratch port. NEVER the live 9333: everything this script does to a port
                # it cannot attribute is a refusal, but the launch itself would still bind.
                "AIZU_CDP_PORT": "9446", "TMPDIR": str(tmp_path)})
    out = subprocess.run(["bash", str(SCRIPT)], env=env, capture_output=True, text=True,
                         timeout=120)

    # No CDP ever comes up behind a fake browser, so the script ends in its own timeout — the
    # launch is what we are reading, not the exit code.
    argv = (tmp_path / "argv.txt").read_text().splitlines()
    assert f"--user-data-dir={base}/{BRAND_CHROME}" in argv
    assert f"--user-data-dir={base}" not in argv          # never the base itself
    assert f"--remote-debugging-port=9446" in argv
    # It names the brand it RESOLVED, not the one the default suggests — announcing
    # "Chrome for Testing" over a system-Chrome launch is the exact confusion A9 is about.
    assert "Launching warmed system Google Chrome" in out.stdout
    assert "NOT been touched" in out.stdout              # the legacy notice, on a real run
    assert (base / "Default").is_dir()                   # ...and it really was not touched
