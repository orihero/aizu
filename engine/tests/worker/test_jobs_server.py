"""HTTP-level job plane (v14 Phase 3): the bearer-gated lease/heartbeat/ack/nack worker
routes and the interim-allowlist-gated operator enqueue. Exercised over the wire on a
real ThreadingHTTPServer so the dispatch order, the bearer gate, the URL-vs-body
identity rules, and the enqueue capability check are the things actually under test
(mirrors tests/worker/test_worker_server.py)."""
import json
import os
import tempfile
import threading
import urllib.error
import urllib.request
from pathlib import Path

import pytest

from aizu.server import (WORKER_BOOTSTRAP_ENV, serve)
from aizu.admin_auth import ADMIN_IP_ALLOWLIST_ENV
from aizu.core.store import Store
from aizu.secrets import SECRET_KEY_ENV
from ._admin import ADMIN_KEY, admin_cookie, set_admin_env

ROOT = Path(__file__).resolve().parents[2]
CONFIG = ROOT / "config"
_INDEX_HTML = '<!doctype html><html><body><div id="root"></div></body></html>'
PW = "longenough1"
BOOTSTRAP = "boot-secret-xyz"
ADMIN_EMAIL = "admin@x.io"
CAP = [[1, "instagram", "acme_handle"]]


def _req(method, base, path, body=None, *, cookie=None, bearer=None):
    data = json.dumps(body).encode() if body is not None else None
    headers = {"Content-Type": "application/json"}
    if cookie:
        headers["Cookie"] = cookie
    if bearer:
        headers["Authorization"] = f"Bearer {bearer}"
    req = urllib.request.Request(base + path, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req) as resp:
            return resp.status, json.loads(resp.read() or "null"), resp.headers.get("Set-Cookie")
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or "null"), e.headers.get("Set-Cookie")


def _post(base, path, body, *, cookie=None, bearer=None):
    return _req("POST", base, path, body, cookie=cookie, bearer=bearer)


def _cookie(set_cookie):
    return set_cookie.split(";", 1)[0]


def _signup(base, email, company="Co"):
    code, resp, set_cookie = _post(base, "/api/auth/signup",
                                   {"email": email, "password": PW, "companyName": company})
    assert code == 200, resp
    return _cookie(set_cookie)


@pytest.fixture(scope="module")
def srv():
    fd, db_path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    panel_dir = tempfile.mkdtemp(suffix="-spa")
    (Path(panel_dir) / "index.html").write_text(_INDEX_HTML, encoding="utf-8")
    httpd = serve(db_path, panel_dir, str(CONFIG), port=0, billing_providers={})
    port = httpd.server_address[1]
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{port}"
    normal_cookie = _signup(base, "normal@x.io", company="Beta")
    # Mint the admin-session cookie once (needs the admin plane env set at login time).
    os.environ[ADMIN_IP_ALLOWLIST_ENV] = "127.0.0.1,::1"
    os.environ[SECRET_KEY_ENV] = ADMIN_KEY
    admin = admin_cookie(base, db_path, ADMIN_EMAIL)
    yield {"base": base, "db": db_path,
           "admin_cookie": admin, "normal_cookie": normal_cookie}
    httpd.shutdown()
    os.environ.pop(ADMIN_IP_ALLOWLIST_ENV, None)
    os.environ.pop(SECRET_KEY_ENV, None)


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    """Bootstrap registration + configure the real admin plane (IP-allowlist + key)."""
    monkeypatch.setenv(WORKER_BOOTSTRAP_ENV, BOOTSTRAP)
    set_admin_env(monkeypatch)


def _register(base, machine_id="m-1", caps=CAP, org_id=1):
    code, resp, _ = _post(base, "/api/worker/register",
                          {"machineId": machine_id, "orgId": org_id,
                           "capabilities": caps, "agentVersion": "0.1.0"},
                          bearer=BOOTSTRAP)
    assert code == 200, resp
    return resp["data"]["token"]


def _enqueue(srv, *, platform="instagram", account="acme_handle", org_id=1,
             cookie=None, job_id=None):
    body = {"campaignId": "c-acme", "platform": platform,
            "requiredAccountHandle": account, "orgId": org_id, "targetLeads": 1,
            "soulText": "be helpful"}
    if job_id:
        body["jobId"] = job_id
    return _post(srv["base"], "/api/admin/jobs/enqueue", body,
                 cookie=cookie or srv["admin_cookie"])


def _isolated(srv, slug):
    """Register a worker + enqueue a job on a UNIQUE account handle so this test's
    queue can't be leased by another test against the shared module DB. Returns
    (token, account)."""
    account = f"acct-{slug}"
    token = _register(srv["base"], machine_id=f"m-{slug}",
                      caps=[[1, "instagram", account]])
    return token, account


# ----- enqueue (operator) --------------------------------------------------------

def test_enqueue_requires_platform_admin(srv):
    # An org user session is not an admin session — the real gate rejects it (401).
    code, resp, _ = _enqueue(srv, cookie=srv["normal_cookie"])
    assert code == 401, resp


def test_enqueue_rejects_a_job_no_worker_can_serve(srv):
    # No worker declares 'youtube' → enqueue is rejected rather than idling forever.
    code, resp, _ = _enqueue(srv, platform="youtube")
    assert code == 400, resp
    assert "no registered worker" in resp["error"]


def test_enqueue_succeeds_when_a_capable_worker_exists(srv):
    _register(srv["base"], machine_id="m-enq")
    code, resp, _ = _enqueue(srv, job_id="job-enq-1")
    assert code == 200, resp
    assert resp["data"]["job"]["status"] == "queued"
    assert resp["data"]["job"]["id"] == "job-enq-1"


# ----- lease ---------------------------------------------------------------------

def test_lease_requires_a_worker_bearer_token(srv):
    code, resp, _ = _post(srv["base"], "/api/worker/lease", {})
    assert code == 401, resp


def test_lease_returns_an_enqueued_job_then_empties(srv):
    token, account = _isolated(srv, "lease")
    _enqueue(srv, job_id="job-lease-1", account=account)
    code, resp, _ = _post(srv["base"], "/api/worker/lease", {}, bearer=token)
    assert code == 200, resp
    assert resp["data"]["job"]["id"] == "job-lease-1"
    assert resp["data"]["job"]["soulText"] == "be helpful"
    # Queue now drained for this capability → empty lease is data:null (never 204).
    code, resp, _ = _post(srv["base"], "/api/worker/lease", {}, bearer=token)
    assert code == 200 and resp["data"] is None


def test_lease_surfaces_campaign_brief_but_never_platform_credentials(srv):
    """B4 regression (campaign_brief half): the ACTUAL cross-machine path a remote
    worker uses is POST /api/worker/lease -> store._job_row_to_lease, which used to
    whitelist only id/orgId/campaignId/platform/requiredAccountHandle/targetLeads/
    durationMinutes/engineMode/soulText/runId -- silently dropping campaignBrief from
    the wire response even though it round-trips fine through the raw spec column
    (store.get_job). A remote worker's JobSpec.from_payload would then see no
    campaignBrief, campaign_brief would end up None, and the worker would fall back to
    its own (empty) local DB — exactly the bug B4 was meant to fix. campaignBrief MUST
    still ride the lease.

    SECURITY REVIEW CRITICAL/HIGH (the other half, now the opposite requirement): even
    when a job's raw `spec` DB row carries a `platform_credentials` key — simulating a
    stale/legacy/tampered row, since a fresh enqueue never writes one anymore — the
    lease response must NEVER surface it under either key spelling. A worker gets its
    credential exclusively from the lease-holder-gated
    POST /api/worker/jobs/{id}/credential, never for free on the lease."""
    token, account = _isolated(srv, "brief")
    store = Store(srv["db"])
    try:
        store.enqueue_job(
            job_id="job-brief-1", campaign_id="c-acme", platform="instagram",
            org_id=1, required_account_handle=account,
            spec={"target_leads": 1, "engine_mode": "harvest",
                  "campaign_brief": {"platform": "instagram", "goal": "lead"},
                  "platform_credentials": {"api_key": "ORG-KEY"}})
    finally:
        store.close()
    code, resp, _ = _post(srv["base"], "/api/worker/lease", {}, bearer=token)
    assert code == 200, resp
    assert resp["data"]["job"]["campaignBrief"] == {"platform": "instagram", "goal": "lead"}
    assert "platformCredentials" not in resp["data"]["job"]
    assert "platform_credentials" not in resp["data"]["job"]


def _seed_spend_job(srv, slug, *, campaign_id, spend_rows=()):
    """A worker + a queued job on their own account handle and their OWN campaign, with
    optional pre-existing cloud spend. Returns (token, job_id)."""
    token, account = _isolated(srv, slug)
    job_id = f"job-{slug}"
    store = Store(srv["db"])
    try:
        store.upsert_campaign_meta(campaign_id, org_id=1)
        for stage, usd in spend_rows:
            store.log_spend(campaign_id, stage, usd, model="m1")
        store.enqueue_job(job_id=job_id, campaign_id=campaign_id, platform="instagram",
                          org_id=1, required_account_handle=account,
                          spec={"target_leads": 1, "engine_mode": "harvest"})
    finally:
        store.close()
    return token, job_id


def test_lease_surfaces_the_campaigns_prior_cloud_spend(srv):
    """B9 regression, over the SERVED endpoint (the B4 trap): `priorSpendUsd` is what
    lets a box subtract spend the campaign already burned on OTHER machines from its own
    AIZU_SPEND_CAP, instead of restarting the ceiling at $0 per box. Store-layer coverage
    alone would not catch it being dropped from the wire — `_handle_worker_lease`
    re-wraps `store.lease_one_job`'s dict into the response body, and B4 shipped
    completely inert precisely because only the store layer was tested."""
    token, _ = _seed_spend_job(srv, "priorspend", campaign_id="c-prior-spend",
                               spend_rows=[("match", 3.0), ("vision", 1.5)])

    code, resp, _ = _post(srv["base"], "/api/worker/lease", {}, bearer=token)

    assert code == 200, resp
    assert resp["data"]["job"]["priorSpendUsd"] == pytest.approx(4.5)


def test_lease_prior_spend_is_zero_for_an_unspent_campaign(srv):
    token, _ = _seed_spend_job(srv, "nospend", campaign_id="c-no-spend")
    code, resp, _ = _post(srv["base"], "/api/worker/lease", {}, bearer=token)
    assert code == 200, resp
    assert resp["data"]["job"]["priorSpendUsd"] == 0.0


def test_ack_rolls_fleet_spend_into_the_cloud_spend_log(srv):
    token, job_id = _seed_spend_job(srv, "ackspend", campaign_id="c-ack-spend")
    _post(srv["base"], "/api/worker/lease", {}, bearer=token)

    code, ack, _ = _post(
        srv["base"], f"/api/worker/jobs/{job_id}/ack",
        {"summary": {"session_id": "s-ack-spend"}, "dbId": "a-remote-box",
         "spend": [{"stage": "match", "model": "m1", "usd": 0.6, "at": 900.0},
                   {"stage": "vision", "model": "m2", "usd": 0.4, "at": 950.0}]},
        bearer=token)

    assert code == 200 and ack["data"]["recorded"] is True
    store = Store(srv["db"])
    try:
        # Before B9 the cloud spend_log never saw a single fleet dollar — the panel
        # showed $0 spent / cpl None, and every box's cap restarted at $0.
        assert store.total_spend("c-ack-spend") == pytest.approx(1.0)
    finally:
        store.close()


def test_nack_rolls_fleet_spend_into_the_cloud_spend_log(srv):
    token, job_id = _seed_spend_job(srv, "nackspend", campaign_id="c-nack-spend")
    _post(srv["base"], "/api/worker/lease", {}, bearer=token)

    code, resp, _ = _post(
        srv["base"], f"/api/worker/jobs/{job_id}/nack",
        {"reason": "error", "dbId": "a-remote-box",
         "spend": [{"stage": "match", "usd": 2.25}]}, bearer=token)

    assert code == 200, resp
    assert resp["data"]["outcome"] == "requeued"   # NOT a 500 with the job left leased
    store = Store(srv["db"])
    try:
        assert store.total_spend("c-nack-spend") == pytest.approx(2.25)
    finally:
        store.close()


def test_ack_rejects_a_non_array_spend_field(srv):
    token, job_id = _seed_spend_job(srv, "badspend", campaign_id="c-bad-spend")
    _post(srv["base"], "/api/worker/lease", {}, bearer=token)
    code, resp, _ = _post(srv["base"], f"/api/worker/jobs/{job_id}/ack",
                          {"summary": {}, "spend": "nope"}, bearer=token)
    assert code == 400, resp


def test_nack_rejects_a_non_string_db_id(srv):
    token, job_id = _seed_spend_job(srv, "baddbid", campaign_id="c-bad-dbid")
    _post(srv["base"], "/api/worker/lease", {}, bearer=token)
    code, resp, _ = _post(srv["base"], f"/api/worker/jobs/{job_id}/nack",
                          {"reason": "x", "dbId": 17}, bearer=token)
    assert code == 400, resp


# ----- job credential (SECURITY REVIEW CRITICAL/HIGH) ----------------------------

def test_credential_endpoint_returns_the_orgs_secret_to_the_lease_holder(srv):
    """The happy path: the worker that actually leased a youtube job asks for its
    credential and gets the org's connected (decrypted) secret back."""
    account = None  # youtube is per-org-credentialed, not account-pinned
    token = _register(srv["base"], machine_id="m-cred-holder",
                      caps=[[1, "youtube", account]])
    store = Store(srv["db"])
    try:
        store.set_integration_secret(1, "youtube", {"api_key": "ORG-YT-KEY"})
    finally:
        store.close()
    code, enq, _ = _enqueue(srv, platform="youtube", account="", job_id="job-cred-1")
    assert code == 200, enq
    code, lease, _ = _post(srv["base"], "/api/worker/lease", {}, bearer=token)
    assert code == 200 and lease["data"]["job"]["id"] == "job-cred-1"

    code, resp, _ = _post(srv["base"], "/api/worker/jobs/job-cred-1/credential", {},
                          bearer=token)
    assert code == 200, resp
    assert resp["data"]["credential"] == {"api_key": "ORG-YT-KEY"}


def test_credential_endpoint_404s_for_a_worker_that_does_not_hold_the_lease(srv):
    """An actual breach attempt: a SECOND, validly-registered, capability-matching
    worker tries to pull the credential for a job the FIRST worker leased. Capability
    matching alone (the pool-wide `[null, platform, null]` wildcard both workers share)
    would let it lease a similar job — but the credential endpoint's gate is the
    per-job lease-holder check, strictly tighter, so this must 404, not 403 (BOLA-safe:
    a 404 discloses neither that the job exists nor who holds it — mirrors the
    'unknown campaign'/'unknown run' convention used elsewhere in server.py)."""
    holder = _register(srv["base"], machine_id="m-cred-owner",
                       caps=[[1, "youtube", None]])
    attacker = _register(srv["base"], machine_id="m-cred-attacker",
                         caps=[[1, "youtube", None]])
    store = Store(srv["db"])
    try:
        store.set_integration_secret(1, "youtube", {"api_key": "ORG-YT-KEY-2"})
    finally:
        store.close()
    _enqueue(srv, platform="youtube", account="", job_id="job-cred-2")
    code, lease, _ = _post(srv["base"], "/api/worker/lease", {}, bearer=holder)
    assert code == 200 and lease["data"]["job"]["id"] == "job-cred-2"

    code, resp, _ = _post(srv["base"], "/api/worker/jobs/job-cred-2/credential", {},
                          bearer=attacker)
    assert code == 404, resp
    assert "credential" not in json.dumps(resp)  # the secret never left the server


def test_credential_endpoint_requires_a_worker_bearer_token(srv):
    # No bearer at all.
    code, resp, _ = _post(srv["base"], "/api/worker/jobs/whatever/credential", {})
    assert code == 401, resp
    assert resp["error"] == "invalid or revoked worker token"
    # A garbage/unregistered bearer is rejected exactly the same way (no distinction
    # that would let a caller tell "no token" from "wrong token").
    code, resp, _ = _post(srv["base"], "/api/worker/jobs/whatever/credential", {},
                          bearer="garbage-not-a-real-token")
    assert code == 401, resp
    assert resp["error"] == "invalid or revoked worker token"


def test_credential_endpoint_is_none_when_nothing_is_connected(srv):
    """A per-org-credentialed platform the org simply hasn't connected yet is NOT an
    error — {ok:true, data:{credential:null}} — the worker's cli.py fallback chain
    (baked -> local store -> env) already treats a legitimate None this way. Uses
    'reddit' (not 'youtube') so it can't collide with a sibling test's org-1 youtube
    secret on this module-scoped shared DB."""
    token = _register(srv["base"], machine_id="m-cred-none", caps=[[1, "reddit", None]])
    _enqueue(srv, platform="reddit", account="", job_id="job-cred-none")
    _post(srv["base"], "/api/worker/lease", {}, bearer=token)
    code, resp, _ = _post(srv["base"], "/api/worker/jobs/job-cred-none/credential", {},
                          bearer=token)
    assert code == 200, resp
    assert resp["data"]["credential"] is None


# ----- job heartbeat / ack / nack ------------------------------------------------

def test_full_lease_heartbeat_ack_round_trip(srv):
    token, account = _isolated(srv, "rt")
    _enqueue(srv, job_id="job-rt", account=account)
    code, lease, _ = _post(srv["base"], "/api/worker/lease", {}, bearer=token)
    assert code == 200 and lease["data"]["job"]["id"] == "job-rt"

    code, hb, _ = _post(srv["base"], "/api/worker/jobs/job-rt/heartbeat",
                        {"jobId": "job-rt"}, bearer=token)
    assert code == 200 and hb["data"]["halt"] is False
    assert hb["data"]["leaseExpiresAt"] is not None

    code, ack, _ = _post(srv["base"], "/api/worker/jobs/job-rt/ack",
                         {"summary": {"matches": 4, "session_id": "s-rt"}}, bearer=token)
    assert code == 200 and ack["data"]["recorded"] is True

    store = Store(srv["db"])
    try:
        assert store.get_job("job-rt")["status"] == "done"
    finally:
        store.close()


def test_heartbeat_syncs_run_events_into_the_cloud_feed(srv):
    # Gap A: a fleet job's live activity reaches the org drawer via the job heartbeat.
    token, account = _isolated(srv, "events")
    _enqueue(srv, job_id="job-ev", account=account)
    _post(srv["base"], "/api/worker/lease", {}, bearer=token)

    body = {"jobId": "job-ev", "runId": "run-ev-1", "runEvents": [
        {"seq": 1, "phase": "scan", "level": "info", "message": "started",
         "sessionId": "s-ev", "createdAt": 100.0},
        {"seq": 2, "phase": "scan", "level": "info", "message": "found a lead",
         "sessionId": "s-ev", "createdAt": 101.0},
    ]}
    code, hb, _ = _post(srv["base"], "/api/worker/jobs/job-ev/heartbeat", body,
                        bearer=token)
    assert code == 200, hb

    store = Store(srv["db"])
    try:
        job = store.get_job("job-ev")
        events = store.fetch_run_events("run-ev-1", org_id=job["orgId"])
        assert [e["message"] for e in events] == ["started", "found a lead"]
        # BOLA: the events are stamped with the JOB's org, not visible to another org.
        assert store.fetch_run_events("run-ev-1", org_id=job["orgId"] + 999) == []
    finally:
        store.close()


def test_heartbeat_on_a_lost_lease_signals_halt(srv):
    token, account = _isolated(srv, "lost")
    _enqueue(srv, job_id="job-lost", account=account)
    _post(srv["base"], "/api/worker/lease", {}, bearer=token)
    _post(srv["base"], "/api/worker/jobs/job-lost/ack",
          {"summary": {}}, bearer=token)  # job now done → lease gone
    code, hb, _ = _post(srv["base"], "/api/worker/jobs/job-lost/heartbeat",
                        {"jobId": "job-lost"}, bearer=token)
    assert code == 200 and hb["data"]["halt"] is True


def test_nack_requeues_and_reports_outcome(srv):
    token, account = _isolated(srv, "nack")
    _enqueue(srv, job_id="job-nack", account=account)
    _post(srv["base"], "/api/worker/lease", {}, bearer=token)
    code, resp, _ = _post(srv["base"], "/api/worker/jobs/job-nack/nack",
                          {"reason": "error"}, bearer=token)
    assert code == 200, resp
    assert resp["data"]["outcome"] == "requeued"
    assert resp["data"]["recorded"] is True
    store = Store(srv["db"])
    try:
        assert store.get_job("job-nack")["status"] == "queued"
    finally:
        store.close()


def test_nack_requires_a_reason(srv):
    token = _register(srv["base"], machine_id="m-nack2")
    code, resp, _ = _post(srv["base"], "/api/worker/jobs/whatever/nack",
                          {}, bearer=token)
    assert code == 400, resp


def test_job_routes_require_a_worker_token(srv):
    for action, body in (("heartbeat", {}), ("ack", {"summary": {}}),
                         ("nack", {"reason": "x"}), ("credential", {})):
        code, resp, _ = _post(srv["base"], f"/api/worker/jobs/jX/{action}", body)
        assert code == 401, (action, resp)


def test_one_worker_cannot_ack_anothers_job(srv):
    account = "acct-owned"
    owner = _register(srv["base"], machine_id="m-owner",
                      caps=[[1, "instagram", account]])
    other = _register(srv["base"], machine_id="m-other",
                      caps=[[1, "instagram", account]])
    _enqueue(srv, job_id="job-owned", account=account)
    _post(srv["base"], "/api/worker/lease", {}, bearer=owner)  # owner leases it
    # The other worker (valid token, same capability) acks the SAME job id → no-op.
    code, resp, _ = _post(srv["base"], "/api/worker/jobs/job-owned/ack",
                          {"summary": {}}, bearer=other)
    assert code == 200 and resp["data"]["recorded"] is False
    store = Store(srv["db"])
    try:
        assert store.get_job("job-owned")["status"] == "leased"  # still owner's
    finally:
        store.close()


# ----- ack lead sync-back (Phase 3) ----------------------------------------------

def test_ack_syncs_captured_leads_into_the_cloud(srv):
    token, account = _isolated(srv, "leadsync")
    _enqueue(srv, job_id="job-leadsync", account=account)
    _post(srv["base"], "/api/worker/lease", {}, bearer=token)
    # Register the job's campaign to an org so the synced lead is org-stamped.
    store = Store(srv["db"])
    try:
        store.upsert_campaign_meta("c-acme", org_id=1)
    finally:
        store.close()
    lead = {"commentId": "sync-cmt-1", "reelId": "r1", "username": "buyer",
            "text": "want it", "score": 0.9, "reason": "intent",
            "extracted": {"phone": "1"}, "tier": "local", "platform": "instagram",
            "sessionId": "s-x", "capturedAt": 900.0}
    code, ack, _ = _post(srv["base"], "/api/worker/jobs/job-leadsync/ack",
                         {"summary": {"session_id": "s-x"}, "leads": [lead]}, bearer=token)
    assert code == 200 and ack["data"]["recorded"] is True
    store = Store(srv["db"])
    try:
        rows = [m for m in store.matches("c-acme") if m["comment_id"] == "sync-cmt-1"]
        assert len(rows) == 1
        assert rows[0]["org_id"] == 1 and rows[0]["status"] == "new"
        assert rows[0]["captured_at"] == 900.0
        assert rows[0]["extracted"] == {"phone": "1"}
    finally:
        store.close()


def test_ack_with_leads_still_requires_a_worker_bearer_token(srv):
    # No bearer → 401: a lead can never be injected anonymously.
    code, resp, _ = _post(srv["base"], "/api/worker/jobs/whatever/ack",
                          {"summary": {}, "leads": [{"commentId": "x", "reelId": "r"}]})
    assert code == 401, resp


def test_ack_rejects_a_non_array_leads_field(srv):
    token, account = _isolated(srv, "badleads")
    _enqueue(srv, job_id="job-badleads", account=account)
    _post(srv["base"], "/api/worker/lease", {}, bearer=token)
    code, resp, _ = _post(srv["base"], "/api/worker/jobs/job-badleads/ack",
                          {"summary": {}, "leads": "nope"}, bearer=token)
    assert code == 400, resp


# ----- execution-backend switch (v16) --------------------------------------------

def test_execution_backend_get_requires_admin(srv):
    code, resp, _ = _req("GET", srv["base"], "/api/admin/execution-backend",
                         cookie=srv["normal_cookie"])
    assert code == 401, resp


def test_execution_backend_defaults_to_in_process(srv):
    code, resp, _ = _req("GET", srv["base"], "/api/admin/execution-backend",
                         cookie=srv["admin_cookie"])
    assert code == 200, resp
    assert resp["data"]["backend"] == "in_process"
    assert set(resp["data"]["options"]) == {"in_process", "distributed"}


def test_execution_backend_switch_round_trips(srv):
    code, resp, _ = _post(srv["base"], "/api/admin/execution-backend",
                          {"backend": "distributed"}, cookie=srv["admin_cookie"])
    assert code == 200 and resp["data"]["backend"] == "distributed"
    code, got, _ = _req("GET", srv["base"], "/api/admin/execution-backend",
                        cookie=srv["admin_cookie"])
    assert got["data"]["backend"] == "distributed"
    # Reset so the shared module DB doesn't leak state into other tests.
    _post(srv["base"], "/api/admin/execution-backend",
          {"backend": "in_process"}, cookie=srv["admin_cookie"])


def test_execution_backend_rejects_unknown(srv):
    code, resp, _ = _post(srv["base"], "/api/admin/execution-backend",
                          {"backend": "magic"}, cookie=srv["admin_cookie"])
    assert code == 400, resp


def test_execution_backend_set_requires_admin(srv):
    code, resp, _ = _post(srv["base"], "/api/admin/execution-backend",
                          {"backend": "distributed"}, cookie=srv["normal_cookie"])
    assert code == 401, resp


# ----- worker report validators (unit) -------------------------------------------

def test_validate_worker_ack_none_payload_has_every_key():
    """The `payload is None` short-circuit must carry `spend` too, so `_handle_job_ack`
    can index `fields["spend"]` unconditionally instead of leaning on `.get`."""
    from aizu.server import _validate_worker_ack
    fields, err = _validate_worker_ack(None)
    assert err is None
    assert fields == {"summary": {}, "leads": [], "spend": [], "dbId": None}


def test_validate_worker_ack_drops_ragged_spend_rows_and_caps():
    from aizu.server import _validate_worker_ack
    from aizu.core.store import MAX_SYNC_SPEND_ROWS
    rows = ["nope", None, 7] + [{"stage": "s", "usd": 1.0}] * (MAX_SYNC_SPEND_ROWS + 5)
    fields, err = _validate_worker_ack({"spend": rows, "dbId": "  box-7  "})
    assert err is None
    assert len(fields["spend"]) == MAX_SYNC_SPEND_ROWS
    assert all(isinstance(r, dict) for r in fields["spend"])
    assert fields["dbId"] == "box-7"          # trimmed


def test_validate_worker_nack_carries_spend_and_db_id():
    from aizu.server import _validate_worker_nack
    fields, err = _validate_worker_nack(
        {"reason": "error", "spend": [{"stage": "match", "usd": 1.0}], "dbId": "box-9"})
    assert err is None
    assert fields["spend"] == [{"stage": "match", "usd": 1.0}]
    assert fields["dbId"] == "box-9"
    # Absent → an empty batch and no sentinel (an older worker binary).
    bare, err = _validate_worker_nack({"reason": "error"})
    assert err is None and bare["spend"] == [] and bare["dbId"] is None
    # Empty-string dbId is not an identity — normalized to None, so nothing is skipped
    # by an accidental "" == "" comparison.
    blank, err = _validate_worker_nack({"reason": "error", "dbId": "   "})
    assert err is None and blank["dbId"] is None
