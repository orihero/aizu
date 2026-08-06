"""Phase 2 — structured halt classification (HaltKind).

A HaltSession now carries a `kind` so the fan-out can tell an account-level halt
(which poisons the shared CDP browser) from a wall-clock daytime gate or an API
rate-limit (which don't). These tests pin the contract + that the three CDP engine
entrypoints fold the kind into their summary."""
import importlib

import pytest

from aizu.cli import _POISON_HALT_KINDS
from aizu.engines.base import HARD_HALT_KINDS, SOFT_HALT_KINDS, SUMMARY_KEYS, HaltSession


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


# --- Gap #1: SOFT (auto-resumable) vs HARD (human-gated) halt taxonomy ---------


def test_soft_and_hard_halt_kinds_are_disjoint():
    assert SOFT_HALT_KINDS == {"action_block", "canary"}
    assert HARD_HALT_KINDS == {"checkpoint", "login"}
    assert not (SOFT_HALT_KINDS & HARD_HALT_KINDS)


def test_daytime_and_unknown_are_in_neither_bucket():
    # `daytime` is its own scheduled wall-clock gate (not a platform signal at all)
    # and `unknown` is the conservative unclassified default — neither auto-resumes
    # via the cooldown table, and neither is human-gated either; they're simply
    # outside this taxonomy.
    assert "daytime" not in SOFT_HALT_KINDS and "daytime" not in HARD_HALT_KINDS
    assert "unknown" not in SOFT_HALT_KINDS and "unknown" not in HARD_HALT_KINDS


def test_soft_and_hard_kinds_together_equal_the_poison_kinds():
    # Every CDP-browser-poisoning kind is exactly one of SOFT or HARD — poisoning
    # the shared browser (an orthogonal fan-out concern, _POISON_HALT_KINDS) and
    # needing a human (this taxonomy) are independent axes that happen to cover the
    # same four kinds today.
    assert SOFT_HALT_KINDS | HARD_HALT_KINDS == _POISON_HALT_KINDS


class _FakeCooldownStore:
    """Records record_soft_halt calls; nothing else on Store is touched by
    Session._halt."""

    def __init__(self):
        self.calls = []

    def record_soft_halt(self, campaign_id, platform, kind):
        self.calls.append((campaign_id, platform, kind))


class _FakeCampaign:
    campaign_id = "c1"
    platform = "instagram"


@pytest.mark.parametrize("kind,is_soft", [
    ("action_block", True),
    ("canary", True),
    ("checkpoint", False),
    ("login", False),
    ("daytime", False),
    ("unknown", False),
])
def test_session_halt_routes_soft_kinds_to_cooldown_only(kind, is_soft):
    """Session._halt (engines/instagram/session.py) is the SOFT-vs-HARD routing
    decision point: it must escalate the cooldown for exactly the SOFT kinds and
    leave every other kind's HaltSession behavior byte-for-byte as before."""
    from aizu.engines.instagram.session import Session

    store = _FakeCooldownStore()
    session = Session.__new__(Session)   # _halt only reads self.store/self.campaign
    session.store = store
    session.campaign = _FakeCampaign()

    with pytest.raises(HaltSession) as excinfo:
        session._halt("boom", kind=kind)

    assert excinfo.value.reason == "boom"
    assert excinfo.value.kind == kind
    if is_soft:
        assert store.calls == [("c1", "instagram", kind)]
    else:
        assert store.calls == []


@pytest.mark.parametrize("module,cls,kind", [
    ("aizu.engines.instagram.session", "Session", "action_block"),
    ("aizu.engines.x.session", "XSession", "canary"),
    ("aizu.engines.linkedin.session", "LinkedInSession", "daytime"),
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
