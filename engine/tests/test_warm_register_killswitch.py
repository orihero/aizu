"""warm-register CLI + warming kill-switch (warming PRD §6.5, §9.1)."""
import os
import tempfile

import pytest

from aizu import warming_control
from aizu.cli import main
from aizu.core.accounts import PROVISIONED
from aizu.core.store import Store
from aizu.secrets import SecretCipher


def _db():
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    return path


# ---- warm-register ----

def test_warm_register_records_provisioned_account():
    path = _db()
    rc = main(["--db", path, "warm-register", "--org", "1", "--platform", "x",
               "--username", "acme", "--chrome-profile", "/p/x/1",
               "--cdp-port", "9333", "--timezone", "Asia/Tashkent", "--skip-verify"])
    assert rc == 0
    store = Store(path)
    accts = store.list_accounts(1, platform="x")
    assert len(accts) == 1
    a = accts[0]
    assert a["username"] == "acme" and a["state"] == PROVISIONED
    assert a["cdp_port"] == 9333
    assert a["fingerprint"] == {"timezone_id": "Asia/Tashkent"}
    store.close()


def test_warm_register_rejects_non_warmable_platform():
    path = _db()
    rc = main(["--db", path, "warm-register", "--org", "1", "--platform", "youtube",
               "--username", "chan", "--skip-verify"])
    assert rc == 2


def test_warm_register_stores_proxy_encrypted(monkeypatch):
    monkeypatch.setenv("AIZU_SECRET_KEY", SecretCipher.generate_key())
    path = _db()
    rc = main(["--db", path, "warm-register", "--org", "1", "--platform", "linkedin",
               "--username", "u", "--proxy", "http://user:pass@host:8080",
               "--skip-verify"])
    assert rc == 0
    store = Store(path)
    aid = store.list_accounts(1, platform="linkedin")[0]["id"]
    assert store.get_account_secret(1, "linkedin", aid) == {"proxy": "http://user:pass@host:8080"}
    store.close()


# ---- kill-switch ----

def test_kill_switch_off_by_default(monkeypatch):
    monkeypatch.delenv("AIZU_WARMING_ENABLED", raising=False)
    store = Store(_db())
    reason = warming_control.warming_kill_reason(store, org_id=1, platform="x")
    assert reason and "AIZU_WARMING_ENABLED" in reason
    store.close()


def test_kill_switch_env_enables(monkeypatch):
    monkeypatch.setenv("AIZU_WARMING_ENABLED", "1")
    store = Store(_db())
    assert warming_control.warming_kill_reason(store, org_id=1, platform="x") is None
    store.close()


def test_kill_switch_per_org_disable(monkeypatch):
    monkeypatch.setenv("AIZU_WARMING_ENABLED", "1")
    store = Store(_db())
    store.set_setting(1, "warmingEnabled", False)
    assert warming_control.warming_kill_reason(store, org_id=1, platform="x") is not None
    # a different org is unaffected
    assert warming_control.warming_kill_reason(store, org_id=2, platform="x") is None
    store.close()


def test_kill_switch_per_platform_disable(monkeypatch):
    monkeypatch.setenv("AIZU_WARMING_ENABLED", "1")
    store = Store(_db())
    store.set_setting(1, "warmingDisabledPlatforms", ["x"])
    assert warming_control.warming_kill_reason(store, org_id=1, platform="x") is not None
    assert warming_control.warming_kill_reason(store, org_id=1, platform="linkedin") is None
    store.close()


def test_control_flag_halt_also_fails_warming_gate(monkeypatch):
    """BUILD-PLAN Phase 4 defense-in-depth: a fleet-console platform halt ALSO fails the
    in-engine warming gate, so a halted platform stops warming even mid-lease."""
    monkeypatch.setenv("AIZU_WARMING_ENABLED", "1")
    store = Store(_db())
    assert warming_control.warming_kill_reason(store, org_id=1, platform="x") is None
    store.set_control_flag(scope="platform", scope_key="x", halt=True)
    reason = warming_control.warming_kill_reason(store, org_id=1, platform="x")
    assert reason and "halted" in reason
    # A different platform is unaffected.
    assert warming_control.warming_kill_reason(store, org_id=1, platform="y") is None
    store.close()
