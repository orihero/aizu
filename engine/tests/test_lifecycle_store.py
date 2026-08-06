"""Schema v12 — campaign lifecycle controls: archive + pause precedence + the
additive column self-heal (campaign-lifecycle-controls PRD, Phase 1)."""
import os
import sqlite3
import tempfile

import pytest

from aizu.core.store import COOLDOWN_BASE_SECONDS, COOLDOWN_MAX_SECONDS, SCHEMA_VERSION, Store


def fresh_store():
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    return Store(path), path


def _cols(conn, table):
    return {r[1] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()}


_V12_COLS = {
    "archived_at", "paused_reason", "schedule_enabled", "schedule_kind",
    "schedule_dow", "schedule_hour", "schedule_minute", "schedule_tz",
    "next_run_at", "last_scheduled_run_at", "schedule_target_leads",
    "schedule_duration_minutes",
}


# ----- schema -----

def test_fresh_db_is_v12_with_lifecycle_columns():
    store, _ = fresh_store()
    try:
        assert _V12_COLS <= _cols(store._conn, "campaign_meta")
        ver = store._conn.execute(
            "SELECT value FROM meta WHERE key='schema_version'").fetchone()[0]
        # v12 added the lifecycle columns (asserted above); v13 (billing) bumped
        # the version. The lifecycle columns must still be present at the current
        # version — that's the invariant this test guards.
        assert ver == str(SCHEMA_VERSION)
        assert int(ver) >= 12
    finally:
        store.close()


def test_v11_db_self_heals_to_v12_on_open():
    """A v11-shaped campaign_meta (no lifecycle columns) gains them on the next open
    via _add_column_if_missing, and existing rows take the DEFAULTs (unscheduled,
    unarchived) — the additive idiom, no rename-dance."""
    store, path = fresh_store()
    store.upsert_campaign_meta("c1", org_id=1, status="live", goal_target=40)
    store.close()
    # Strip the v12 surface back to a v11 shape.
    conn = sqlite3.connect(path)
    conn.execute("DROP INDEX IF EXISTS idx_campaign_meta_next_run")
    conn.execute("DROP INDEX IF EXISTS idx_campaign_meta_archived")
    for col in _V12_COLS:
        conn.execute(f"ALTER TABLE campaign_meta DROP COLUMN {col}")
    conn.execute("UPDATE meta SET value='11' WHERE key='schema_version'")
    conn.commit()
    conn.close()
    assert not (_V12_COLS & _cols(sqlite3.connect(path), "campaign_meta"))

    store2 = Store(path)        # re-open self-heals
    try:
        assert _V12_COLS <= _cols(store2._conn, "campaign_meta")
        meta = store2.get_campaign_meta("c1")
        assert meta["goal_target"] == 40           # row preserved
        assert meta["archived_at"] is None          # default
        assert meta["schedule_enabled"] == 0        # default
        ver = store2._conn.execute(
            "SELECT value FROM meta WHERE key='schema_version'").fetchone()[0]
        assert ver == str(SCHEMA_VERSION)
    finally:
        store2.close()


def test_v12_migration_idempotent():
    _, path = fresh_store()
    Store(path).close()           # re-open an already-v12 DB → no-op
    store = Store(path)
    try:
        store.upsert_campaign_meta("c", org_id=1, status="live")
        assert store.get_campaign_meta("c")["schedule_enabled"] == 0
    finally:
        store.close()


# ----- archive (dedicated UPDATE, not the COALESCE upsert) -----

def test_archive_stamps_without_touching_status():
    store, _ = fresh_store()
    store.upsert_campaign_meta("c", org_id=1, status="draft")
    meta = store.set_campaign_archived("c", True)
    assert meta["archived_at"] is not None
    assert meta["status"] == "draft"               # plain archive leaves status


def test_archive_while_live_transitions_to_paused():
    store, _ = fresh_store()
    store.upsert_campaign_meta("c", org_id=1, status="live")
    meta = store.set_campaign_archived("c", True, new_status="paused")
    assert meta["archived_at"] is not None
    assert meta["status"] == "paused"              # (archived, live) never reachable


def test_unarchive_round_trips_and_actually_nulls_column():
    """Proves the dedicated UPDATE clears the column — a COALESCE-merge upsert
    could never set archived_at back to NULL."""
    store, _ = fresh_store()
    store.upsert_campaign_meta("c", org_id=1, status="live")
    store.set_campaign_archived("c", True, new_status="paused")
    meta = store.set_campaign_archived("c", False)
    assert meta["archived_at"] is None


def test_archive_unknown_campaign_returns_none():
    store, _ = fresh_store()
    assert store.set_campaign_archived("nope", True) is None


def test_archive_rejects_bad_status():
    store, _ = fresh_store()
    store.upsert_campaign_meta("c", org_id=1, status="live")
    with pytest.raises(ValueError):
        store.set_campaign_archived("c", True, new_status="banana")


# ----- pause precedence -----

def test_user_pause_cleared_by_user_resume():
    store, _ = fresh_store()
    store.upsert_campaign_meta("c", org_id=1, status="live")
    store.set_campaign_paused("c", paused=True, reason="user")
    assert store.get_campaign_meta("c")["status"] == "paused"
    meta = store.set_campaign_paused("c", paused=False, reason="user")
    assert meta["status"] == "live"
    assert meta["paused_reason"] is None


def test_auto_pause_not_cleared_by_user_resume():
    """A system 'auto' halt outranks a 'user' resume — the operator toggle is a
    no-op until the cause is addressed."""
    store, _ = fresh_store()
    store.upsert_campaign_meta("c", org_id=1, status="live")
    store.set_campaign_paused("c", paused=True, reason="auto")
    meta = store.set_campaign_paused("c", paused=False, reason="user")
    assert meta["status"] == "paused"              # still paused
    assert meta["paused_reason"] == "auto"


def test_auto_resume_clears_auto_pause():
    store, _ = fresh_store()
    store.upsert_campaign_meta("c", org_id=1, status="live")
    store.set_campaign_paused("c", paused=True, reason="auto")
    meta = store.set_campaign_paused("c", paused=False, reason="auto")
    assert meta["status"] == "live"
    assert meta["paused_reason"] is None


def test_set_paused_unknown_campaign_returns_none():
    store, _ = fresh_store()
    assert store.set_campaign_paused("nope", paused=True) is None


# ----- schedule persistence (Phase 3) -----

def test_set_and_clear_schedule_round_trip():
    store, _ = fresh_store()
    store.upsert_campaign_meta("c", org_id=1, status="live")
    meta = store.set_campaign_schedule("c", kind="daily", hour=9, minute=0,
                                       next_run_at=1_800_000_000.0, target_leads=25)
    assert meta["schedule_enabled"] == 1
    assert meta["schedule_kind"] == "daily" and meta["schedule_hour"] == 9
    assert meta["next_run_at"] == 1_800_000_000.0
    assert meta["schedule_target_leads"] == 25
    # Clear actually NULLs next_run_at (dedicated UPDATE, not the COALESCE upsert).
    cleared = store.clear_campaign_schedule("c")
    assert cleared["schedule_enabled"] == 0
    assert cleared["next_run_at"] is None


def test_due_scheduled_excludes_not_due_paused_archived_and_disabled():
    store, _ = fresh_store()
    # Due + live → returned.
    store.upsert_campaign_meta("due", org_id=1, status="live")
    store.set_campaign_schedule("due", kind="daily", hour=9, minute=0, next_run_at=100.0)
    # Future fire → not yet due.
    store.upsert_campaign_meta("future", org_id=1, status="live")
    store.set_campaign_schedule("future", kind="daily", hour=9, minute=0, next_run_at=9e12)
    # Paused → excluded by the runnable predicate.
    store.upsert_campaign_meta("paused", org_id=1, status="paused")
    store.set_campaign_schedule("paused", kind="daily", hour=9, minute=0, next_run_at=100.0)
    # Archived (still 'live') → excluded.
    store.upsert_campaign_meta("arch", org_id=1, status="live")
    store.set_campaign_schedule("arch", kind="daily", hour=9, minute=0, next_run_at=100.0)
    store.set_campaign_archived("arch", True)
    # Disabled schedule → excluded.
    store.upsert_campaign_meta("off", org_id=1, status="live")

    due = store.due_scheduled_campaigns(now_ts=1000.0)
    assert [d["campaign_id"] for d in due] == ["due"]


def test_advance_scheduled_run_moves_next_and_stamps_last():
    store, _ = fresh_store()
    store.upsert_campaign_meta("c", org_id=1, status="live")
    store.set_campaign_schedule("c", kind="daily", hour=9, minute=0, next_run_at=100.0)
    store.advance_scheduled_run("c", next_run_at=200.0, fired_at=101.0)
    meta = store.get_campaign_meta("c")
    assert meta["next_run_at"] == 200.0
    assert meta["last_scheduled_run_at"] == 101.0


def test_set_schedule_unknown_campaign_returns_none():
    store, _ = fresh_store()
    assert store.set_campaign_schedule("nope", kind="daily", hour=9, minute=0,
                                       next_run_at=1.0) is None


# ----- session_cooldowns (gap #1: self-healing anti-bot cooldown) -----

def test_record_soft_halt_backoff_doubles_each_attempt():
    store, _ = fresh_store()
    try:
        now = 1_700_000_000.0
        expected_deltas = [COOLDOWN_BASE_SECONDS, COOLDOWN_BASE_SECONDS * 2,
                           COOLDOWN_BASE_SECONDS * 4, COOLDOWN_BASE_SECONDS * 8]
        for i, expected_delta in enumerate(expected_deltas, start=1):
            row = store.record_soft_halt("c1", "instagram", "action_block", now=now)
            assert row["attempt"] == i
            assert row["cooldown_until"] - now == pytest.approx(expected_delta)
    finally:
        store.close()


def test_record_soft_halt_backoff_caps_at_max():
    store, _ = fresh_store()
    try:
        now = 1_700_000_000.0
        row = None
        for _ in range(12):        # 15min * 2**11 would blow way past the 6h cap
            row = store.record_soft_halt("c1", "instagram", "canary", now=now)
        assert row["cooldown_until"] - now == pytest.approx(COOLDOWN_MAX_SECONDS)
    finally:
        store.close()


def test_cooldown_persists_and_rehydrates_after_restart():
    """No separate warm-up step exists: a fresh Store(path) — simulating a process
    restart — sees exactly what the prior process last wrote, by simply reading the
    row back."""
    store, path = fresh_store()
    now = 1_700_000_000.0
    store.record_soft_halt("c1", "instagram", "canary", now=now)
    store.close()

    store2 = Store(path)
    try:
        row = store2.get_cooldown("c1", "instagram")
        assert row is not None
        assert row["attempt"] == 1
        assert row["last_kind"] == "canary"
        remaining = store2.cooldown_remaining("c1", "instagram", now=now + 1.0)
        assert remaining == pytest.approx(COOLDOWN_BASE_SECONDS - 1.0)
    finally:
        store2.close()


def test_cooldown_remaining_zero_once_elapsed_but_row_persists():
    store, _ = fresh_store()
    try:
        now = 1_700_000_000.0
        store.record_soft_halt("c1", "instagram", "action_block", now=now)
        past_cooldown = now + COOLDOWN_BASE_SECONDS + 1.0
        assert store.cooldown_remaining("c1", "instagram", now=past_cooldown) == 0.0
        assert store.get_cooldown("c1", "instagram") is not None  # row still there
    finally:
        store.close()


def test_cooldown_remaining_is_zero_when_never_recorded():
    store, _ = fresh_store()
    try:
        assert store.get_cooldown("never", "instagram") is None
        assert store.cooldown_remaining("never", "instagram") == 0.0
    finally:
        store.close()


def test_clear_cooldown_resets_the_streak():
    store, _ = fresh_store()
    try:
        now = 1_700_000_000.0
        store.record_soft_halt("c1", "instagram", "action_block", now=now)
        store.record_soft_halt("c1", "instagram", "action_block", now=now)  # attempt 2
        store.clear_cooldown("c1", "instagram")
        assert store.get_cooldown("c1", "instagram") is None

        row = store.record_soft_halt("c1", "instagram", "action_block", now=now)
        assert row["attempt"] == 1     # starts over, not continuing the old streak
    finally:
        store.close()


def test_clear_cooldown_on_untouched_campaign_is_a_no_op():
    store, _ = fresh_store()
    try:
        store.clear_cooldown("nope", "instagram")   # must not raise
        assert store.get_cooldown("nope", "instagram") is None
    finally:
        store.close()


def test_cooldown_is_scoped_per_campaign_and_platform():
    store, _ = fresh_store()
    try:
        now = 1_700_000_000.0
        store.record_soft_halt("c1", "instagram", "action_block", now=now)
        assert store.get_cooldown("c1", "linkedin") is None   # different platform
        assert store.get_cooldown("c2", "instagram") is None  # different campaign
    finally:
        store.close()
