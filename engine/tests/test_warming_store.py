"""Store account-pool methods — CRUD, lifecycle guard, assignment, secrets,
resolve_flag transaction boundary (warming PRD §3.3)."""
import os
import tempfile

import pytest

from aizu.core.accounts import (
    ACTIVE,
    COOLING,
    FLAGGED,
    InvalidAccountTransition,
    PROVISIONED,
    READY,
    WARMING,
    warming_sentinel_campaign,
)
from aizu.core.store import Store
from aizu.secrets import SecretCipher


def fresh_store(with_cipher=False):
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    cipher = SecretCipher(SecretCipher.generate_key()) if with_cipher else None
    return Store(path, secret_cipher=cipher), path


# ---- add_account / get / list ----

def test_add_account_round_trips_with_json_fields():
    store, _ = fresh_store()
    aid = store.add_account(1, "x", "acme_co",
                            profile_dir="/p/x/1", cdp_port=9333,
                            fingerprint={"timezone_id": "Asia/Tashkent"})
    acct = store.get_account(aid)
    assert acct["username"] == "acme_co"
    assert acct["state"] == PROVISIONED
    assert acct["cdp_port"] == 9333
    assert acct["fingerprint"] == {"timezone_id": "Asia/Tashkent"}   # decoded


def test_add_account_rejects_non_warmable_platform():
    store, _ = fresh_store()
    with pytest.raises(ValueError, match="not warmable"):
        store.add_account(1, "youtube", "chan")
    with pytest.raises(ValueError, match="not warmable"):
        store.add_account(1, "reddit", "chan")


def test_add_account_idempotent_on_identity():
    store, _ = fresh_store()
    a1 = store.add_account(1, "x", "dup")
    a2 = store.add_account(1, "x", "dup")
    assert a1 == a2                       # UNIQUE(org, platform, username)


def test_list_accounts_scoped_by_org_platform_state():
    store, _ = fresh_store()
    store.add_account(1, "x", "a")
    store.add_account(1, "linkedin", "b")
    store.add_account(2, "x", "c")
    assert len(store.list_accounts(1)) == 2
    assert len(store.list_accounts(1, platform="x")) == 1
    assert len(store.list_accounts(2)) == 1
    assert store.list_accounts(1, state=READY) == []


# ---- lifecycle ----

def test_lifecycle_transition_writes_audit_row():
    store, _ = fresh_store()
    aid = store.add_account(1, "x", "a")
    store.update_account_lifecycle(aid, WARMING, reason="kickoff")
    acct = store.update_account_lifecycle(aid, READY, reason="warmth_gate_passed")
    assert acct["state"] == READY
    log = store.account_state_changes(aid)
    assert [(r["from_state"], r["to_state"]) for r in log] == [
        (PROVISIONED, WARMING), (WARMING, READY)]
    assert log[-1]["reason"] == "warmth_gate_passed"


def test_lifecycle_rejects_illegal_transition():
    store, _ = fresh_store()
    aid = store.add_account(1, "x", "a")
    with pytest.raises(InvalidAccountTransition):
        store.update_account_lifecycle(aid, ACTIVE, reason="skip-warming")
    assert store.get_account(aid)["state"] == PROVISIONED   # unchanged


def test_lifecycle_extra_fields_whitelisted():
    store, _ = fresh_store()
    aid = store.add_account(1, "x", "a")
    store.update_account_lifecycle(aid, WARMING, reason="r")
    store.update_account_lifecycle(aid, READY, reason="r")    # cooling needs ready/active
    acct = store.update_account_lifecycle(
        aid, COOLING, reason="rate", cooling_until=123.0, consecutive_flag_count=2)
    assert acct["cooling_until"] == 123.0
    assert acct["consecutive_flag_count"] == 2
    with pytest.raises(ValueError, match="non-mutable"):
        store.update_account_lifecycle(aid, WARMING, reason="r", username="hacked")


def test_update_ramp():
    store, _ = fresh_store()
    aid = store.add_account(1, "x", "a")
    acct = store.update_account_ramp(aid, ramp_day=5, warmth_floor=0.3,
                                     last_warmed_at=999.0)
    assert acct["ramp_day"] == 5 and acct["warmth_floor"] == 0.3
    assert acct["last_warmed_at"] == 999.0


# ---- assignment + cross-org integrity ----

def _register_campaign(store, cid, org_id):
    store.upsert_campaign_meta(cid, org_id=org_id, status="live")


def test_assign_and_resolve_account():
    store, _ = fresh_store()
    _register_campaign(store, "camp1", 1)
    aid = store.add_account(1, "x", "a")
    store.assign_account("camp1", "x", aid, pinned=True)
    resolved = store.resolve_account_for_campaign("camp1", "x")
    assert resolved["id"] == aid


def test_assign_rejects_cross_org():
    store, _ = fresh_store()
    _register_campaign(store, "camp1", 1)
    other = store.add_account(2, "x", "other")     # different org
    with pytest.raises(ValueError, match="cross-org"):
        store.assign_account("camp1", "x", other)


def test_resolve_returns_none_for_non_warmable():
    store, _ = fresh_store()
    _register_campaign(store, "yt1", 1)
    assert store.resolve_account_for_campaign("yt1", "youtube") is None


def test_resolve_pool_picks_harvest_ready_account():
    store, _ = fresh_store()
    _register_campaign(store, "camp1", 1)
    cold = store.add_account(1, "x", "cold")
    ready = store.add_account(1, "x", "ready")
    store.update_account_lifecycle(ready, WARMING, reason="r")
    store.update_account_lifecycle(ready, READY, reason="r")
    resolved = store.resolve_account_for_campaign("camp1", "x")
    assert resolved["id"] == ready                  # ready beats provisioned


def test_resolve_none_when_pool_empty():
    store, _ = fresh_store()
    _register_campaign(store, "camp1", 1)
    assert store.resolve_account_for_campaign("camp1", "x") is None


# ---- resolve_flag transaction boundary ----

def test_resolve_flag_sets_resolved_and_transitions_atomically():
    store, _ = fresh_store()
    aid = store.add_account(1, "x", "a")
    store.update_account_lifecycle(aid, WARMING, reason="r")
    store.update_account_lifecycle(aid, READY, reason="r")
    store.update_account_lifecycle(aid, FLAGGED, reason="checkpoint")
    store.raise_flag("account_challenge", "halt", "checkpoint",
                     org_id=1, account_id=aid)
    flag_id = store.open_flags(org_id=1)[0]["id"]

    assert store.resolve_flag(flag_id, 1, to_state=WARMING) is True
    assert store.open_flags(org_id=1) == []                 # resolved
    assert store.get_account(aid)["state"] == WARMING        # transitioned


def test_resolve_flag_rejects_cross_org():
    store, _ = fresh_store()
    aid = store.add_account(1, "x", "a")
    store.raise_flag("account_challenge", "halt", "x", org_id=1, account_id=aid)
    flag_id = store.open_flags(org_id=1)[0]["id"]
    assert store.resolve_flag(flag_id, 999) is False         # wrong org
    assert len(store.open_flags(org_id=1)) == 1              # still open


def test_resolve_flag_illegal_transition_raises_and_leaves_flag_open():
    store, _ = fresh_store()
    aid = store.add_account(1, "x", "a")          # PROVISIONED
    store.raise_flag("account_challenge", "halt", "x", org_id=1, account_id=aid)
    flag_id = store.open_flags(org_id=1)[0]["id"]
    with pytest.raises(InvalidAccountTransition):
        store.resolve_flag(flag_id, 1, to_state=READY)        # provisioned->ready illegal
    assert len(store.open_flags(org_id=1)) == 1              # flag NOT resolved


# ---- warming session/action stamping (sentinel) ----

def test_warming_session_and_action_stamp_account_id():
    store, _ = fresh_store()
    aid = store.add_account(1, "x", "a")
    sentinel = warming_sentinel_campaign(1)
    store.start_session("ws1", sentinel, "x", org_id=1,
                        engine_mode="warming", account_id=aid)
    sess = store.get_session("ws1")
    assert sess["engine_mode"] == "warming"
    assert sess["account_id"] == aid
    assert sess["org_id"] == 1
    store.log_action(sentinel, "follow", target="@someone", succeeded=True,
                     session_id="ws1", account_id=aid)
    row = store._conn.execute(
        "SELECT account_id, reel_id FROM actions WHERE session_id='ws1'").fetchone()
    assert row["account_id"] == aid and row["reel_id"] is None


# ---- per-account per-day action counts (warming-writes PRD §3.7, X6) ----

_TASHKENT_OFFSET = 5 * 3600
_DAY = 86400


def _insert_action(store, *, account_id, action_type, created_at, succeeded=True):
    """Insert one raw actions row at a controlled epoch (log_action stamps
    time.time(), so the day-bucket test wires created_at directly)."""
    store._conn.execute(
        "INSERT INTO actions(campaign_id, session_id, reel_id, action_type, "
        "target, succeeded, created_at, account_id) VALUES(?,?,?,?,?,?,?,?)",
        ("__warming__:1", "ws", None, action_type, "@t", int(succeeded),
         created_at, account_id))
    store._conn.commit()


def test_action_counts_for_account_day_counts_current_local_day():
    store, _ = fresh_store()
    aid = store.add_account(1, "instagram", "a")
    # `now` sits comfortably inside a Tashkent day (noon UTC+5).
    now = 100 * _DAY + 12 * 3600 - _TASHKENT_OFFSET
    _insert_action(store, account_id=aid, action_type="like", created_at=now)
    _insert_action(store, account_id=aid, action_type="like", created_at=now - 60)
    _insert_action(store, account_id=aid, action_type="save", created_at=now - 120)
    counts = store.action_counts_for_account_day(aid, now=now)
    assert counts == {"like": 2, "save": 1}


def test_action_counts_for_account_day_filters_by_action_type():
    store, _ = fresh_store()
    aid = store.add_account(1, "instagram", "a")
    now = 100 * _DAY + 12 * 3600 - _TASHKENT_OFFSET
    _insert_action(store, account_id=aid, action_type="like", created_at=now)
    _insert_action(store, account_id=aid, action_type="follow", created_at=now)
    assert store.action_counts_for_account_day(aid, now=now, action_type="like") == 2 - 1
    assert store.action_counts_for_account_day(aid, now=now, action_type="follow") == 1
    assert store.action_counts_for_account_day(aid, now=now, action_type="share") == 0


def test_action_counts_for_account_day_excludes_other_days():
    store, _ = fresh_store()
    aid = store.add_account(1, "instagram", "a")
    now = 100 * _DAY + 12 * 3600 - _TASHKENT_OFFSET
    _insert_action(store, account_id=aid, action_type="like", created_at=now)
    _insert_action(store, account_id=aid, action_type="like", created_at=now - _DAY)
    _insert_action(store, account_id=aid, action_type="like", created_at=now + _DAY)
    assert store.action_counts_for_account_day(aid, now=now, action_type="like") == 1


def test_action_counts_for_account_day_excludes_failed_and_other_accounts():
    store, _ = fresh_store()
    aid = store.add_account(1, "instagram", "a")
    other = store.add_account(1, "instagram", "b")
    now = 100 * _DAY + 12 * 3600 - _TASHKENT_OFFSET
    _insert_action(store, account_id=aid, action_type="like", created_at=now)
    _insert_action(store, account_id=aid, action_type="like", created_at=now,
                   succeeded=False)
    _insert_action(store, account_id=other, action_type="like", created_at=now)
    assert store.action_counts_for_account_day(aid, now=now, action_type="like") == 1


# ---- per-account secrets ----

def test_account_secret_round_trip():
    store, _ = fresh_store(with_cipher=True)
    aid = store.add_account(1, "x", "a")
    store.put_account_secret(1, "x", aid, {"proxy": "http://u:p@host:8080"})
    assert store.get_account_secret(1, "x", aid) == {"proxy": "http://u:p@host:8080"}
    assert store.get_account_secret(1, "x", 999) is None
