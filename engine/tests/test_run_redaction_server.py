"""v27 bridge changes: the run log leaves the customer app, the superadmin plane gains
the full feed + a run picker, and campaign creation is plan-limited.

Two halves. The first exercises `_aggregate_run_progress` directly — it is the ONE place
the org-facing scalars are computed, and the fixtures here are the shapes that broke the
earlier drafts (five sessions under one run id, a feed_walk detail that regresses if you
take "the newest", a success event duplicated across two attempts). The second drives a
real ThreadingHTTPServer so the auth posture of the new admin routes and the wire shape
of the redacted feed are what is tested, not a hand-called handler.
"""
import json
import os
import shutil
import tempfile
import threading
import urllib.error
import urllib.request
from pathlib import Path

import pytest

from aizu import admin_auth, server
from aizu.admin_auth import ADMIN_IP_ALLOWLIST_ENV
from aizu.auth import hash_password
from aizu.core.config import load_campaign
from aizu.core.store import SessionCounters, Store
from aizu.runner import RunManager
from aizu.secrets import SECRET_KEY_ENV, SecretCipher
from aizu.server import serve

ROOT = Path(__file__).resolve().parents[1]
CONFIG = ROOT / "config"
_LANDING_HTML = '<!doctype html><html><body><div id="landing"></div></body></html>'
_APP_HTML = '<!doctype html><html><body><div id="root"></div></body></html>'
KEY = SecretCipher.generate_key()
ADMIN_PW = "longenough1"


# ===== _aggregate_run_progress (pure) ==========================================

def _event(**kw):
    """One fetch_run_events-shaped row. `detail` is written as the JSON TEXT the
    column really holds, so the decode path is exercised too."""
    row = {"id": kw.get("id", 1), "seq": kw.get("seq", 1),
           "campaignId": "c1", "sessionId": kw.get("session", "s1"),
           "phase": kw["phase"], "level": kw.get("level", "info"),
           "message": kw.get("message", "m"), "createdAt": kw.get("at", 100.0),
           "platform": "instagram"}
    detail = kw.get("detail")
    row["detail"] = json.dumps(detail) if detail is not None else None
    return row


_EMPTY_COUNTERS = {"reelsSeen": 0, "relevancePasses": 0, "matches": 0}


def _progress(events, *, counters=None, lead_rows=0, finished=False, failed=False):
    return server._aggregate_run_progress(
        events, counters=counters or _EMPTY_COUNTERS, lead_rows=lead_rows,
        finished=finished, failed=failed)


def test_retried_match_events_are_deduped_before_counting():
    """run_events is append-only per ATTEMPT while the store dedupes on comment id, so
    a retry that re-scans a post it already scored emits the same success twice."""
    events = [
        _event(phase="comments", level="success", session="s1",
               detail={"username": "aziz", "score": 0.8, "reelId": "r1"}),
        _event(phase="comments", level="success", session="s2",   # attempt 2, same lead
               detail={"username": "aziz", "score": 0.8, "reelId": "r1"}),
        _event(phase="comments", level="success", session="s2",
               detail={"username": "dana", "score": 0.9, "reelId": "r1"}),
    ]
    assert _progress(events)["leadsFound"] == 2


def test_per_post_rollup_never_double_counts_the_comment_events():
    """Both shapes land under phase=comments/level=success: one per COMMENT and one
    per POST ("2 match(es) on reel r1"). Summing them would double every lead."""
    events = [
        _event(phase="comments", level="success",
               detail={"username": "aziz", "reelId": "r1"}),
        _event(phase="comments", level="success",
               detail={"username": "dana", "reelId": "r1"}),
        _event(phase="comments", level="success", detail={"reelId": "r1", "found": 2}),
    ]
    assert _progress(events)["leadsFound"] == 2


def test_non_instagram_item_ids_are_understood():
    """The item key is platform-specific (postId/videoId/...). Keying on "reelId" alone
    would collapse a whole LinkedIn run into one bucket and under-count it to 1."""
    events = [
        _event(phase="comments", level="success",
               detail={"username": "aziz", "postId": "p1"}),
        _event(phase="comments", level="success",
               detail={"username": "aziz", "postId": "p2"}),
    ]
    assert _progress(events)["leadsFound"] == 2


def test_the_lead_estimate_accepts_raw_detail_text():
    """The picker reads `run_events.detail` straight out of SQL (a TEXT column), while
    the activity feed passes dicts it already decoded. One helper serves both, so the
    two surfaces cannot drift into two different definitions of "leads found"."""
    raw = [json.dumps({"username": "aziz", "reelId": "r1"}),
           json.dumps({"username": "aziz", "reelId": "r1"}),   # retry, same lead
           json.dumps({"username": "dana", "reelId": "r1"}),
           None, "not json at all", "[]"]                      # never raises
    assert server._leads_from_match_events(raw) == 2
    # ...and the decoded form gives the identical answer.
    assert server._leads_from_match_events(
        [{"username": "aziz", "reelId": "r1"}, {"username": "aziz", "reelId": "r1"},
         {"username": "dana", "reelId": "r1"}]) == 2


def test_feed_walk_is_summed_per_session_max_not_taken_newest():
    """One run_id spans MANY sessions and each engine reports its OWN counters. Taking
    the newest feed_walk detail both under-reports and goes BACKWARDS on a retry — the
    customer watches the number fall from 8 to 2 mid-run."""
    events = [
        _event(phase="feed_walk", session="s1", seq=1,
               detail={"reelsSeen": 6, "relevancePasses": 3}),
        _event(phase="feed_walk", session="s1", seq=2,
               detail={"reelsSeen": 8, "relevancePasses": 4}),
        _event(phase="feed_walk", session="s2", seq=1,   # newest, but a fresh session
               detail={"reelsSeen": 2, "relevancePasses": 1}),
    ]
    out = _progress(events)
    assert out["itemsScanned"] == 10       # 8 + 2, not 2
    assert out["relevantFound"] == 5


def test_feed_walk_matches_detail_is_ignored():
    """`counters.matches` is bumped once per POST after a whole comment batch while the
    success events fire per COMMENT, so it lags — observed reading 0 against 15 success
    rows in one run. Two disagreeing lead counts must never render together."""
    events = [
        _event(phase="feed_walk", detail={"reelsSeen": 4, "matches": 99}),
        _event(phase="comments", level="success", detail={"username": "aziz",
                                                          "reelId": "r1"}),
    ]
    assert _progress(events)["leadsFound"] == 1


def test_session_counters_and_real_rows_are_folded_with_max():
    """Both read zero for the whole of a fleet run and become correct at ack, so they
    are folded in with max() — never taken blindly, never allowed to pull a number down."""
    events = [_event(phase="feed_walk", detail={"reelsSeen": 4, "relevancePasses": 2})]
    out = _progress(events, counters={"reelsSeen": 40, "relevancePasses": 1,
                                      "matches": 0}, lead_rows=7)
    assert out["itemsScanned"] == 40       # ack-time counter is larger → wins
    assert out["relevantFound"] == 2       # event estimate is larger → survives
    assert out["leadsFound"] == 7          # the authoritative rows


def test_phase_is_an_allow_list_and_never_leaks_an_internal_name():
    assert _progress([_event(phase="feed_walk")])["phase"] == "searching"
    assert _progress([_event(phase="comments")])["phase"] == "qualifying"
    assert _progress([_event(phase="halt", level="error")])["phase"] == "stopped"
    # A phase this bridge has never heard of degrades, it does not pass through.
    assert _progress([_event(phase="brand_new_stage")])["phase"] == "working"


def test_phase_starting_failed_and_done():
    assert _progress([])["phase"] == "starting"          # not "nothing found"
    assert _progress([_event(phase="feed_walk")], finished=True)["phase"] == "done"
    assert _progress([_event(phase="feed_walk")], failed=True)["phase"] == "failed"


def test_malformed_detail_never_breaks_the_poll():
    events = [{"phase": "comments", "level": "success", "detail": "not json",
               "createdAt": 1.0, "seq": 1, "sessionId": "s1"},
              {"phase": "feed_walk", "detail": "[1,2]", "createdAt": 2.0,
               "seq": 2, "sessionId": "s1"}]
    out = _progress(events)
    assert out["leadsFound"] == 0 and out["itemsScanned"] == 0
    assert out["lastEventAt"] == 2.0


# ===== HTTP: the org feed, the admin plane, the campaign cap ===================

def _req(method, base, path, body=None, *, cookie=None):
    data = json.dumps(body).encode() if body is not None else None
    headers = {"Content-Type": "application/json"}
    if cookie:
        headers["Cookie"] = cookie
    req = urllib.request.Request(base + path, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req) as resp:
            return resp.status, json.loads(resp.read() or "null")
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or "null")


class _FakeProc:
    pid = 4242

    def __init__(self):
        self.returncode = None

    def wait(self):
        self.returncode = 0
        return 0

    def poll(self):
        return self.returncode

    def terminate(self):
        self.returncode = 0

    def kill(self):
        self.returncode = 0


def _spawner(argv, cwd, env, log_path):
    return _FakeProc()


def _ready_probe(cdp_url: str, **_kwargs) -> dict:
    return {"ready": True, "cdp": "ok", "instagram": "logged_in",
            "checkedAt": 0.0, "cdpUrl": cdp_url, "detail": None}


@pytest.fixture(scope="module")
def srv():
    # Set here as well as in the per-test `_env` fixture: a module-scoped fixture is
    # built BEFORE the first function-scoped one, and the admin plane fails closed
    # without an allowlist — so the one admin login below would 401. Restored at
    # teardown so a later module's "no key configured" case still sees no key.
    prior = {k: os.environ.get(k) for k in (ADMIN_IP_ALLOWLIST_ENV, SECRET_KEY_ENV)}
    os.environ[ADMIN_IP_ALLOWLIST_ENV] = "127.0.0.1,::1"
    os.environ[SECRET_KEY_ENV] = KEY
    fd, db_path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    panel_dir = tempfile.mkdtemp(suffix="-spa")
    (Path(panel_dir) / "index.html").write_text(_LANDING_HTML, encoding="utf-8")
    (Path(panel_dir) / "app").mkdir()
    (Path(panel_dir) / "app" / "index.html").write_text(_APP_HTML, encoding="utf-8")
    manager = RunManager(db_path=db_path, config_dir=str(CONFIG),
                         engine_root=panel_dir, log_dir=Path(panel_dir) / "run-logs",
                         spawner=_spawner, python_exe="py")
    httpd = serve(db_path, panel_dir, str(CONFIG), port=0, run_manager=manager,
                  readiness_probe=_ready_probe, billing_providers={})
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{httpd.server_address[1]}"
    _, signup = _req("POST", base, "/api/auth/signup",
                     {"email": "v27@aizu.test", "password": "test-password-123",
                      "companyName": "V27 Co"})
    cookie = _session_cookie(base)
    cipher = SecretCipher(KEY)
    store = Store(db_path, secret_cipher=cipher)
    try:
        org_id = int(store._conn.execute(
            "SELECT id FROM organizations ORDER BY id LIMIT 1").fetchone()[0])
        totp = admin_auth.generate_totp_secret()
        store.create_platform_admin(email="ops@v27.test",
                                    password_hash=hash_password(ADMIN_PW),
                                    mfa_secret=cipher.encrypt({"totp": totp}))
    finally:
        store.close()
    # ONE admin login for the whole module: TOTP anti-replay forbids reusing a code
    # inside its window, so logging in per test would 401 on the second call.
    admin_cookie = _admin_login(base, totp)
    yield {"base": base, "db": db_path, "cookie": cookie, "orgId": org_id,
           "adminCookie": admin_cookie}
    httpd.shutdown()
    os.unlink(db_path)
    shutil.rmtree(panel_dir, ignore_errors=True)
    for key, value in prior.items():
        if value is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = value


def _session_cookie(base: str) -> str:
    req = urllib.request.Request(
        base + "/api/auth/login",
        data=json.dumps({"email": "v27@aizu.test",
                         "password": "test-password-123"}).encode(),
        headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req) as resp:
        return resp.headers["Set-Cookie"].split(";", 1)[0]


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    # Loopback is the test peer; the admin plane fails CLOSED without an allowlist.
    monkeypatch.setenv(ADMIN_IP_ALLOWLIST_ENV, "127.0.0.1,::1")
    monkeypatch.setenv(SECRET_KEY_ENV, KEY)


def _admin_login(base: str, totp: str) -> str:
    req = urllib.request.Request(
        base + "/api/admin/login",
        data=json.dumps({"email": "ops@v27.test", "password": ADMIN_PW,
                         "totpCode": admin_auth.totp_now(totp)}).encode(),
        headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req) as resp:
        return resp.headers["Set-Cookie"].split(";", 1)[0]


def _admin_cookie(srv) -> str:
    return srv["adminCookie"]


# `upsert_subscription` drops any event whose `last_event_ts` is not strictly newer
# than the stored one (the monotonic webhook guard), so every tier switch in this
# module needs a fresh, ascending stamp. Handing tests literal numbers made that an
# ordering trap: inserting a case with a stamp above a LATER test's silently dropped
# that test's switch and ran it on the wrong plan. The counter removes the coupling.
_TIER_TS = [1000.0]


def _set_tier(srv, tier: str) -> None:
    _TIER_TS[0] += 10.0
    store = Store(srv["db"])
    try:
        store.upsert_subscription(srv["orgId"], last_event_ts=_TIER_TS[0], tier=tier,
                                  status="active")
    finally:
        store.close()


def _seed_run(srv, run_id: str) -> str:
    """A finished, org-owned run: one session with counters, a flag, and the three
    narrative events the customer must no longer see."""
    cid = load_campaign(CONFIG / "campaign.md").campaign_id
    store = Store(srv["db"])
    try:
        store.upsert_campaign_meta(cid, org_id=srv["orgId"], status="live")
        store.start_session(f"sess-{run_id}", cid, "instagram", run_id=run_id)
        for i, (phase, level, msg, detail) in enumerate([
                ("lifecycle", "info", "Run started — campaign x", None),
                ("relevance", "success", "Relevant ✓ — @acme.io",
                 {"reelId": "r1", "author": "acme.io"}),
                ("comments", "success", "Match: @aziz (score 0.82)",
                 {"username": "aziz", "score": 0.82, "reelId": "r1"})], start=1):
            store.emit_run_event(run_id, i, phase, level, msg, campaign_id=cid,
                                 session_id=f"sess-{run_id}",
                                 detail=json.dumps(detail) if detail else None)
        store.update_counters(f"sess-{run_id}", SessionCounters(
            reels_seen=5, relevance_passes=2, comments_scored=10, matches=1))
        store.raise_flag("feed_health", "soft", "skip ratio high", campaign_id=cid,
                         session_id=f"sess-{run_id}", org_id=srv["orgId"])
        store.end_session(f"sess-{run_id}", "completed")
    finally:
        store.close()
    return cid


def test_org_run_activity_carries_no_narrative_events(srv):
    _seed_run(srv, "run-redact")
    code, resp = _req("GET", srv["base"], "/api/run/activity?runId=run-redact",
                      cookie=srv["cookie"])
    assert code == 200
    data = resp["data"]
    # The whole point: no message, no detail, no session/campaign id reaches the org.
    assert data["events"] == []
    assert data["eventsRedacted"] is True
    body = json.dumps(data)
    assert "aziz" not in body and "Match:" not in body and "acme.io" not in body
    # ...and the run is still legible while it runs.
    assert data["finished"] is True
    assert data["phase"] == "done"
    assert data["leadsFound"] == 1
    assert data["itemsScanned"] == 5 and data["relevantFound"] == 2
    assert data["lastEventAt"] is not None
    assert data["counters"]["matches"] == 1
    # Flags stay — they drive the "fix your agent" UX and are a state, not a log.
    assert [f["kind"] for f in data["flags"]] == ["feed_health"]


def test_org_run_activity_cursor_is_inert(srv):
    """`after` stays accepted (and echoed) so the panel's poll plumbing is unchanged,
    but it can no longer page anything into view."""
    _seed_run(srv, "run-cursor")
    code, resp = _req("GET", srv["base"], "/api/run/activity?runId=run-cursor&after=0",
                      cookie=srv["cookie"])
    assert code == 200 and resp["data"]["events"] == []
    assert resp["data"]["cursor"] == 0


def test_admin_run_activity_needs_the_admin_plane(srv):
    _seed_run(srv, "run-admin")
    # An ORG session — even the owner's — is not a platform admin.
    code, _ = _req("GET", srv["base"], "/api/admin/run/activity?runId=run-admin",
                   cookie=srv["cookie"])
    assert code == 401
    code, _ = _req("GET", srv["base"], "/api/admin/run/activity?runId=run-admin")
    assert code == 401


def test_admin_run_activity_returns_the_full_feed(srv):
    _seed_run(srv, "run-full")
    code, resp = _req("GET", srv["base"], "/api/admin/run/activity?runId=run-full",
                      cookie=_admin_cookie(srv))
    assert code == 200
    messages = [e["message"] for e in resp["data"]["events"]]
    assert any(m.startswith("Run started") for m in messages)
    assert any("aziz" in m for m in messages)          # identities, on purpose
    assert resp["data"]["counters"]["matches"] == 1
    assert resp["data"]["finished"] is True


def test_admin_run_activity_requires_a_run_id(srv):
    code, resp = _req("GET", srv["base"], "/api/admin/run/activity",
                      cookie=_admin_cookie(srv))
    assert code == 400 and "runId" in resp["error"]


def test_admin_org_runs_needs_the_admin_plane(srv):
    code, _ = _req("GET", srv["base"], f"/api/admin/orgs/{srv['orgId']}/runs",
                   cookie=srv["cookie"])
    assert code == 401


def test_admin_org_runs_lists_the_orgs_runs(srv):
    _seed_run(srv, "run-picker")
    code, resp = _req("GET", srv["base"], f"/api/admin/orgs/{srv['orgId']}/runs",
                      cookie=_admin_cookie(srv))
    assert code == 200
    run = next(r for r in resp["data"]["runs"] if r["runId"] == "run-picker")
    assert run["status"] == "done" and run["leads"] == 1
    assert run["platforms"] == ["instagram"] and run["sessions"] == 1
    assert run["startedAt"] is not None and run["finishedAt"] is not None


def _seed_dead_lettered_run(srv, run_id: str, *, leads: int) -> str:
    """A fleet run that harvested `leads` and then DIED without acking.

    The shape is measured, not invented: sessions and matches both travel in the ACK
    body, so a job that dead-lettered mirrored NEITHER into the cloud — only its
    narrative events, which land on the ~45s heartbeat. The cloud therefore holds
    run_events and a failed job, and nothing else at all.
    """
    cid = load_campaign(CONFIG / "campaign.md").campaign_id
    store = Store(srv["db"])
    try:
        store.upsert_campaign_meta(cid, org_id=srv["orgId"], status="live")
        store.enqueue_job(job_id=f"job-{run_id}", campaign_id=cid, platform="instagram",
                          spec={"run_id": run_id, "target_leads": 10},
                          org_id=srv["orgId"])
        with store._tx() as c:                    # dead-letter it the way the reclaim does
            c.execute("UPDATE jobs SET status='failed', attempts=5 WHERE id=?",
                      (f"job-{run_id}",))
        seq = 0
        for attempt in range(2):                  # two attempts, same leads re-scanned
            for i in range(leads):
                seq += 1
                store.emit_run_event(
                    run_id, seq, "comments", "success", f"Match: @user{i}",
                    campaign_id=cid, session_id=f"sess-{run_id}-{attempt}",
                    detail=json.dumps({"username": f"user{i}", "score": 0.9,
                                       "reelId": f"r{i}"}))
    finally:
        store.close()
    return cid


def test_admin_org_runs_reports_a_dead_lettered_runs_real_harvest(srv):
    """The picker used to sum `sessions.matches` — and a run that never acked has no
    session rows, so the one run an operator most needs to open was listed as
    "0 leads" beside a log that said fifteen. Both numbers now come from the same
    `_leads_from_match_events` definition the log uses."""
    # Arrange
    _seed_dead_lettered_run(srv, "run-dead", leads=15)
    # Act
    code, resp = _req("GET", srv["base"], f"/api/admin/orgs/{srv['orgId']}/runs",
                      cookie=_admin_cookie(srv))
    assert code == 200
    run = next(r for r in resp["data"]["runs"] if r["runId"] == "run-dead")
    # Assert — the deduped estimate survives the retry that re-scored every lead...
    assert run["leadsFound"] == 15 and run["leads"] == 15
    # ...and the payload still says plainly that none of them reached the account.
    assert run["leadsDelivered"] == 0
    assert run["delivery"] == "not_delivered"
    assert run["status"] == "failed"


def test_the_picker_and_the_run_progress_quote_the_same_numbers(srv):
    """The whole point of sharing one definition: two surfaces, one answer.

    The run-progress payload (`_aggregate_run_progress`) and the picker row now fold
    the same events through the same helper, so a run cannot read 15 in the drawer
    and 0 in the list — which is exactly what it did.
    """
    # Arrange
    _seed_dead_lettered_run(srv, "run-agree", leads=7)
    # Act
    _, listed = _req("GET", srv["base"], f"/api/admin/orgs/{srv['orgId']}/runs",
                     cookie=_admin_cookie(srv))
    _, progress = _req("GET", srv["base"], "/api/run/activity?runId=run-agree",
                       cookie=srv["cookie"])
    # Assert
    row = next(r for r in listed["data"]["runs"] if r["runId"] == "run-agree")
    assert row["leadsFound"] == progress["data"]["leadsFound"] == 7
    assert row["leadsDelivered"] == progress["data"]["leadsDelivered"] == 0
    assert row["delivery"] == progress["data"]["delivery"] == "not_delivered"


def test_a_healthy_runs_leads_are_reported_as_delivered(srv):
    """The other direction: a run that acked has real `matches` rows, so found and
    delivered converge and the row carries no not-delivered state to explain.

    Seeded under its OWN org: a `matches` row is billable lead usage, and the plan
    clamp cases further down this module read that same counter.
    """
    # Arrange
    code, signup = _req("POST", srv["base"], "/api/auth/signup",
                        {"email": "healthy@aizu.test", "password": "test-password-123",
                         "companyName": "Healthy Co"})
    assert code == 200, signup
    org_id = signup["data"]["user"]["orgId"]
    store = Store(srv["db"])
    try:
        store.upsert_campaign_meta("camp-healthy", org_id=org_id, status="live",
                                   display_name="Healthy")
        store.start_session("sess-healthy", "camp-healthy", "instagram",
                            run_id="run-healthy")
        store.emit_run_event("run-healthy", 1, "comments", "success", "Match: @aziz",
                             campaign_id="camp-healthy", session_id="sess-healthy",
                             detail=json.dumps({"username": "aziz", "reelId": "r1"}))
        store.update_counters("sess-healthy", SessionCounters(
            reels_seen=3, relevance_passes=1, comments_scored=4, matches=1))
        store.upsert_match(campaign_id="camp-healthy", reel_id="r1",
                           comment_id="cm-healthy", username="aziz", text="how much?",
                           lang="en", score=0.9, reason="asked price", extracted=None,
                           tier="local", platform="instagram",
                           session_id="sess-healthy", intent="Wants a price")
        store.end_session("sess-healthy", "completed")
    finally:
        store.close()
    # Act
    code, resp = _req("GET", srv["base"], f"/api/admin/orgs/{org_id}/runs",
                      cookie=_admin_cookie(srv))
    # Assert
    assert code == 200
    run = next(r for r in resp["data"]["runs"] if r["runId"] == "run-healthy")
    assert run["leadsFound"] == run["leadsDelivered"] == run["leads"] == 1
    assert run["delivery"] == "delivered"
    assert run["status"] == "done"


def test_admin_org_runs_404s_an_unknown_org(srv):
    code, _ = _req("GET", srv["base"], "/api/admin/orgs/99999/runs",
                   cookie=_admin_cookie(srv))
    assert code == 404


def test_admin_org_route_rejects_an_unknown_subresource(srv):
    """The allow-list is the whole gate on the subresource half — an unknown segment
    must never reach the handler."""
    assert server._match_admin_org_route("/api/admin/orgs/1/runs") == (1, "runs")
    assert server._match_admin_org_route("/api/admin/orgs/1/secrets") is None


# ----- campaign cap -----------------------------------------------------------

def _create(srv, campaign_id: str, *, op="create"):
    return _req("POST", srv["base"], "/api/campaign",
                {"campaignId": campaign_id, "op": op, "displayName": campaign_id,
                 "status": "draft",
                 "brief": {"platform": "youtube", "seedChannels": ["UC_a"]}},
                cookie=srv["cookie"])


def test_campaign_cap_blocks_the_second_create_on_free(srv):
    _set_tier(srv, "free")
    _wipe_campaigns(srv)
    code, _ = _create(srv, "cap-one")
    assert code == 200
    code, resp = _create(srv, "cap-two")
    assert code == 402
    assert resp["error"] == ("Plan limit reached (1 campaigns on Free). "
                             "Upgrade to add more campaigns.")


def test_campaign_cap_never_blocks_an_edit(srv):
    _set_tier(srv, "free")
    _wipe_campaigns(srv)
    code, created = _create(srv, "cap-edit")
    assert code == 200
    code, _ = _req("POST", srv["base"], "/api/campaign",
                   {"campaignId": created["data"]["campaign_id"], "op": "edit",
                    "displayName": "Renamed"}, cookie=srv["cookie"])
    assert code == 200


def test_archived_campaigns_do_not_count_towards_the_cap(srv):
    """The cap bounds the WORKING set, so an org at its limit can archive its way
    forward rather than being wedged with no move but an upgrade."""
    _set_tier(srv, "free")
    _wipe_campaigns(srv)
    code, created = _create(srv, "cap-archive")
    assert code == 200
    assert _create(srv, "cap-blocked")[0] == 402
    code, _ = _req("POST", srv["base"], "/api/campaign/archive",
                   {"campaignId": created["data"]["campaign_id"], "archived": True},
                   cookie=srv["cookie"])
    assert code == 200
    assert _create(srv, "cap-allowed")[0] == 200


def test_campaign_cap_on_lite_allows_three_and_refuses_the_fourth(srv):
    """The cap is read PER TIER, not hardcoded to the Free 1. Every create under the
    limit must go through untouched — a gate that only ever gets tested at its
    boundary can be off by one in the permissive direction and nobody notices."""
    _set_tier(srv, "lite")
    _wipe_campaigns(srv)
    for n in range(1, 4):
        assert _create(srv, f"lite-{n}")[0] == 200, f"campaign {n} of 3 must be allowed"
    code, resp = _create(srv, "lite-4")
    assert code == 402
    assert resp["error"] == ("Plan limit reached (3 campaigns on Lite). "
                             "Upgrade to add more campaigns.")


def test_unlimited_tier_is_not_read_as_zero(srv):
    """`campaign_cap` is None on the paid tiers — a falsy check would block every
    create on exactly the plans that are supposed to be unlimited."""
    _set_tier(srv, "pro")
    _wipe_campaigns(srv)
    assert _create(srv, "pro-one")[0] == 200
    assert _create(srv, "pro-two")[0] == 200
    assert _create(srv, "pro-three")[0] == 200


def _wipe_campaigns(srv) -> None:
    """Reset the org's campaign set between cap tests (the module fixture shares one
    DB, and the cap is a count over exactly this table)."""
    store = Store(srv["db"])
    try:
        with store._tx() as c:
            c.execute("DELETE FROM campaign_meta WHERE org_id=?", (srv["orgId"],))
    finally:
        store.close()


# ----- run-start plan bounds --------------------------------------------------

def test_run_start_reports_the_plan_bounds(srv):
    """The clamp is enforcement; these three numbers are the SURFACING of it, so the
    run UI can bound its own input instead of discovering the limit as a 402."""
    _set_tier(srv, "free")
    _wipe_campaigns(srv)
    cid = load_campaign(CONFIG / "campaign.md").campaign_id
    store = Store(srv["db"])
    try:
        store.upsert_campaign_meta(cid, org_id=srv["orgId"], status="live")
    finally:
        store.close()
    code, resp = _req("POST", srv["base"], "/api/run",
                      {"scope": "campaign", "campaignId": cid, "mode": "dry",
                       "targetLeadCount": 500}, cookie=srv["cookie"])
    assert code == 202
    data = resp["data"]
    assert data["maxRunLeads"] == 10          # the Free period allowance
    assert data["leadsRemaining"] == 10
    assert data["targetLeads"] == 10          # 500 clamped down to what is left
    _req("POST", srv["base"], "/api/run/stop", {}, cookie=srv["cookie"])


def _live_campaign(srv) -> str:
    """The file campaign, registered live to this org — the run gate needs an owned,
    non-archived campaign before it ever reaches the plan clamp."""
    cid = load_campaign(CONFIG / "campaign.md").campaign_id
    store = Store(srv["db"])
    try:
        store.upsert_campaign_meta(cid, org_id=srv["orgId"], status="live")
    finally:
        store.close()
    return cid


def _start_run(srv, cid: str, target: int):
    code, resp = _req("POST", srv["base"], "/api/run",
                      {"scope": "campaign", "campaignId": cid, "mode": "dry",
                       "targetLeadCount": target}, cookie=srv["cookie"])
    _req("POST", srv["base"], "/api/run/stop", {}, cookie=srv["cookie"])
    return code, resp


def test_run_target_clamp_follows_the_plan_not_a_constant(srv):
    """The same oversized request clamps to a DIFFERENT number on a different tier.
    Asserting only the Free case cannot tell a plan lookup from a hardcoded 10."""
    _wipe_campaigns(srv)
    cid = _live_campaign(srv)
    _set_tier(srv, "lite")
    code, resp = _start_run(srv, cid, 500)
    assert code == 202
    assert resp["data"]["maxRunLeads"] == 50      # the Lite period allowance
    assert resp["data"]["targetLeads"] == 50


def test_a_target_inside_the_plan_is_left_alone(srv):
    """The clamp is a ceiling, not a rewrite: a modest target must survive verbatim,
    or every Free run would silently become a 10-lead run."""
    _wipe_campaigns(srv)
    cid = _live_campaign(srv)
    _set_tier(srv, "free")
    code, resp = _start_run(srv, cid, 3)
    assert code == 202
    assert resp["data"]["targetLeads"] == 3
    assert resp["data"]["maxRunLeads"] == 10


def test_leads_already_delivered_this_period_shrink_the_run_target(srv):
    """`leadsRemaining` is the period allowance MINUS what the org already received,
    so the last run of a nearly-spent period is bounded by the remainder — not by the
    full plan allowance, which would let one run overshoot the period cap."""
    _wipe_campaigns(srv)
    cid = _live_campaign(srv)
    _set_tier(srv, "free")
    store = Store(srv["db"])
    try:
        for n in range(7):
            store.upsert_match(campaign_id=cid, reel_id="r", comment_id=f"spent-{n}",
                               username=f"u{n}", text="t", lang="en", score=0.9,
                               reason="x", extracted=None, tier="local",
                               platform="instagram", intent=f"Wants item {n}")
    finally:
        store.close()
    code, resp = _start_run(srv, cid, 500)
    assert code == 202
    data = resp["data"]
    assert data["maxRunLeads"] == 10           # the plan allowance is unchanged...
    assert data["leadsRemaining"] == 3         # ...but only 3 of it is left
    assert data["targetLeads"] == 3            # and that is what bounds the run


# ===== E.5: a dead-lettered run keeps NO leads — the estimate is the only record ====
#
# A fleet run reconciles with the cloud only at ACK. Leads travel in the ack body, so a
# run that dead-letters (attempts exhausted, `status=failed`) leaves its harvest in the
# WORKER's local sqlite and the cloud sees zero rows — forever. Spend has the opposite
# asymmetry (the nack body ships it), which is how a card ends up reading "$X spent,
# 0 leads" with nothing on it to explain the pairing.


def test_five_sessions_under_one_run_id_sum_rather_than_pick_the_newest():
    """The MEASURED prod shape: 35 events across 5 session ids under one run id, every
    feed_walk from a different session, and one session that produced real matches while
    emitting no feed_walk at all. "Newest wins" would report 3 scanned out of 35."""
    events = []
    seq = 0
    for i, (seen, relevant) in enumerate(
            [(9, 4), (7, 3), (11, 5), (5, 2), (3, 1)], start=1):
        for step in (1, 2):                     # two progress beats per session
            seq += 1
            events.append(_event(phase="feed_walk", session=f"s{i}", seq=step,
                                 id=seq, at=100.0 + seq,
                                 detail={"reelsSeen": seen if step == 2 else seen - 1,
                                         "relevancePasses": relevant}))
    # A sixth session emitted NO feed_walk but did qualify two leads.
    for uid in ("aziz", "dana"):
        seq += 1
        events.append(_event(phase="comments", level="success", session="s6",
                             id=seq, at=100.0 + seq,
                             detail={"username": uid, "reelId": "r9"}))
    out = _progress(events)
    assert out["itemsScanned"] == 35            # 9+7+11+5+3, not the newest 3
    assert out["relevantFound"] == 15
    assert out["leadsFound"] == 2               # the silent session still counted


def test_leads_delivered_is_the_org_rows_and_converges_on_a_healthy_run():
    """The two numbers are the same thing seen before and after ack, so a run that
    delivered everything it found reports them equal and reads `delivered`."""
    events = [
        _event(phase="comments", level="success",
               detail={"username": "aziz", "reelId": "r1"}),
        _event(phase="comments", level="success",
               detail={"username": "dana", "reelId": "r1"}),
    ]
    out = _progress(events, lead_rows=2, finished=True)
    assert out["leadsFound"] == 2 and out["leadsDelivered"] == 2
    assert out["delivery"] == "delivered"


def test_a_dead_lettered_run_keeps_its_estimate_and_says_it_was_not_delivered():
    """The "authoritative rows win at ack" reconciliation NEVER fires for a run that
    never acks, so `max(estimate, rows)` collapses to the estimate — permanently. That
    estimate must not be discarded, reset, or recomputed to zero when the job flips to
    `failed`: for that run it is the only record the customer will ever have."""
    events = [_event(phase="comments", level="success", session=f"s{i}",
                     detail={"username": f"u{i}", "reelId": f"r{i}"})
              for i in range(15)]
    out = _progress(events, lead_rows=0, finished=True, failed=True)
    assert out["leadsFound"] == 15              # not zeroed by the failure
    assert out["leadsDelivered"] == 0           # nothing reached the account
    assert out["delivery"] == "not_delivered"
    assert out["phase"] == "failed"


def test_a_run_still_in_flight_is_pending_not_a_failure():
    """EVERY fleet run reads found > delivered mid-flight (the rows land at ack), so an
    unfinished gap must not render as the dead-letter state."""
    events = [_event(phase="comments", level="success",
                     detail={"username": "aziz", "reelId": "r1"})]
    out = _progress(events, lead_rows=0, finished=False)
    assert out["leadsFound"] == 1 and out["leadsDelivered"] == 0
    assert out["delivery"] == "pending"


def test_leads_found_is_never_reported_below_the_delivered_rows():
    """The event estimate under-counts by design (one person, two qualifying comments on
    one post dedupes to one) and the rows are authoritative, so the pair can only ever
    close upward — never into a negative gap."""
    out = _progress([], lead_rows=9, finished=True)
    assert out["leadsFound"] == 9 and out["leadsDelivered"] == 9
    assert out["delivery"] == "delivered"


def test_org_run_activity_ships_both_lead_numbers(srv):
    """Wire shape: `_seed_run` mirrors a session that scored a match but writes no
    `matches` row — exactly a run whose leads never reached the account."""
    _seed_run(srv, "run-undelivered")
    code, resp = _req("GET", srv["base"], "/api/run/activity?runId=run-undelivered",
                      cookie=srv["cookie"])
    assert code == 200
    data = resp["data"]
    assert data["finished"] is True
    assert data["leadsFound"] == 1              # what the run discovered
    assert data["leadsDelivered"] == 0          # what reached the account
    assert data["delivery"] == "not_delivered"
    # Still no identity anywhere near the honest state.
    assert "aziz" not in json.dumps(data)


# ===== E.7: spend and leads have OPPOSITE failure asymmetries — present the pair ====

from datetime import datetime as _datetime           # noqa: E402  (section-local)

from aizu.core.config import load_soul as _load_soul  # noqa: E402
from aizu.panel import _draft_campaign, build_raw     # noqa: E402


def _card_store():
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    return Store(path)


def _draft(store, cid, *, leads, won, spend, leads_found=None):
    store.upsert_campaign_meta(cid, status="live")
    m = next(x for x in store.list_campaign_meta() if x["campaign_id"] == cid)
    rollup = {cid: {"leads": leads, "won": won, "spend": spend}}
    return _draft_campaign(store, m, rollup, _datetime.now(), 20.0,
                           leads_found=leads_found)


def test_campaign_card_pairs_the_spend_with_an_explicit_not_delivered_state():
    """A dead-lettered run banks its spend and strands its leads. The card must not read
    "$4.10 spent, 0 leads" as though the campaign simply found nothing."""
    store = _card_store()
    try:
        card = _draft(store, "dead", leads=0, won=0, spend=4.10,
                      leads_found={"dead": 15})
    finally:
        store.close()
    assert card["leadsFound"] == 15
    assert card["leadsDelivered"] == 0
    assert card["delivery"] == "not_delivered"
    assert card["spent"] == 4.1                 # never hidden, never zeroed
    assert card["leads"] == 0                   # the delivered count is unchanged


def test_no_cpl_is_ever_synthesised_from_the_found_estimate():
    """A cost per lead over leads the customer cannot open is a fiction, so CPL stays
    guarded on `won` even when 15 were found."""
    store = _card_store()
    try:
        card = _draft(store, "dead2", leads=0, won=0, spend=4.10,
                      leads_found={"dead2": 15})
    finally:
        store.close()
    assert card["cpl"] is None


def test_a_dash_in_cpl_is_not_the_discriminator():
    """CPL is guarded on WIN_STATUS, so it reads `—` on EVERY untriaged campaign. This
    one delivered all 12 of its leads and has spent real money; it is healthy, and only
    `leadsFound > leadsDelivered` can tell the two cards apart."""
    store = _card_store()
    try:
        card = _draft(store, "healthy", leads=12, won=0, spend=4.10,
                      leads_found={"healthy": 12})
    finally:
        store.close()
    assert card["cpl"] is None                  # identical display to the failed card...
    assert card["delivery"] == "delivered"      # ...and a completely different state


def test_no_evidence_of_an_undelivered_run_renders_the_healthy_shape():
    """`leads_found` absent means "nothing on record says a run went undelivered" — the
    estimate is floored at the delivered rows, so a missing entry can never fabricate a
    gap out of an org that simply has no run_events left after the retention sweep."""
    store = _card_store()
    try:
        card = _draft(store, "quiet", leads=3, won=1, spend=1.0)
    finally:
        store.close()
    assert card["leadsFound"] == 3 and card["leadsDelivered"] == 3
    assert card["delivery"] == "delivered"


def test_reports_and_top_campaigns_rows_carry_the_pair():
    """The two other places spend and leads land on one row: the reports table (leads +
    cpl + spend) and the dashboard's top-campaigns mini list."""
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    store = Store(path)
    campaign = load_campaign(CONFIG / "campaign.md")
    try:
        store.upsert_campaign_meta(campaign.campaign_id, status="live")
        raw = build_raw(store, _load_soul(CONFIG / "soul.md"), campaign,
                        leads_found={campaign.campaign_id: 15})
    finally:
        store.close()
    card = next(c for c in raw["CAMPAIGNS"] if c["id"] == campaign.campaign_id)
    assert card["delivery"] == "not_delivered" and card["leadsFound"] == 15
    row = next(r for r in raw["REPORTS"]["week"]["perCampaign"]
               if r["id"] == campaign.campaign_id)
    assert row["leadsFound"] == 15 and row["leadsDelivered"] == 0
    assert row["delivery"] == "not_delivered"
    mini = next(t for t in raw["DASHBOARD"]["week"]["topCampaigns"]
                if t["id"] == campaign.campaign_id)
    assert mini["delivery"] == "not_delivered"
