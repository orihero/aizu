"""Phase 2 — structured halt classification (HaltKind).

A HaltSession now carries a `kind` so the fan-out can tell an account-level halt
(which poisons the shared CDP browser) from a wall-clock daytime gate or an API
rate-limit (which don't). These tests pin the contract + that the three CDP engine
entrypoints fold the kind into their summary."""
import importlib

import pytest

from reelradar.cli import _POISON_HALT_KINDS
from reelradar.engines.base import SUMMARY_KEYS, HaltSession


def test_halt_session_default_kind_is_unknown():
    h = HaltSession("something went wrong")
    assert h.reason == "something went wrong"
    assert h.kind == "unknown"            # never poisons by default


def test_halt_session_stores_explicit_kind():
    assert HaltSession("blocked", kind="action_block").kind == "action_block"


def test_summary_keys_has_twelve_with_halt_kind_last():
    assert len(SUMMARY_KEYS) == 12
    assert SUMMARY_KEYS[-1] == "halt_kind"


def test_poison_kinds_exclude_daytime_and_unknown():
    # Account-level halts poison the CDP browser; a daytime gate / unknown / API
    # rate-limit (None) must NOT — they only end or pause the run.
    assert _POISON_HALT_KINDS == {"action_block", "checkpoint", "login", "canary"}
    assert "daytime" not in _POISON_HALT_KINDS
    assert "unknown" not in _POISON_HALT_KINDS


@pytest.mark.parametrize("module,cls,kind", [
    ("reelradar.engines.instagram.session", "Session", "action_block"),
    ("reelradar.engines.x.session", "XSession", "canary"),
    ("reelradar.engines.linkedin.session", "LinkedInSession", "daytime"),
])
def test_cdp_engine_run_session_folds_halt_kind(monkeypatch, module, cls, kind):
    """Each CDP engine's module-level run_session catches a re-raised HaltSession
    and returns a summary carrying both halt_reason AND the classified halt_kind —
    so dispatch never sees the exception and the kind reaches the fan-out."""
    mod = importlib.import_module(module)

    class FakeSession:
        def __init__(self, **_kw):
            self.session_id = "sess-fake"

        def run(self):
            raise HaltSession("boom", kind=kind)

    monkeypatch.setattr(mod, cls, FakeSession)
    out = mod.run_session(campaign=None, store=None, router=None, feed=None,
                          soul=None, pacer=None)
    assert out["halt_kind"] == kind
    assert out["halt_reason"] == "boom"
    assert out["session_id"] == "sess-fake"
