"""Store-layer lifecycle control flags (BUILD-PLAN Phase 4, C6).

control_flags is the SOURCE OF TRUTH for drain/halt/update_required. resolve_
control_flags OR-merges every applicable scope (global + org + platform + worker) so an
operator can halt a whole platform, drain one org, or pin an update to one worker.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from aizu.core.store import Store


@pytest.fixture
def store(tmp_path: Path) -> Store:
    s = Store(str(tmp_path / "flags.db"))
    yield s
    s.close()


# ----- set / merge --------------------------------------------------------------

def test_set_global_halt_resolves_for_everyone(store: Store):
    store.set_control_flag(scope="global", halt=True, reason="maintenance")
    flags = store.resolve_control_flags(org_id=7, platform="instagram", worker_id="w1")
    assert flags == {"drain": False, "halt": True, "update_required": False}


def test_unset_scope_resolves_all_false(store: Store):
    assert store.resolve_control_flags(org_id=1, platform="instagram", worker_id="w1") \
        == {"drain": False, "halt": False, "update_required": False}


def test_platform_halt_does_not_leak_to_other_platform(store: Store):
    store.set_control_flag(scope="platform", scope_key="instagram", halt=True)
    assert store.resolve_control_flags(platform="instagram")["halt"] is True
    assert store.resolve_control_flags(platform="youtube")["halt"] is False


def test_org_drain_scoped_to_that_org(store: Store):
    store.set_control_flag(scope="org", scope_key="42", drain=True)
    assert store.resolve_control_flags(org_id=42)["drain"] is True
    assert store.resolve_control_flags(org_id=7)["drain"] is False


def test_worker_scope_pins_to_one_box(store: Store):
    store.set_control_flag(scope="worker", scope_key="box-a", update_required=True)
    assert store.resolve_control_flags(worker_id="box-a")["update_required"] is True
    assert store.resolve_control_flags(worker_id="box-b")["update_required"] is False


def test_flags_or_merge_across_scopes(store: Store):
    store.set_control_flag(scope="global", drain=True)
    store.set_control_flag(scope="platform", scope_key="instagram", halt=True)
    flags = store.resolve_control_flags(org_id=1, platform="instagram", worker_id="w1")
    assert flags == {"drain": True, "halt": True, "update_required": False}


def test_partial_update_keeps_other_flags(store: Store):
    store.set_control_flag(scope="global", drain=True, halt=True)
    # Flip only halt off; drain must survive (None = leave unchanged).
    store.set_control_flag(scope="global", halt=False)
    flags = store.resolve_control_flags()
    assert flags["drain"] is True and flags["halt"] is False


# ----- clear / list -------------------------------------------------------------

def test_clear_resets_all_flags(store: Store):
    store.set_control_flag(scope="platform", scope_key="instagram", halt=True)
    assert store.clear_control_flags(scope="platform", scope_key="instagram") is True
    assert store.resolve_control_flags(platform="instagram")["halt"] is False


def test_clear_missing_returns_false(store: Store):
    assert store.clear_control_flags(scope="global") is False


def test_list_returns_set_rows(store: Store):
    store.set_control_flag(scope="global", halt=True, reason="maint", set_by="a@b.co")
    store.set_control_flag(scope="org", scope_key="3", drain=True)
    rows = store.list_control_flags()
    assert len(rows) == 2
    globals_ = [r for r in rows if r["scope"] == "global"][0]
    assert globals_["halt"] is True and globals_["reason"] == "maint"
    assert globals_["setBy"] == "a@b.co"


# ----- validation ---------------------------------------------------------------

def test_unknown_scope_rejected(store: Store):
    with pytest.raises(ValueError):
        store.set_control_flag(scope="galaxy", halt=True)
    with pytest.raises(ValueError):
        store.clear_control_flags(scope="galaxy")


def test_global_ignores_scope_key(store: Store):
    # A stray scope_key on a global flag is normalised to '' so it always resolves.
    store.set_control_flag(scope="global", scope_key="ignored", halt=True)
    assert store.resolve_control_flags()["halt"] is True
