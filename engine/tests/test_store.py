import json
import os
import sqlite3
import tempfile
import time

from aizu.core.store import SCHEMA_VERSION, Store, SessionCounters


def fresh_store():
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    return Store(path), path


def test_match_idempotent_and_status_preserved():
    store, _ = fresh_store()
    kw = dict(campaign_id="c", reel_id="r", comment_id="cm1", username="u",
              text="hi", lang="en", reason="x", extracted={"intent": "buy"}, tier="local")
    store.upsert_match(score=0.8, **kw)
    store.set_status("c", "cm1", "interested")
    # Re-poll with a new score must NOT reset the human status.
    store.upsert_match(score=0.4, **kw)
    rows = store.matches("c")
    assert len(rows) == 1
    assert rows[0]["status"] == "interested"
    assert rows[0]["score"] == 0.4  # scored fields do refresh
    assert rows[0]["extracted"]["intent"] == "buy"


def test_set_status_reports_whether_row_updated():
    store, _ = fresh_store()
    store.upsert_match(campaign_id="c", reel_id="r", comment_id="cm1", username="u",
                       text="hi", lang="en", score=0.8, reason="x",
                       extracted=None, tier="local")
    assert store.set_status("c", "cm1", "in_progress") is True
    assert store.set_status("c", "missing", "in_progress") is False
    assert store.set_status("other-campaign", "cm1", "in_progress") is False


def test_seen_reels_watermark():
    store, _ = fresh_store()
    assert not store.is_seen("c", "r1")
    store.mark_seen("c", "r1", relevant=True)
    assert store.is_seen("c", "r1")


def test_resume_from_persisted_state():
    store, path = fresh_store()
    store.mark_seen("c", "r1", relevant=True)
    store.set_cursor("c", "r1", "5")
    store.close()
    # New process, same DB → state survives.
    store2 = Store(path)
    assert store2.is_seen("c", "r1")
    assert store2.get_cursor("c", "r1") == "5"


def test_watchlist_ttl():
    store, _ = fresh_store()
    store.add_to_watchlist("c", "r1", ttl_days=10)
    store.add_to_watchlist("c", "r2", ttl_days=-1)  # already expired
    active = store.active_watchlist("c")
    assert "r1" in active and "r2" not in active
    assert store.prune_watchlist("c") == 1


def test_spend_accumulates():
    store, _ = fresh_store()
    store.log_spend("c", "match", 0.01)
    store.log_spend("c", "relevance", 0.02)
    assert abs(store.total_spend("c") - 0.03) < 1e-9


def test_counters_roundtrip():
    store, _ = fresh_store()
    store.start_session("s1", "c")
    store.update_counters("s1", SessionCounters(reels_seen=5, matches=2, feed_health_flag=True))
    s = store.get_session("s1")
    assert s["reels_seen"] == 5 and s["matches"] == 2 and s["feed_health_flag"] == 1


def test_platform_separates_matches():
    """Same campaign + comment_id on two platforms are distinct rows (the PK
    includes platform); a status mark on one must not touch the other."""
    store, _ = fresh_store()
    kw = dict(campaign_id="c", reel_id="r", comment_id="cm1", username="u",
              text="hi", lang="en", score=0.8, reason="x", extracted=None, tier="local")
    store.upsert_match(platform="instagram", **kw)
    store.upsert_match(platform="youtube", **kw)
    rows = store.matches("c")
    assert len(rows) == 2
    assert {r["platform"] for r in rows} == {"instagram", "youtube"}

    store.set_status("c", "cm1", "interested", platform="youtube")
    by_platform = {r["platform"]: r["status"] for r in store.matches("c")}
    assert by_platform["youtube"] == "interested"
    assert by_platform["instagram"] == "new"   # untouched


def test_seen_and_cursor_are_platform_scoped():
    store, _ = fresh_store()
    store.mark_seen("c", "r1", relevant=True, platform="instagram")
    assert store.is_seen("c", "r1", platform="instagram")
    assert not store.is_seen("c", "r1", platform="youtube")   # different namespace
    store.set_cursor("c", "r1", "5", platform="instagram")
    assert store.get_cursor("c", "r1", platform="instagram") == "5"
    assert store.get_cursor("c", "r1", platform="youtube") is None


def test_campaign_brief_roundtrip():
    store, _ = fresh_store()
    assert store.get_campaign_brief("c") is None
    brief = {"platform": "telegram", "threshold": 0.8, "seed_channels": ["product_chat"],
             "relevance_def": "saas product", "match_def": "buyer", "extract_def": "- phone"}
    store.upsert_campaign_brief("c", brief)
    assert store.get_campaign_brief("c") == brief
    # upsert replaces
    store.upsert_campaign_brief("c", {**brief, "threshold": 0.6})
    assert store.get_campaign_brief("c")["threshold"] == 0.6


def _make_legacy_v1_db(path):
    """Build a pre-platform (v1) DB by hand, with one row in each state table."""
    conn = sqlite3.connect(path)
    conn.executescript("""
        CREATE TABLE meta(key TEXT PRIMARY KEY, value TEXT);
        CREATE TABLE matches(campaign_id TEXT NOT NULL, reel_id TEXT NOT NULL,
            comment_id TEXT NOT NULL, session_id TEXT, username TEXT, text TEXT,
            lang TEXT, score REAL, reason TEXT, extracted TEXT,
            status TEXT NOT NULL DEFAULT 'new', tier TEXT, captured_at REAL NOT NULL,
            updated_at REAL NOT NULL, PRIMARY KEY(campaign_id, comment_id));
        CREATE INDEX idx_matches_reel ON matches(campaign_id, reel_id);
        CREATE INDEX idx_matches_status ON matches(campaign_id, status);
        CREATE TABLE seen_reels(campaign_id TEXT NOT NULL, reel_id TEXT NOT NULL,
            first_seen REAL NOT NULL, last_seen REAL NOT NULL, relevant INTEGER,
            author TEXT, caption TEXT, ocr_text TEXT, PRIMARY KEY(campaign_id, reel_id));
        CREATE TABLE comment_cursors(campaign_id TEXT NOT NULL, reel_id TEXT NOT NULL,
            last_cursor TEXT, last_polled REAL, PRIMARY KEY(campaign_id, reel_id));
        CREATE TABLE watchlist(campaign_id TEXT NOT NULL, reel_id TEXT NOT NULL,
            added_at REAL NOT NULL, expires_at REAL NOT NULL,
            match_count INTEGER NOT NULL DEFAULT 0, PRIMARY KEY(campaign_id, reel_id));
        CREATE TABLE sessions(session_id TEXT PRIMARY KEY, campaign_id TEXT NOT NULL,
            started_at REAL NOT NULL, ended_at REAL, status TEXT NOT NULL DEFAULT 'running',
            halt_reason TEXT, reels_seen INTEGER NOT NULL DEFAULT 0,
            already_seen_skips INTEGER NOT NULL DEFAULT 0,
            relevance_passes INTEGER NOT NULL DEFAULT 0, comments_scored INTEGER NOT NULL DEFAULT 0,
            matches INTEGER NOT NULL DEFAULT 0, escalations INTEGER NOT NULL DEFAULT 0,
            spend_usd REAL NOT NULL DEFAULT 0.0, feed_health_flag INTEGER NOT NULL DEFAULT 0);
    """)
    conn.execute("INSERT INTO meta VALUES('schema_version','1')")
    conn.execute("INSERT INTO matches(campaign_id, reel_id, comment_id, status, "
                 "captured_at, updated_at) VALUES('c','r','cm1','confirmed',1.0,1.0)")
    conn.execute("INSERT INTO seen_reels(campaign_id, reel_id, first_seen, last_seen) "
                 "VALUES('c','r',1.0,1.0)")
    conn.execute("INSERT INTO sessions(session_id, campaign_id, started_at) "
                 "VALUES('s1','c',1.0)")
    conn.commit()
    conn.close()


def test_legacy_v1_db_migrates_preserving_data_as_instagram():
    fd, path = tempfile.mkstemp(suffix=".db"); os.close(fd)
    _make_legacy_v1_db(path)

    store = Store(path)   # opening triggers the v1 -> v2 migration
    try:
        rows = store.matches("c")
        assert len(rows) == 1
        assert rows[0]["platform"] == "instagram"     # legacy rows stamped
        assert rows[0]["status"] == "interested"       # v6 remap: confirmed → interested
        assert store.is_seen("c", "r", platform="instagram")
        sess = store.all_sessions("c")
        assert len(sess) == 1 and sess[0]["platform"] == "instagram"
    finally:
        store.close()

    # schema_version bumped, and no leftover legacy tables.
    conn = sqlite3.connect(path)
    ver = conn.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()[0]
    leftovers = conn.execute(
        "SELECT name FROM sqlite_master WHERE name LIKE '%__legacy_v1'").fetchall()
    conn.close()
    assert ver == str(SCHEMA_VERSION) and leftovers == []


def test_migration_is_idempotent():
    """Re-opening an already-migrated DB must not re-run the rebuild or lose rows."""
    fd, path = tempfile.mkstemp(suffix=".db"); os.close(fd)
    _make_legacy_v1_db(path)
    Store(path).close()          # first open migrates
    store = Store(path)          # second open must be a no-op
    try:
        assert len(store.matches("c")) == 1
    finally:
        store.close()


def test_action_logging_and_counts():
    store, _ = fresh_store()
    store.log_action("c", "like", reel_id="r1", target="r1", succeeded=True, session_id="s1")
    store.log_action("c", "like", reel_id="r2", target="r2", succeeded=False, session_id="s1")
    store.log_action("c", "follow", reel_id="r1", target="acme.io",
                     succeeded=True, session_id="s1")
    counts = store.action_counts("c", session_id="s1")
    assert counts.get("like") == 1     # only succeeded actions counted
    assert counts.get("follow") == 1


# ----- v3: panel ops tables + aggregations -----

_V3_TABLES = ("campaign_meta", "team_members", "settings", "integrations")


def _table_names(path):
    conn = sqlite3.connect(path)
    names = {r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
    conn.close()
    return names


def test_fresh_db_is_v3_with_ops_tables():
    _, path = fresh_store()
    names = _table_names(path)
    for t in _V3_TABLES:
        assert t in names


def test_legacy_v1_db_migrates_to_v3_lossless():
    """A hand-built v1 DB must migrate straight to v3 — rows preserved AND the new
    ops tables created in the same open."""
    fd, path = tempfile.mkstemp(suffix=".db"); os.close(fd)
    _make_legacy_v1_db(path)
    store = Store(path)
    try:
        assert store.matches("c")[0]["status"] == "interested"  # v6 remap of legacy 'confirmed'
        assert store.list_team() == []                          # new table usable
        store.set_setting(1, "productName", "X")          # v7: settings are per-org
        assert store.get_settings(1)["productName"] == "X"
    finally:
        store.close()
    names = _table_names(path)
    for t in _V3_TABLES:
        assert t in names
    conn = sqlite3.connect(path)
    ver = conn.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()[0]
    conn.close()
    assert ver == str(SCHEMA_VERSION)


def test_v3_migration_idempotent():
    _, path = fresh_store()
    Store(path).close()           # re-open an already-v3 DB → no-op
    store = Store(path)
    try:
        store.add_team_member(name="A B", email="a@b.com")
        assert len(store.list_team()) == 1
    finally:
        store.close()


def test_matches_by_day_and_hour_bucket_in_tashkent():
    import calendar
    store, _ = fresh_store()
    # 2026-06-10 23:00 UTC = 2026-06-11 04:00 Tashkent → bucketed into the 11th.
    ts = calendar.timegm((2026, 6, 10, 23, 0, 0, 0, 0, 0))
    store._conn.execute(
        "INSERT INTO matches(campaign_id, platform, reel_id, comment_id, status, "
        "captured_at, updated_at) VALUES('c','instagram','r','cm1','new',?,?)", (ts, ts))
    store._conn.commit()
    assert store.matches_by_day("c") == [{"day": "2026-06-11", "n": 1}]
    assert store.matches_by_hour("c") == {4: 1}


def test_aggregations_window_and_funnel():
    store, _ = fresh_store()
    store.start_session("s1", "c")
    store.update_counters("s1", SessionCounters(
        reels_seen=20, relevance_passes=6, comments_scored=40, matches=2))
    store.upsert_match(campaign_id="c", reel_id="r", comment_id="cm1", username="u",
                       text="hi", lang="en", score=0.9, reason="x", extracted=None,
                       tier="local")
    store.set_status("c", "cm1", "interested")
    store.log_spend("c", "match", 0.05)
    store.log_spend("c", "vision", 0.09)
    assert store.won_count("c") == 1
    assert store.scored_count("c") == 40
    assert store.spend_by_stage("c") == {"match": 0.05, "vision": 0.09}
    assert store.funnel_totals("c") == {"reels": 20, "relevant": 6, "scored": 40, "matches": 2}
    assert store.per_campaign_rollup() == [
        {"campaignId": "c", "leads": 1, "won": 1, "spend": 0.14}]


def test_per_campaign_rollup_reports_spend_without_leads():
    """Spend comes from spend_log, never from the existence of leads.

    The rollup used to be built with `FROM matches ... GROUP BY campaign_id`, so a
    campaign that burned budget without capturing a single lead had no row at all
    and the panel defaulted its card to `spent: 0` — while the very same
    /api/reports payload summed that money under spendByStage."""
    store, _ = fresh_store()
    store.log_spend("dry", "vision", 999.0)
    store.upsert_match(campaign_id="wet", reel_id="r", comment_id="cm1", username="u",
                       text="hi", lang="en", score=0.9, reason="x", extracted=None,
                       tier="local")
    rollup = {r["campaignId"]: r for r in store.per_campaign_rollup()}
    assert rollup["dry"] == {"campaignId": "dry", "leads": 0, "won": 0, "spend": 999.0}
    assert rollup["wet"] == {"campaignId": "wet", "leads": 1, "won": 0, "spend": 0.0}


def test_campaign_meta_coalesce_merge():
    store, _ = fresh_store()
    store.upsert_campaign_meta("c", budget_cap=50.0, status="paused")
    merged = store.upsert_campaign_meta("c", goal_target=100)
    assert merged["status"] == "paused"        # untouched field preserved
    assert merged["budget_cap"] == 50.0
    assert merged["goal_target"] == 100


def test_campaign_meta_rejects_bad_status():
    store, _ = fresh_store()
    try:
        store.upsert_campaign_meta("c", status="banana")
        assert False, "expected ValueError"
    except ValueError:
        pass


def test_team_crud_and_unique_email():
    store, _ = fresh_store()
    tid = store.add_team_member(name="Jane Doe", email="j@x.com")
    assert store.list_team()[0]["initials"] == "JD"     # derived from name
    assert store.update_team_member(tid, role="admin") is True
    assert store.list_team()[0]["role"] == "admin"
    try:
        store.add_team_member(name="Other", email="j@x.com")   # dup email
        assert False, "expected IntegrityError"
    except sqlite3.IntegrityError:
        pass
    assert store.delete_team_member(tid) is True
    assert store.delete_team_member(9999) is False


def test_integration_override_upsert():
    store, _ = fresh_store()
    row = store.set_integration(1, "instagram", connected=True, detail="@acct")
    assert row["connected"] == 1 and row["detail"] == "@acct"
    row2 = store.set_integration(1, "instagram", detail="@renamed")   # connected preserved
    assert row2["connected"] == 1 and row2["detail"] == "@renamed"


# ----- v5: users + auth sessions -----

import time as _time

import pytest as _pytest

from aizu.auth import hash_password


def test_fresh_db_has_auth_tables():
    _, path = fresh_store()
    assert {"users", "auth_sessions"} <= _table_names(path)


def test_create_user_and_lookup():
    store, _ = fresh_store()
    uid = store.create_user("a@b.com", hash_password("pw", iterations=1000))
    assert uid >= 1 and store.count_users() == 1
    by_email = store.get_user_by_email("a@b.com")
    by_id = store.get_user_by_id(uid)
    assert by_email["id"] == uid and by_id["email"] == "a@b.com"
    assert store.get_user_by_email("missing@b.com") is None


def test_create_user_rejects_duplicate_email():
    store, _ = fresh_store()
    store.create_user("dup@b.com", hash_password("pw", iterations=1000))
    with _pytest.raises(sqlite3.IntegrityError):
        store.create_user("dup@b.com", hash_password("pw2", iterations=1000))


def test_auth_session_lifecycle_and_expiry():
    store, _ = fresh_store()
    uid = store.create_user("s@b.com", hash_password("pw", iterations=1000))
    now = _time.time()
    store.create_auth_session("tok-live", uid, now + 3600)
    store.create_auth_session("tok-expired", uid, now - 1)

    live = store.get_auth_session_user("tok-live")
    assert live["id"] == uid and live["email"] == "s@b.com"
    assert store.get_auth_session_user("tok-expired") is None   # expired → anonymous
    assert store.get_auth_session_user("tok-unknown") is None

    assert store.delete_auth_session("tok-live") is True
    assert store.get_auth_session_user("tok-live") is None
    assert store.delete_auth_session("tok-live") is False        # already gone


def test_purge_expired_auth_sessions():
    store, _ = fresh_store()
    uid = store.create_user("p@b.com", hash_password("pw", iterations=1000))
    now = _time.time()
    store.create_auth_session("live", uid, now + 3600)
    store.create_auth_session("dead", uid, now - 10)
    assert store.purge_expired_auth_sessions() == 1
    assert store.get_auth_session_user("live") is not None


def test_deleting_user_cascades_to_sessions():
    store, path = fresh_store()
    uid = store.create_user("c@b.com", hash_password("pw", iterations=1000))
    store.create_auth_session("tok", uid, _time.time() + 3600)
    # foreign_keys=ON (set in Store.__init__) makes the session row cascade-delete.
    with store._tx() as c:
        c.execute("DELETE FROM users WHERE id=?", (uid,))
    assert store.get_auth_session_user("tok") is None


def test_create_user_with_session_is_atomic_happy_path():
    store, _ = fresh_store()
    uid = store.create_user_with_session(
        "a@b.com", hash_password("pw", iterations=1000), "raw-tok", _time.time() + 3600)
    assert store.count_users() == 1
    assert store.get_auth_session_user("raw-tok")["id"] == uid


def test_create_user_with_session_rolls_back_on_session_conflict():
    store, _ = fresh_store()
    store.create_user_with_session(
        "a@b.com", hash_password("pw", iterations=1000), "dup-tok", _time.time() + 3600)
    # Reusing the same token for a different email collides on the auth_sessions PK;
    # the whole transaction must roll back, leaving no orphaned second user.
    with _pytest.raises(sqlite3.IntegrityError):
        store.create_user_with_session(
            "b@b.com", hash_password("pw", iterations=1000), "dup-tok", _time.time() + 3600)
    assert store.count_users() == 1
    assert store.get_user_by_email("b@b.com") is None


# ----- v6: lead Kanban status set + audit log + notes -----

def _seed_match(store, comment_id="cm1", platform="instagram", score=0.9):
    store.upsert_match(campaign_id="c", reel_id="r", comment_id=comment_id, username="u",
                       text="hi", lang="en", score=score, reason="x", extracted=None,
                       tier="local", platform=platform)


def test_status_remap_v5_to_v6():
    """An old-vocabulary DB remaps its status values exactly once on open."""
    fd, path = tempfile.mkstemp(suffix=".db"); os.close(fd)
    store = Store(path)
    for cid, st in (("cm_new", "new"), ("cm_rev", "new"), ("cm_conf", "new"),
                    ("cm_disc", "new")):
        _seed_match(store, comment_id=cid)
    # Force the old vocabulary directly + pretend the DB is v5, then reopen.
    store._conn.execute("UPDATE matches SET status='reviewing' WHERE comment_id='cm_rev'")
    store._conn.execute("UPDATE matches SET status='confirmed' WHERE comment_id='cm_conf'")
    store._conn.execute("UPDATE matches SET status='discarded' WHERE comment_id='cm_disc'")
    store._conn.execute("UPDATE meta SET value='5' WHERE key='schema_version'")
    store._conn.commit()
    store.close()

    store2 = Store(path)   # opening runs the v6 remap
    try:
        by_id = {m["comment_id"]: m["status"] for m in store2.matches("c")}
        assert by_id == {"cm_new": "new", "cm_rev": "in_progress",
                         "cm_conf": "interested", "cm_disc": "archived"}
    finally:
        store2.close()


def test_set_status_writes_audit_row_with_user():
    store, _ = fresh_store()
    _seed_match(store)
    store.set_status("c", "cm1", "in_progress", user={"id": 7, "email": "a@b.com"})
    hist = store.status_history("c", "cm1")
    assert len(hist) == 1
    assert hist[0]["fromStatus"] == "new" and hist[0]["toStatus"] == "in_progress"
    assert hist[0]["by"] == "a@b.com" and hist[0]["byId"] == 7


def test_set_status_forced_reason_required():
    store, _ = fresh_store()
    _seed_match(store)
    for terminal in ("closed", "couldnt_connect", "archived"):
        _seed_match(store)  # reset to a known 'new' is unnecessary; same row reused
        with _pytest.raises(ValueError):
            store.set_status("c", "cm1", terminal, user={"id": 1, "email": "a@b"})
    # With a reason it succeeds and stores the reason on the audit row.
    assert store.set_status("c", "cm1", "closed", user={"id": 1, "email": "a@b"},
                            reason="  no budget  ") is True
    assert store.status_history("c", "cm1")[-1]["reason"] == "no budget"


def test_set_status_noop_skips_audit():
    store, _ = fresh_store()
    _seed_match(store)
    assert store.set_status("c", "cm1", "new", user={"id": 1, "email": "a@b"}) is True
    assert store.status_history("c", "cm1") == []     # no-op logged nothing
    assert store.set_status("c", "cm1", "new", user={"id": 1, "email": "a@b"},
                            log_noop=True) is True
    assert len(store.status_history("c", "cm1")) == 1  # explicit opt-in logs it


def test_add_and_delete_note_owner_only():
    store, _ = fresh_store()
    _seed_match(store)
    note = store.add_note("c", "cm1", "called, left voicemail",
                          author={"id": 1, "email": "a@b"})
    assert note["id"] >= 1 and note["body"] == "called, left voicemail"
    assert len(store.notes_for("c", "cm1")) == 1
    assert store.delete_note(note["id"], 2) == "forbidden"     # not the author
    assert store.delete_note(9999, 1) == "not_found"
    assert store.delete_note(note["id"], 1) == "deleted"       # author may delete
    assert store.notes_for("c", "cm1") == []


def test_add_note_validates_body():
    store, _ = fresh_store()
    _seed_match(store)
    with _pytest.raises(ValueError):
        store.add_note("c", "cm1", "   ", author={"id": 1, "email": "a@b"})
    with _pytest.raises(ValueError):
        store.add_note("c", "cm1", "x" * 5000, author={"id": 1, "email": "a@b"})


def test_delete_note_keeps_audit_reason():
    store, _ = fresh_store()
    _seed_match(store)
    store.set_status("c", "cm1", "closed", user={"id": 1, "email": "a@b"}, reason="done")
    note = store.add_note("c", "cm1", "done", author={"id": 1, "email": "a@b"})
    store.delete_note(note["id"], 1)
    assert store.status_history("c", "cm1")[-1]["reason"] == "done"   # audit intact


def test_status_breakdown_and_pipeline():
    store, _ = fresh_store()
    for i, st in enumerate(["new", "in_progress", "interested", "closed"]):
        _seed_match(store, comment_id=f"cm{i}")
        if st != "new":
            reason = "r" if st in ("closed", "couldnt_connect", "archived") else None
            store.set_status("c", f"cm{i}", st, user={"id": 1, "email": "a@b"}, reason=reason)
    b = store.status_breakdown("c")
    assert b["new"] == 1 and b["interested"] == 1 and b["closed"] == 1
    assert set(b) == {"new", "in_progress", "interested", "closed",
                      "couldnt_connect", "archived", "needs_review"}
    p = store.pipeline_conversion("c")
    assert p["total"] == 4 and p["won"] == 2           # interested + closed
    assert p["winRate"] == 0.5


def test_status_changes_by_user():
    store, _ = fresh_store()
    _seed_match(store, comment_id="cm1")
    _seed_match(store, comment_id="cm2")
    store.set_status("c", "cm1", "in_progress", user={"id": 1, "email": "a@b"})
    store.set_status("c", "cm2", "in_progress", user={"id": 1, "email": "a@b"})
    store.set_status("c", "cm2", "interested", user={"id": 2, "email": "c@d"})
    rows = {r["email"]: r["changes"] for r in store.status_changes_by_user("c")}
    assert rows == {"a@b": 2, "c@d": 1}


def test_needs_attention():
    store, _ = fresh_store()
    now = _time.time()
    # A lead stuck in in_progress with an old change.
    _seed_match(store, comment_id="stuck")
    store.set_status("c", "stuck", "in_progress", user={"id": 1, "email": "a@b"})
    store._conn.execute(
        "UPDATE lead_status_changes SET created_at=? WHERE comment_id='stuck'",
        (now - 30 * 86400,))
    store._conn.execute("UPDATE matches SET updated_at=? WHERE comment_id='stuck'",
                        (now - 30 * 86400,))
    # A couldnt_connect lead.
    _seed_match(store, comment_id="cc")
    store.set_status("c", "cc", "couldnt_connect", user={"id": 1, "email": "a@b"}, reason="x")
    # An idle, still-open, never-touched lead (old capture).
    _seed_match(store, comment_id="idle")
    store._conn.execute("UPDATE matches SET captured_at=? WHERE comment_id='idle'",
                        (now - 30 * 86400,))
    store._conn.commit()
    a = store.needs_attention("c", now=now)
    assert a["stuckInProgress"] == 1
    assert a["couldntConnectTotal"] == 1
    assert a["noActivity"] >= 1


# ----- v10: run activity feed -----

def test_emit_and_fetch_run_events_cursor():
    """Events page on the global insertion id; after_id returns only newer rows."""
    store, _ = fresh_store()
    for i in range(1, 4):
        store.emit_run_event("run-1", i, "lifecycle", "info", f"step {i}",
                             campaign_id="c", session_id="s1", org_id=7)
    rows = store.fetch_run_events("run-1")
    assert [r["message"] for r in rows] == ["step 1", "step 2", "step 3"]
    assert [r["seq"] for r in rows] == [1, 2, 3]
    # Cursor on id: after the first row's id returns only the rest.
    after = rows[0]["id"]
    tail = store.fetch_run_events("run-1", after_id=after)
    assert [r["message"] for r in tail] == ["step 2", "step 3"]
    # A cursor past the end returns nothing.
    assert store.fetch_run_events("run-1", after_id=rows[-1]["id"]) == []
    # C7: no session row was created for "s1" → platform LEFT JOINs to null.
    assert all(r["platform"] is None for r in rows)


def test_fetch_run_events_returns_platform_from_session():
    """An event whose session_id resolves to a sessions row carries that session's
    platform (multi-platform plan C7) — the join, no schema change."""
    store, _ = fresh_store()
    store.start_session("sess-ig", "c", "instagram", run_id="run-1", org_id=1)
    store.emit_run_event("run-1", 1, "lifecycle", "info", "hi",
                         campaign_id="c", session_id="sess-ig", org_id=1)
    rows = store.fetch_run_events("run-1")
    assert rows[0]["platform"] == "instagram"


def test_fetch_run_events_is_org_scoped_and_run_scoped():
    store, _ = fresh_store()
    store.emit_run_event("run-a", 1, "lifecycle", "info", "a", org_id=1)
    store.emit_run_event("run-b", 1, "lifecycle", "info", "b", org_id=2)
    # run scoping: only this run's rows.
    assert [r["message"] for r in store.fetch_run_events("run-a")] == ["a"]
    # org scoping: a foreign org sees nothing even with the right run_id.
    assert store.fetch_run_events("run-a", org_id=2) == []
    assert [r["message"] for r in store.fetch_run_events("run-a", org_id=1)] == ["a"]


def test_fetch_run_events_respects_limit():
    store, _ = fresh_store()
    for i in range(1, 11):
        store.emit_run_event("run-1", i, "comments", "info", f"e{i}", org_id=1)
    assert len(store.fetch_run_events("run-1", limit=4)) == 4


def test_emit_run_event_resolves_org_from_campaign():
    """org_id falls back to the campaign's owner when not passed (registry truth)."""
    store, _ = fresh_store()
    store.upsert_campaign_meta("camp-x", org_id=42, status="live")
    store.emit_run_event("run-1", 1, "lifecycle", "info", "hi", campaign_id="camp-x")
    assert store.fetch_run_events("run-1", org_id=42)[0]["message"] == "hi"
    assert store.fetch_run_events("run-1", org_id=99) == []


def test_emit_run_event_never_raises_on_bad_input():
    """The feed is defensive: a broken insert is swallowed, never crashes the run."""
    store, _ = fresh_store()
    store.close()  # force the connection to be unusable
    # Must not raise despite the closed connection.
    store.emit_run_event("run-1", 1, "lifecycle", "info", "hi", org_id=1)


def test_start_session_stamps_run_id_and_org():
    store, _ = fresh_store()
    store.upsert_campaign_meta("camp-x", org_id=42, status="live")
    store.start_session("sess-1", "camp-x", run_id="run-1")
    row = store.get_session("sess-1")
    assert row["run_id"] == "run-1"
    assert row["org_id"] == 42
    # Backwards-compatible: the legacy 3-arg call still works (run_id/org_id NULL).
    store.start_session("sess-2", "unregistered")
    row2 = store.get_session("sess-2")
    assert row2["run_id"] is None and row2["org_id"] is None


def test_sessions_for_run_aggregates_batch():
    """A batch (run-all) run spans several sessions sharing one run_id."""
    store, _ = fresh_store()
    store.upsert_campaign_meta("c1", org_id=1, status="live")
    store.upsert_campaign_meta("c2", org_id=1, status="live")
    store.start_session("s1", "c1", run_id="run-1")
    store.start_session("s2", "c2", run_id="run-1")
    store.start_session("s3", "c1", run_id="run-2")
    sessions = store.sessions_for_run("run-1")
    assert {s["session_id"] for s in sessions} == {"s1", "s2"}
    assert store.sessions_for_run("run-1", org_id=99) == []


def test_run_events_open_flags_joins_run_sessions():
    store, _ = fresh_store()
    store.upsert_campaign_meta("c1", org_id=1, status="live")
    store.start_session("s1", "c1", run_id="run-1")
    store.raise_flag("feed_health", "soft", "tapping out", campaign_id="c1", session_id="s1")
    flags = store.run_events_open_flags("run-1")
    assert len(flags) == 1 and flags[0]["kind"] == "feed_health"
    assert store.run_events_open_flags("run-1", org_id=99) == []


def test_prune_run_events_drops_old_and_excess_runs():
    store, _ = fresh_store()
    # An old event (beyond the TTL) is pruned.
    store.emit_run_event("old-run", 1, "lifecycle", "info", "stale", org_id=1)
    store._conn.execute("UPDATE run_events SET created_at=? WHERE run_id='old-run'",
                        (_time.time() - 30 * 86400,))
    store._conn.commit()
    # A fresh event survives.
    store.emit_run_event("fresh-run", 1, "lifecycle", "info", "live", org_id=1)
    store.prune_run_events()
    assert store.fetch_run_events("old-run") == []
    assert len(store.fetch_run_events("fresh-run")) == 1
    # keep_runs cap: only the most recent N runs survive.
    for i in range(5):
        store.emit_run_event(f"r{i}", 1, "lifecycle", "info", "x", org_id=1)
    store.prune_run_events(keep_runs=2)
    surviving = {r["run_id"] for r in store._conn.execute(
        "SELECT DISTINCT run_id FROM run_events").fetchall()}
    assert len(surviving) == 2


def test_v10_self_heal_on_upgrading_db():
    """A pre-v10 DB (sessions without run_id/org_id, no run_events) self-heals on
    open: the table is created, the columns are added, and prior rows are preserved."""
    fd, path = tempfile.mkstemp(suffix=".db"); os.close(fd)
    _make_legacy_v1_db(path)   # builds a sessions table lacking run_id/org_id, no run_events
    store = Store(path)
    try:
        cols = {r[1] for r in store._conn.execute("PRAGMA table_info(sessions)").fetchall()}
        assert "run_id" in cols and "org_id" in cols
        tables = {r[0] for r in store._conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
        assert "run_events" in tables
        # The pre-existing session row survived the upgrade.
        assert store.get_session("s1") is not None
        # The new feed methods are usable post-upgrade.
        store.emit_run_event("run-1", 1, "lifecycle", "info", "ok", org_id=1)
        assert len(store.fetch_run_events("run-1")) == 1
    finally:
        store.close()
    conn = sqlite3.connect(path)
    ver = conn.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()[0]
    conn.close()
    assert ver == str(SCHEMA_VERSION)


# ---- v18: Uzbek-only local STT (seen_reels.transcript*/sessions.transcriptions) ----


def test_fresh_db_is_v18_with_stt_columns():
    """A brand-new DB gets the v18 STT columns and the v19 video-analysis columns
    straight from SCHEMA (no self-heal needed) and stamps schema_version."""
    store, path = fresh_store()
    seen_cols = {r[1] for r in store._conn.execute(
        "PRAGMA table_info(seen_reels)").fetchall()}
    assert {"transcript", "transcript_lang", "transcript_ms"} <= seen_cols
    # v19 video-analysis columns land from SCHEMA on a fresh DB too.
    assert {"video_analyzed", "video_analysis_summary"} <= seen_cols
    session_cols = {r[1] for r in store._conn.execute(
        "PRAGMA table_info(sessions)").fetchall()}
    assert "transcriptions" in session_cols
    assert "video_analyses" in session_cols
    conn = sqlite3.connect(path)
    ver = conn.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()[0]
    conn.close()
    assert ver == str(SCHEMA_VERSION)


def test_v18_self_heal_stt_columns_on_upgrading_db():
    """A pre-v18 DB (built by hand, no transcript*/transcriptions columns) self-heals
    on open: the columns are added via ALTER TABLE and prior rows survive — same
    self-heal contract already proven for v10 (run_events) and v17 (found_by_models)."""
    fd, path = tempfile.mkstemp(suffix=".db"); os.close(fd)
    _make_legacy_v1_db(path)   # a pre-v2 DB predates every additive column, incl. v18's
    store = Store(path)
    try:
        seen_cols = {r[1] for r in store._conn.execute(
            "PRAGMA table_info(seen_reels)").fetchall()}
        assert {"transcript", "transcript_lang", "transcript_ms"} <= seen_cols
        session_cols = {r[1] for r in store._conn.execute(
            "PRAGMA table_info(sessions)").fetchall()}
        assert "transcriptions" in session_cols
        # The pre-existing seen_reels row survived the ALTER TABLE self-heal, with
        # the new columns taking NULL (no backfill, matches every other additive
        # column added this way).
        row = store._conn.execute(
            "SELECT transcript, transcript_lang, transcript_ms FROM seen_reels "
            "WHERE campaign_id='c' AND reel_id='r'").fetchone()
        assert row is not None
        assert tuple(row) == (None, None, None)
        # The new mark_seen()/reels() STT kwargs are usable post-upgrade.
        store.mark_seen("c", "r", transcript="salom dunyo", transcript_lang="uz")
        reels = store.reels("c")
        assert reels[0]["transcript"] == "salom dunyo"
        assert reels[0]["transcript_lang"] == "uz"
    finally:
        store.close()
    conn = sqlite3.connect(path)
    ver = conn.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()[0]
    conn.close()
    assert ver == str(SCHEMA_VERSION)


def test_mark_seen_transcript_roundtrips_and_sticky_merges():
    """transcript/transcript_lang round-trip through mark_seen()/reels(), and a
    later mark_seen() call with transcript=None must NOT blank a previously
    written transcript — the same COALESCE sticky-merge semantics already proven
    for ocr_text."""
    store, _ = fresh_store()
    store.mark_seen("c", "r1", relevant=True, caption="salom",
                    transcript="mahsulot narxi qancha", transcript_lang="uz")
    reels = store.reels("c")
    assert len(reels) == 1
    assert reels[0]["transcript"] == "mahsulot narxi qancha"
    assert reels[0]["transcript_lang"] == "uz"

    # A later poll of the same reel (e.g. relevance re-check) omits transcript —
    # must not blank what was already captured.
    store.mark_seen("c", "r1", relevant=True, caption="salom")
    reels = store.reels("c")
    assert reels[0]["transcript"] == "mahsulot narxi qancha"
    assert reels[0]["transcript_lang"] == "uz"


def test_mark_seen_without_transcript_leaves_it_null():
    """A campaign that never transcribes (STT gate off) must leave transcript*
    NULL — no accidental default text."""
    store, _ = fresh_store()
    store.mark_seen("c", "r1", relevant=True)
    reels = store.reels("c")
    assert reels[0]["transcript"] is None
    assert reels[0]["transcript_lang"] is None


def test_transcriptions_counter_roundtrips():
    """SessionCounters.transcriptions (mirrors escalations) persists through
    update_counters()/get_session() — the same counter parity already proven for
    reels_seen/matches/feed_health_flag in test_counters_roundtrip."""
    store, _ = fresh_store()
    store.start_session("s1", "c")
    store.update_counters("s1", SessionCounters(transcriptions=3, matches=1))
    s = store.get_session("s1")
    assert s["transcriptions"] == 3 and s["matches"] == 1


# ---- v20: session liveness heartbeat (SessionWatchdog hang-prevention fix) ----


def test_start_session_seeds_heartbeat_and_pid():
    store, _ = fresh_store()
    before = time.time()
    store.start_session("s1", "c")
    s = store.get_session("s1")
    assert s["pid"] == os.getpid()
    assert s["last_activity_at"] is not None
    assert s["last_activity_at"] >= before


def test_update_counters_bumps_last_activity_at():
    store, _ = fresh_store()
    store.start_session("s1", "c")
    seeded = store.get_session("s1")["last_activity_at"]
    # Force the clock forward so the bump is observably different, then confirm
    # a normal counters flush (every engine's periodic Session._flush) advances
    # the heartbeat — this is the ONLY thing that keeps a live session's
    # last_activity_at fresh; no other call site needs to touch it.
    with store._tx() as c:
        c.execute("UPDATE sessions SET last_activity_at=? WHERE session_id=?",
                  (seeded - 1000, "s1"))
    store.update_counters("s1", SessionCounters(reels_seen=1))
    bumped = store.get_session("s1")["last_activity_at"]
    assert bumped > seeded - 1000


def test_find_stalled_sessions_flags_only_the_quiet_one():
    store, _ = fresh_store()
    store.start_session("s-fresh", "c")
    store.start_session("s-stale", "c")
    now = time.time()
    with store._tx() as c:
        c.execute("UPDATE sessions SET last_activity_at=? WHERE session_id=?",
                  (now - 1000, "s-stale"))
    stalled = store.find_stalled_sessions(stall_timeout_s=180, now=now)
    assert [r["session_id"] for r in stalled] == ["s-stale"]


def test_find_stalled_sessions_falls_back_to_started_at_when_never_bumped():
    """A pre-v20 row (or one whose heartbeat was never bumped) has
    last_activity_at NULL — the fallback to started_at must still flag it once
    it's old enough, not silently exempt it forever."""
    store, path = fresh_store()
    store.start_session("s1", "c")
    now = time.time()
    conn = sqlite3.connect(path)
    conn.execute(
        "UPDATE sessions SET started_at=?, last_activity_at=NULL WHERE session_id=?",
        (now - 1000, "s1"))
    conn.commit()
    conn.close()
    store2 = Store(path)
    stalled = store2.find_stalled_sessions(stall_timeout_s=180, now=now)
    assert [r["session_id"] for r in stalled] == ["s1"]


def test_find_stalled_sessions_ignores_non_running_rows():
    store, _ = fresh_store()
    store.start_session("s1", "c")
    store.end_session("s1", "completed")
    now = time.time()
    with store._tx() as c:
        c.execute("UPDATE sessions SET last_activity_at=? WHERE session_id=?",
                  (now - 1000, "s1"))
    assert store.find_stalled_sessions(stall_timeout_s=180, now=now) == []


# ----- v27: matches.intent ------------------------------------------------------

def test_upsert_match_intent_survives_a_repoll_that_derives_nothing():
    """Since v27 the intent is the ONLY lead prose an org sees, so a re-poll that
    derives nothing must never blank it — unlike `reason`, which always takes the
    latest verdict. `derive_intent`'s documented last resort is `""`, and SQL `''`
    is NOT NULL, so a bare COALESCE would happily overwrite a good stored line:
    empty/whitespace has to bind as NULL for the COALESCE to actually protect it."""
    store, _ = fresh_store()
    kw = dict(campaign_id="c1", reel_id="r1", comment_id="m1", username="u",
              text="t", lang="en", extracted=None, tier="local")
    store.upsert_match(score=0.9, reason="first",
                       intent="Wants red sneakers, size 42", **kw)
    assert store.matches("c1")[0]["intent"] == "Wants red sneakers, size 42"

    # A re-poll with no intent at all: the intent survives, the reason refreshes.
    store.upsert_match(score=0.5, reason="second", **kw)
    row = store.matches("c1")[0]
    assert row["intent"] == "Wants red sneakers, size 42"
    assert row["reason"] == "second"

    # derive_intent's last-resort "" (and a whitespace-only line) must not clobber.
    store.upsert_match(score=0.5, reason="third", intent="", **kw)
    assert store.matches("c1")[0]["intent"] == "Wants red sneakers, size 42"
    store.upsert_match(score=0.5, reason="fourth", intent="   ", **kw)
    assert store.matches("c1")[0]["intent"] == "Wants red sneakers, size 42"

    # A fresh, non-empty intent DOES win — the rule is "never blank", not "never change".
    store.upsert_match(score=0.5, reason="fifth", intent="Wants size 43", **kw)
    assert store.matches("c1")[0]["intent"] == "Wants size 43"


def test_v27_intent_column_is_added_to_a_legacy_db():
    """A pre-v27 DB gains `matches.intent` on open and its existing rows take NULL,
    which org-facing readers render as a neutral placeholder rather than a guess
    derived from the raw comment."""
    store, path = fresh_store()
    # Seed the row through the normal path first, so it carries every default a real
    # pre-v27 row has (captured_at, status, ...) rather than a hand-built stub.
    store.upsert_match(campaign_id="c1", reel_id="r1", comment_id="old-1",
                       username="u", text="t", lang="en", score=0.9, reason="x",
                       extracted=None, tier="local", platform="instagram")
    store.close()
    conn = sqlite3.connect(path)
    # Simulate a v26 database: drop the column and wind the recorded version back.
    conn.execute("ALTER TABLE matches DROP COLUMN intent")
    conn.execute("UPDATE meta SET value='26' WHERE key='schema_version'")
    conn.commit()
    conn.close()

    store = Store(path)
    try:
        cols = {r[1] for r in store._conn.execute("PRAGMA table_info(matches)")}
        assert "intent" in cols
        assert store._conn.execute(
            "SELECT value FROM meta WHERE key='schema_version'").fetchone()[0] \
            == str(SCHEMA_VERSION)
        # The legacy row survives the migration and reads as "not captured".
        legacy = store.matches("c1")[0]
        assert legacy["comment_id"] == "old-1" and legacy["intent"] is None
        # ...and it can take an intent on the next re-poll.
        store.upsert_match(campaign_id="c1", reel_id="r1", comment_id="old-1",
                           username="u", text="t", lang="en", score=0.9, reason="x",
                           extracted=None, tier="local", platform="instagram",
                           intent="Wants a quote")
        assert store.matches("c1")[0]["intent"] == "Wants a quote"
    finally:
        store.close()


# --- v27 reveal metering (the audit trail IS the meter) ----------------------

def _reveal_row(store, org_id, campaign_id, platform, comment_id, result,
                *, actor=1):
    """One `reveal_lead` audit row, written exactly the way the endpoint writes it."""
    store.record_audit(org_id, actor, "reveal_lead",
                       target=f"{campaign_id}|{platform}|{comment_id}",
                       detail=Store.reveal_audit_detail(campaign_id, platform, result))


def test_reveal_meter_counts_distinct_leads_not_calls():
    """The drawer never caches a revealed lead, so it re-reveals on every open. A
    call-counting meter would spend a Free org's ten-lead month on one lead."""
    store, _ = fresh_store()
    try:
        for _ in range(5):
            _reveal_row(store, 1, "c1", "instagram", "cm1", "revealed")
        _reveal_row(store, 1, "c1", "instagram", "cm2", "revealed")
        assert store.count_reveals_this_period(1, 0.0) == 2
    finally:
        store.close()


def test_reveal_meter_ignores_every_outcome_but_revealed():
    """A denial, a 404 and a refusal at the cap are all audited — and none of them
    handed out an identity, so none may cost allowance. Otherwise anyone could burn
    an org's month with noise, and the cap would meter its own refusals."""
    store, _ = fresh_store()
    try:
        for result in ("denied", "not_found", "capped"):
            _reveal_row(store, 1, "c1", "instagram", f"cm-{result}", result)
        assert store.count_reveals_this_period(1, 0.0) == 0
        assert store.lead_revealed_this_period(1, "c1|instagram|cm-denied", 0.0) is False
    finally:
        store.close()


def test_reveal_meter_is_org_scoped_and_period_scoped():
    store, _ = fresh_store()
    try:
        _reveal_row(store, 1, "c1", "instagram", "cm1", "revealed")
        _reveal_row(store, 2, "c9", "instagram", "cm9", "revealed")
        assert store.count_reveals_this_period(1, 0.0) == 1     # not the other org's
        # A window that starts in the future contains nothing: the period anchor is
        # what makes the allowance renew rather than accumulate forever.
        assert store.count_reveals_this_period(1, time.time() + 3600) == 0
    finally:
        store.close()


def test_a_forged_campaign_id_cannot_fake_a_revealed_outcome():
    """The outcome lives inside a JSON TEXT column, so the meter matches it with LIKE
    — and the campaign id in the same blob is attacker-controlled. JSON escapes an
    embedded quote, so a value that spells out the needle still cannot produce the
    UNESCAPED quote it starts with."""
    store, _ = fresh_store()
    try:
        forged = 'x", "result": "revealed'
        _reveal_row(store, 1, forged, "instagram", "cm1", "denied")
        assert store.count_reveals_this_period(1, 0.0) == 0
    finally:
        store.close()


def test_lead_revealed_this_period_is_the_free_re_reveal_check():
    store, _ = fresh_store()
    try:
        _reveal_row(store, 1, "c1", "instagram", "cm1", "revealed")
        assert store.lead_revealed_this_period(1, "c1|instagram|cm1", 0.0) is True
        assert store.lead_revealed_this_period(1, "c1|instagram|cm2", 0.0) is False
        # Same predicate as the counter, so the two can never disagree about a lead.
        assert store.lead_revealed_this_period(2, "c1|instagram|cm1", 0.0) is False
    finally:
        store.close()


# --- v27 per-run lead numbers in bulk (the superadmin run picker) ------------

def test_lead_counts_by_run_counts_rows_not_session_counters():
    """`sessions.matches` is a progress counter that overshoots (it increments once
    per POST after a whole comment batch), so the delivered number has to be a real
    row count — the same join `matches_for_run` uses."""
    store, _ = fresh_store()
    try:
        store.upsert_campaign_meta("c1", org_id=1)
        store.start_session("s1", "c1", "instagram", run_id="run-1")
        for i in range(3):
            store.upsert_match(campaign_id="c1", reel_id="r1", comment_id=f"cm{i}",
                               username=f"u{i}", text="t", lang="en", score=0.9,
                               reason="x", extracted=None, tier="local",
                               platform="instagram", session_id="s1")
        store.update_counters("s1", SessionCounters(matches=99))   # a lying counter
        assert store.lead_counts_by_run(1) == {"run-1": 3}
        # A run with no rows is simply absent — the caller reads that as the 0 it is.
        assert store.lead_counts_by_run(1).get("run-never") is None
    finally:
        store.close()


def test_match_event_details_by_run_returns_only_the_match_events():
    store, _ = fresh_store()
    try:
        store.upsert_campaign_meta("c1", org_id=1)
        store.emit_run_event("run-1", 1, "comments", "success", "Match: @a",
                             campaign_id="c1", org_id=1,
                             detail=json.dumps({"username": "a", "reelId": "r1"}))
        store.emit_run_event("run-1", 2, "feed_walk", "info", "walking",
                             campaign_id="c1", org_id=1,
                             detail=json.dumps({"reelsSeen": 4}))
        store.emit_run_event("run-1", 3, "comments", "info", "Scoring 3 comments",
                             campaign_id="c1", org_id=1, detail=None)
        store.emit_run_event("run-2", 1, "comments", "success", "Match: @b",
                             campaign_id="c1", org_id=1,
                             detail=json.dumps({"username": "b", "reelId": "r2"}))
        out = store.match_event_details_by_run(1, ["run-1", "run-2"])
        assert list(out) == ["run-1", "run-2"]
        assert len(out["run-1"]) == 1 and "a" in out["run-1"][0]
        # Another org's id never resolves, and an empty list is not a SQL syntax error.
        assert store.match_event_details_by_run(2, ["run-1"]) == {}
        assert store.match_event_details_by_run(1, []) == {}
    finally:
        store.close()


def test_run_event_runs_lists_a_run_that_has_no_session_row():
    """The picker's third source. A fleet job mirrors its sessions at ACK, so a run
    that dead-lettered has none — but its events landed on the heartbeat, and without
    this the picker would hide the very run whose log is all that survived it."""
    store, _ = fresh_store()
    try:
        store.upsert_campaign_meta("c1", org_id=1)
        store.emit_run_event("run-dead", 1, "comments", "success", "Match: @a",
                             campaign_id="c1", org_id=1, session_id="w-1",
                             detail=json.dumps({"username": "a", "reelId": "r1"}))
        store.emit_run_event("run-dead", 2, "comments", "success", "Match: @b",
                             campaign_id="c1", org_id=1, session_id="w-2",
                             detail=json.dumps({"username": "b", "reelId": "r1"}))
        rows = store.run_event_runs(1)
    finally:
        store.close()
    assert len(rows) == 1
    row = rows[0]
    assert row["runId"] == "run-dead" and row["campaignId"] == "c1"
    assert row["sessions"] == 2                      # one run id spans many sessions
    assert row["firstAt"] <= row["lastAt"]


# ----- v28: matches.lead_token (the opaque org-facing lead key) ------------------

_V28_SEED = [
    # The four platforms whose comment id is a PERMALINK, spelled the way their own
    # feeds compose it — reddit/feed.py:209, youtube/feed.py:339, telegram/feed.py:160
    # (whose `reel_id` is itself "{channel}/{message}"), x/parsers.py:157.
    ("reddit", "t3_post", "t3_post/t1_xyz"),
    ("youtube", "vid123", "vid123/UgxABC"),
    ("telegram", "channel/55", "channel/55/61"),
    ("x", "1700000000000000001", "1900000000000000009"),
    ("instagram", "reel-a", "c-ig-1"),
]


def _make_v27_matches_db():
    """A database that looks like it was written by the last release before v28:
    `matches` with no `lead_token`, no unique index over it, version stamped 27.

    Built by seeding through the NORMAL path and then winding the column back, the
    same way `test_v27_intent_column_is_added_to_a_legacy_db` does, so the rows carry
    every default a real pre-v28 row has (captured_at, status, org attribution) rather
    than the subset a hand-built INSERT would remember to set.
    """
    store, path = fresh_store()
    store.upsert_campaign_meta("c1", org_id=1)
    for platform, reel_id, comment_id in _V28_SEED:
        store.upsert_match(campaign_id="c1", reel_id=reel_id, comment_id=comment_id,
                           username="u", text="t", lang="en", score=0.9, reason="x",
                           extracted=None, tier="local", platform=platform)
    store.close()
    conn = sqlite3.connect(path)
    # The index has to go first: SQLite refuses to drop a column an index references.
    conn.execute("DROP INDEX IF EXISTS idx_matches_lead_token")
    conn.execute("ALTER TABLE matches DROP COLUMN lead_token")
    conn.execute("UPDATE meta SET value='27' WHERE key='schema_version'")
    conn.commit()
    conn.close()
    return path


def test_the_lead_token_index_is_created_in_the_migration_block_not_in_SCHEMA():
    """A guard on WHERE the unique index is declared, because the wrong answer is a
    total outage rather than a missing index.

    `_init_schema` runs `executescript(SCHEMA)` long before the `_add_column_if_missing`
    ALTER. On an UPGRADING database `CREATE TABLE IF NOT EXISTS matches` does not widen
    the existing table, so at that point `lead_token` does not exist — and a
    `CREATE UNIQUE INDEX ... ON matches(lead_token)` inside the script raises
    `OperationalError: no such column`, which propagates straight out of `Store.__init__`.
    Bridge, worker and CLI all build a Store at startup, so every deployment that has
    ever stored a lead would fail to open, permanently, over an index.

    Nothing in the fresh-DB suites can see that: they all start from a `matches` that
    already has the column. This assertion is cheap and catches the statement being
    moved back, which is the only way the bug returns.
    """
    from aizu.core.store import SCHEMA
    assert "idx_matches_lead_token" not in SCHEMA, (
        "the lead_token unique index must be created AFTER the migration's ALTER and "
        "backfill, not inside the SCHEMA executescript")


def test_a_v27_db_gains_lead_tokens_on_open():
    """The upgrade itself: `lead_token` is the one v28 column that could NOT be left
    NULL on existing rows, because it is the org plane's only handle on a lead. So the
    migration is an ALTER plus a backfill loop, and this pins all of it."""
    path = _make_v27_matches_db()
    store = Store(path)
    try:
        assert store._conn.execute(
            "SELECT value FROM meta WHERE key='schema_version'").fetchone()[0] \
            == str(SCHEMA_VERSION)
        rows = store.matches("c1")
        assert len(rows) == len(_V28_SEED)
        tokens = [r["lead_token"] for r in rows]
        assert all(t and t.strip() for t in tokens), "a pre-v28 row was left keyless"
        assert len(set(tokens)) == len(tokens), "the backfill reused a token"
        # ...and no token echoes the id it stands in for. A derived key would leak
        # the permalink the whole change exists to withhold.
        for r in rows:
            assert r["lead_token"] != r["comment_id"]
            assert r["comment_id"] not in r["lead_token"]
            assert r["reel_id"] not in r["lead_token"]
        # The unique index is present and actually UNIQUE — a plain index here would
        # let two rows share a key, and one org's write would land on the other's lead.
        idx = store._conn.execute(
            "SELECT name, sql FROM sqlite_master "
            "WHERE type='index' AND name='idx_matches_lead_token'").fetchone()
        assert idx is not None, "the v28 unique index was not created"
        assert "UNIQUE" in idx["sql"].upper()
    finally:
        store.close()


def test_reopening_a_migrated_db_does_not_rotate_the_tokens():
    """The backfill's WHERE clause is what makes a re-open a no-op, and that is not a
    performance nicety: the panel holds a token in the /leads/<uid> URL and in its
    query cache, so re-minting on every open would turn every drawer a customer has
    open into a 404 each time the process restarts."""
    path = _make_v27_matches_db()
    store = Store(path)
    try:
        first = {r["comment_id"]: r["lead_token"] for r in store.matches("c1")}
    finally:
        store.close()
    for _ in range(2):
        store = Store(path)
        try:
            again = {r["comment_id"]: r["lead_token"] for r in store.matches("c1")}
        finally:
            store.close()
        assert again == first


def test_a_row_written_by_a_pre_v28_worker_is_healed_on_read():
    """A mixed-version fleet is the normal state during a rollout, not a corruption.

    A worker still running a pre-v28 binary INSERTs into the migrated DB without the
    column, so the row lands with `lead_token` NULL — the box is doing nothing wrong,
    it simply predates the key. `ensure_lead_token` heals that on first read by
    MINTING, never by deriving: every value that could be derived from the row is the
    comment id or contains it, which is exactly what an org-facing key must not be.

    NULLs coexist under the UNIQUE index (SQLite treats them as distinct), which is
    why the un-healed row can sit there at all rather than failing the insert.
    """
    store, path = fresh_store()
    try:
        store.upsert_campaign_meta("c1", org_id=1)
        # The pre-v28 shape: an explicit column list that omits lead_token entirely.
        with store._tx() as c:
            c.execute(
                "INSERT INTO matches (campaign_id, org_id, platform, reel_id, "
                "comment_id, username, text, lang, score, reason, tier, status, "
                "captured_at, updated_at) "
                "VALUES (?,1,'reddit',?,?,'u','t','en',0.9,'x','local','new',?,?)",
                ("c1", "t3_old", "t3_old/t1_old", time.time(), time.time()))
        assert store.matches("c1")[0]["lead_token"] is None
        # Read path heals it...
        token = store.ensure_lead_token("c1", "reddit", "t3_old/t1_old")
        assert token and token != "t3_old/t1_old"
        assert "t3_old" not in token and "t1_old" not in token
        # ...and PERSISTS it, so the key the customer was just handed still resolves.
        assert store.matches("c1")[0]["lead_token"] == token
        assert store.lead_token_for("c1", "reddit", "t3_old/t1_old") == token
        # Idempotent: a second read returns the same key rather than a second one.
        assert store.ensure_lead_token("c1", "reddit", "t3_old/t1_old") == token
        # ...and it round-trips back to the real lead, for the caller's org only.
        assert store.resolve_lead_token(1, token) == {
            "campaignId": "c1", "platform": "reddit", "commentId": "t3_old/t1_old"}
        assert store.resolve_lead_token(2, token) is None
    finally:
        store.close()


def test_a_repoll_never_rewrites_an_existing_lead_token():
    """`upsert_match`'s ON CONFLICT clause deliberately omits `lead_token`.

    Everything else about a lead refreshes on a re-poll — score, reason, intent, the
    extracted dict — because the latest verdict is the true one. The KEY must not,
    for the same reason the backfill is idempotent: it is the value the panel is
    holding right now. Asserted alongside a field that DOES refresh, so a change that
    froze the whole row would not pass by accident."""
    store, _ = fresh_store()
    try:
        kw = dict(campaign_id="c1", reel_id="t3_post", comment_id="t3_post/t1_xyz",
                  username="u", text="t", lang="en", extracted=None, tier="local",
                  platform="reddit")
        store.upsert_match(score=0.9, reason="first", intent="Wants size 42", **kw)
        first = store.matches("c1")[0]["lead_token"]
        assert first
        store.upsert_match(score=0.4, reason="second", intent="Wants size 43", **kw)
        row = store.matches("c1")[0]
        assert row["lead_token"] == first, "a re-poll rotated the panel's lead key"
        assert row["reason"] == "second" and row["intent"] == "Wants size 43"
    finally:
        store.close()
