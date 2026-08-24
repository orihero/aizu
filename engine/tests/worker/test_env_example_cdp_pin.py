"""`engine/.env.example` must not disarm CDP auto-adoption for everyone who copies it.

`WorkerConfig.cdp_url_explicit` is keyed off the PRESENCE of AIZU_CDP_URL, and
`preflight.resolve_cdp_url` reads it to choose between two very different behaviours:
an unpinned box whose Chrome sits on the other well-known port is silently ADOPTED and
keeps working unattended, while a PINNED box gets a named fatal instead. The example
file's own comment says "leave it commented out unless you really do run Chrome
somewhere else" — and then shipped the line live, so the boxes that followed the setup
guide were exactly the boxes that lost auto-adoption (ledger F10, defect F-5).

Both tests here read the shipped file: one on the text, one on the value that text
produces after the loader has run, because a bare `AIZU_CDP_URL=` would satisfy the
first and still pin the box.
"""
from __future__ import annotations

import os
from pathlib import Path
from unittest import mock

from aizu.worker.config import DEFAULT_CDP_URL, WorkerConfig

ENV_EXAMPLE = Path(__file__).resolve().parents[2] / ".env.example"


def _assignments() -> dict[str, str]:
    """Every key the example would actually set, parsed EXACTLY the way `cli._load_env`'s
    built-in parser does (blank lines, `#` comments and lines without `=` are skipped) —
    a different parser here would test a file nobody loads."""
    env: dict[str, str] = {}
    for line in ENV_EXAMPLE.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        env.setdefault(key.strip(), val.strip().strip("'\""))
    return env


def test_env_example_does_not_assign_aizu_cdp_url():
    assert "AIZU_CDP_URL" not in _assignments()


def test_env_example_still_documents_the_knob():
    """Commented out, not deleted — an operator who genuinely runs Chrome elsewhere
    still needs to find the variable and the port in this file."""
    text = ENV_EXAMPLE.read_text(encoding="utf-8")
    assert "#AIZU_CDP_URL=http://127.0.0.1:9333" in text


def test_a_box_provisioned_from_the_example_keeps_auto_adoption():
    """The behaviour that matters: build the config from an environment containing
    nothing but the example's own assignments and confirm the box reads as UNPINNED, on
    the canonical port."""
    with mock.patch.dict(os.environ, _assignments(), clear=True):
        cfg = WorkerConfig.from_env(dispatch_base_url="https://cloud.example.com")
    assert cfg.cdp_url_explicit is False
    assert cfg.cdp_url == DEFAULT_CDP_URL
