# -*- mode: python ; coding: utf-8 -*-
#
# AIZU Worker sidecar — PyInstaller spec (Phase 6 SCAFFOLD, UNCOMPILED).
#
# SCAFFOLD SOURCE ONLY: this environment has no PyInstaller. This spec has NOT been run.
# The `hiddenimports` and `datas` below are a FIRST GUESS and WILL need a real
# `pyinstaller --clean desktop/pyinstaller/sidecar.spec` iteration by an engineer with the
# toolchain — freezing Playwright + cryptography + keyring reliably almost always requires
# adjusting these lists once the frozen binary is exercised on each target OS.
#
# What it builds: a single-binary `reelradar-worker` whose entry point is
# `reelradar.worker.sidecar:main` — the SAME console-script target declared in
# engine/pyproject.toml ([project.scripts] reelradar-worker). The Tauri app supervises this
# binary as a managed child (C3 option A). It NEVER runs reelradar.cli and NEVER RunManager.
#
# Run from the repo root with the engine's venv active, e.g.:
#   cd engine && pyinstaller --clean ../desktop/pyinstaller/sidecar.spec
#
# NOTE ON soul.md (C5): soul is BAKED INTO THE JOB SPEC at enqueue time, so config/soul.md
# is deliberately NOT bundled here. If you must support the legacy file fallback
# (job_runner._resolve_soul → load_soul(cfg_dir/soul.md)), add it via --add-data at build
# time OR uncomment the datas entry marked "LEGACY SOUL FALLBACK" below. Do not bundle it
# by default.

import os
import sys
from pathlib import Path

from PyInstaller.utils.hooks import collect_submodules

# --- Locate the engine package + entry script -------------------------------------------
# The spec lives at desktop/pyinstaller/; the engine is at ../../engine relative to it.
# PyInstaller sets SPECPATH to the spec's directory.
SPEC_DIR = Path(SPECPATH).resolve()  # noqa: F821 (SPECPATH injected by PyInstaller)
ENGINE_DIR = (SPEC_DIR / ".." / ".." / "engine").resolve()

# The entry point is a tiny shim (run_sidecar.py) that imports the sidecar as a PACKAGE and
# calls main() — NOT sidecar.py directly, which would run as __main__ and break its
# package-relative imports (`from ..core ...`). Behaviorally identical to the console script.
ENTRY = str(SPEC_DIR / "run_sidecar.py")


# --- Resolve Playwright's driver dir so the frozen binary ships its browsers driver ------
# Playwright needs its Node driver + package data at runtime; the driver lives next to the
# installed `playwright` package. Found dynamically (never hardcode a site-packages path).
def _playwright_driver_datas():
    try:
        import playwright  # noqa: WPS433 (import at build time is intentional)
    except Exception:
        # If Playwright can't be imported at build time the sidecar can't run a real job;
        # surface it loudly rather than shipping a broken binary.
        print("WARNING: playwright not importable at build time — datas will be empty; "
              "the frozen sidecar will NOT be able to drive Chrome.", file=sys.stderr)
        return []
    pkg_dir = Path(playwright.__file__).parent
    driver_dir = pkg_dir / "driver"
    entries = []
    if driver_dir.exists():
        # Copy the whole driver tree, preserving the `playwright/driver/...` layout so
        # Playwright resolves it at runtime.
        for path in driver_dir.rglob("*"):
            if path.is_file():
                rel = path.relative_to(pkg_dir.parent)  # keep `playwright/driver/...`
                entries.append((str(path), str(rel.parent)))
    else:
        print(f"WARNING: playwright driver dir not found at {driver_dir} — "
              "adjust this spec.", file=sys.stderr)
    return entries


datas = []
datas += _playwright_driver_datas()

# LEGACY SOUL FALLBACK (C5) — soul is baked into the job spec, so this stays COMMENTED.
# Only enable if you must support job_runner's file fallback on a box:
# datas += [(str(ENGINE_DIR / "config" / "soul.md"), "config")]


# --- Hidden imports: first guess (WILL need `pyinstaller --clean` iteration) --------------
# PyInstaller's static analysis misses dynamically/lazily imported modules. These are the
# usual suspects for this stack; expect to add more once the frozen binary throws a
# ModuleNotFoundError under real use on each OS.
hiddenimports = [
    "playwright",
    "playwright.sync_api",
    "httpx",
    # cryptography's OpenSSL backend is imported lazily by Fernet (secrets.py).
    "cryptography.hazmat.backends.openssl",
    "cryptography.hazmat.bindings._rust",
    "rich",
    # keyring + its per-OS backends land in Phase 6 packaging (token/secret storage). The
    # correct backend module differs per OS; the frozen binary must include the right one.
    "keyring",
    "keyring.backends",
    "keyring.backends.macOS",       # macOS Keychain
    "keyring.backends.Windows",     # Windows Credential Locker
    "keyring.backends.SecretService",  # Linux (libsecret)
]

# The engine imports several submodules LAZILY (dispatch.build_feed → engines.*, the
# per-platform CDP engines, core.*), which PyInstaller's static analysis misses. Collect the
# whole reelradar + playwright trees so a real job's lazy imports resolve in the frozen binary.
hiddenimports += collect_submodules("reelradar")
hiddenimports += collect_submodules("playwright")


block_cipher = None

a = Analysis(
    [ENTRY],
    pathex=[str(ENGINE_DIR)],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    # telethon is an OPTIONAL extra (Telegram MTProto). Exclude unless a box runs Telegram
    # jobs, to keep the binary smaller; add it to hiddenimports + install the extra if so.
    excludes=["telethon"],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

# ONEDIR build (exclude_binaries=True + COLLECT): the binaries/data live in a folder next
# to the executable instead of being packed into one file. This avoids the multi-second
# per-launch self-extraction of onefile (the 59MB Playwright-bearing bundle unpacked on
# EVERY start ≈ 20s cold start) — a onedir app launches near-instantly. The Tauri bundle
# ships the whole dist/reelradar-worker/ folder as a resource.
exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="reelradar-worker",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,           # UPX can trip AV/notarization; leave off for signed distribution
    console=True,        # the sidecar logs to stdout/stderr; the Tauri app captures it
    disable_windowed_traceback=False,
    target_arch=None,    # set to "arm64"/"x86_64" for a specific macOS slice if needed
    codesign_identity=None,   # signing is done at the Tauri bundle layer, not here
    entitlements_file=None,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=False,
    name="reelradar-worker",   # → dist/reelradar-worker/ (executable + _internal/)
)
