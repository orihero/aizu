"""What importing `aizu.worker.config` costs (F9).

`worker/preflight.py` imports `readiness` LAZILY, six times, with a comment saying why:
"readiness pulls Playwright in at import time and preflight must stay cheap to import on
an API-only box". That care was undone one line at a time by
`worker/config.py`'s `from ..readiness import DEFAULT_CDP_URL` — a module-level import
that every sidecar process makes, for a single string constant. These tests pin the cost
so the import cannot creep back in, and pin the constant so duplicating the literal
cannot drift.
"""
from __future__ import annotations

import subprocess
import sys

from aizu import readiness
from aizu.cli import DEFAULT_CDP_URL as CLI_DEFAULT_CDP_URL
from aizu.worker.config import DEFAULT_CDP_URL

# Generous: a cold interpreter on a slow worker PC still starts well inside this, and a
# bounded wait is the only kind this repo allows (macOS has no `timeout`).
_IMPORT_TIMEOUT_SEC = 90


def _import_probe(module: str) -> set[str]:
    """Import `module` in a FRESH interpreter and report which of the two modules we
    care about came along. It must be a subprocess: pytest has already imported
    `readiness` (and therefore Playwright) into this process, so an in-process
    `sys.modules` check could only ever pass."""
    code = (
        "import sys;"
        f"__import__({module!r});"
        "print('playwright' if 'playwright' in sys.modules else '', "
        "'readiness' if 'aizu.readiness' in sys.modules else '')"
    )
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True,
                         timeout=_IMPORT_TIMEOUT_SEC, check=True)
    return set(out.stdout.split())


def test_importing_worker_config_does_not_import_readiness_or_playwright():
    """The regression itself: `import aizu.worker.config` must stay Playwright-free."""
    assert _import_probe("aizu.worker.config") == set()


def test_default_cdp_url_stays_in_lockstep_with_readiness():
    """The price of not importing `readiness` is a duplicated literal (the same trade
    `core/cdp.py` already makes). This is the drift guard that makes it safe: 9333 is
    the canonical warmed-Chrome port repo-wide (ledger F10), and a box probing the
    wrong one tells its operator to "start Chrome" on a machine where Chrome is
    already running."""
    assert (DEFAULT_CDP_URL == CLI_DEFAULT_CDP_URL == readiness.DEFAULT_CDP_URL
            == "http://127.0.0.1:9333")


def test_importing_preflight_does_not_import_readiness_or_playwright():
    """The lazy `from .. import readiness` imports inside `worker/preflight.py` are the
    whole point of the exercise, and they only buy something if NOTHING preflight imports
    eagerly pulls readiness in behind their back. Pinning preflight itself — not just
    `config` — is what stops the next single-constant import from quietly undoing them."""
    assert _import_probe("aizu.worker.preflight") == set()


def test_importing_the_cli_does_not_drag_readiness_in():
    """`worker/job_runner.py` does `from .. import cli` at module scope (the
    `_run_session_loop` seam), so EVERY sidecar imports the CLI. While `cli` did
    `from .readiness import DEFAULT_CDP_URL` for one string, that edge re-imported
    readiness — and therefore Playwright — into every worker process, which is the same
    defect as the `worker/config.py` one, one module further out.

    Note what this does NOT claim: importing `cli` still costs Playwright, via
    `core/pacing -> core/human -> core/pw_owner`, which imports the real
    `playwright.sync_api.TimeoutError` on purpose so `except PlaywrightTimeout` catches
    the real class. That edge is by design and belongs to a module that drives browsers
    anyway; this test pins only the readiness edge, which was gratuitous."""
    assert "readiness" not in _import_probe("aizu.cli")
