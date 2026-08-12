"""HTTP-level worker plane (v14): the bearer-gated registry + presence endpoints and
the interim allowlist-gated /api/admin/fleet view. Mirrors the live-server harness in
tests/test_multitenancy_server.py — a real ThreadingHTTPServer on an ephemeral port,
exercised over the wire so the dispatch order, the bearer gate, and the fail-closed
allowlist are the things actually under test."""
import io
import json
import logging
import os
import tempfile
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

import pytest

from aizu.auth import new_session_token
from aizu.server import (WORKER_BOOTSTRAP_ENV, WORKER_LEGACY_BOOTSTRAP_ENV, serve)
from aizu.admin_auth import ADMIN_IP_ALLOWLIST_ENV
from aizu.core.store import Store, hash_session_token
from ._admin import admin_cookie, set_admin_env

ROOT = Path(__file__).resolve().parents[2]
CONFIG = ROOT / "config"
_INDEX_HTML = '<!doctype html><html><body><div id="root"></div></body></html>'
PW = "longenough1"
BOOTSTRAP = "boot-secret-xyz"


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


def _get(base, path, *, cookie=None, bearer=None):
    code, resp, _ = _req("GET", base, path, None, cookie=cookie, bearer=bearer)
    return code, resp


def _cookie(set_cookie):
    return set_cookie.split(";", 1)[0]


def _signup(base, email, company="Co"):
    code, resp, set_cookie = _post(base, "/api/auth/signup",
                                   {"email": email, "password": PW, "companyName": company})
    assert code == 200, resp
    return _cookie(set_cookie)


class _StubRunManager:
    def __init__(self):
        self.launched: list = []

    def launch(self, spec):
        self.launched.append(spec)
        return object(), None

    def status(self, org_id=None):
        return {"active": None, "recent": []}

    def sweep_orphan_pause_files(self):
        return None


@pytest.fixture(scope="module")
def srv():
    fd, db_path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    panel_dir = tempfile.mkdtemp(suffix="-spa")
    (Path(panel_dir) / "index.html").write_text(_INDEX_HTML, encoding="utf-8")
    httpd = serve(db_path, panel_dir, str(CONFIG), port=0,
                  run_manager=_StubRunManager(), billing_providers={})
    port = httpd.server_address[1]
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{port}"

    admin_cookie = _signup(base, "admin@x.io", company="Acme")
    normal_cookie = _signup(base, "normal@x.io", company="Beta")

    yield {"base": base, "db": db_path,
           "admin_cookie": admin_cookie, "normal_cookie": normal_cookie}
    httpd.shutdown()


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    """Each test owns the bootstrap env; default unset (fail closed). The admin plane
    (IP-allowlist + secret key) is configured by default so fleet tests can log in; the
    unset-allowlist test deletes it explicitly."""
    monkeypatch.delenv(WORKER_BOOTSTRAP_ENV, raising=False)
    set_admin_env(monkeypatch)


def _worker_row(db, worker_id):
    store = Store(db)
    try:
        return next((w for w in store.list_workers() if w["id"] == worker_id), None)
    finally:
        store.close()


def _token_hash_in_db(db, worker_id):
    store = Store(db)
    try:
        row = store._conn.execute(
            "SELECT worker_token_hash FROM workers WHERE id=?", (worker_id,)).fetchone()
        return row["worker_token_hash"] if row else None
    finally:
        store.close()


# ----- register --------------------------------------------------------------

def test_register_first_requires_bootstrap_token(srv, monkeypatch):
    # Unset bootstrap ⇒ first register is closed.
    code, resp = _post(srv["base"], "/api/worker/register", {"machineId": "m-none"})[:2]
    assert code == 401, resp

    # Wrong bootstrap secret ⇒ 401.
    monkeypatch.setenv(WORKER_BOOTSTRAP_ENV, BOOTSTRAP)
    code, resp, _ = _post(srv["base"], "/api/worker/register",
                          {"machineId": "m-wrong"}, bearer="not-the-secret")
    assert code == 401, resp

    # Correct bootstrap ⇒ 200 with a token + the heartbeat interval.
    code, resp, _ = _post(srv["base"], "/api/worker/register",
                          {"machineId": "m-ok", "displayName": "Box1",
                           "host": "host1", "os": "linux", "agentVersion": "0.1.0"},
                          bearer=BOOTSTRAP)
    assert code == 200, resp
    assert resp["data"]["workerId"] == "m-ok"
    assert isinstance(resp["data"]["token"], str) and resp["data"]["token"]
    assert resp["data"]["heartbeatIntervalSec"] == 20


def test_register_persists_hash_not_plaintext(srv, monkeypatch):
    monkeypatch.setenv(WORKER_BOOTSTRAP_ENV, BOOTSTRAP)
    code, resp, _ = _post(srv["base"], "/api/worker/register",
                          {"machineId": "m-hash"}, bearer=BOOTSTRAP)
    assert code == 200, resp
    token = resp["data"]["token"]
    stored = _token_hash_in_db(srv["db"], "m-hash")
    assert stored == hash_session_token(token)
    assert stored != token
    # A row really persisted with derived presence fields.
    row = _worker_row(srv["db"], "m-hash")
    assert row is not None and row["status"] in ("online", "stale", "offline")


def test_register_returns_token_exactly_once_and_rotates(srv, monkeypatch):
    monkeypatch.setenv(WORKER_BOOTSTRAP_ENV, BOOTSTRAP)
    _, resp1, _ = _post(srv["base"], "/api/worker/register",
                        {"machineId": "m-rot"}, bearer=BOOTSTRAP)
    token1 = resp1["data"]["token"]
    # Re-register via the EXISTING bearer token rotates it (no bootstrap needed).
    code, resp2, _ = _post(srv["base"], "/api/worker/register",
                           {"machineId": "ignored"}, bearer=token1)
    assert code == 200, resp2
    token2 = resp2["data"]["token"]
    assert token2 != token1
    # Old token no longer authenticates; new one does.
    assert _post(srv["base"], "/api/worker/heartbeat", {}, bearer=token1)[0] == 401
    assert _post(srv["base"], "/api/worker/heartbeat", {}, bearer=token2)[0] == 200


def test_register_token_not_logged(srv, monkeypatch):
    monkeypatch.setenv(WORKER_BOOTSTRAP_ENV, BOOTSTRAP)
    logger = logging.getLogger("aizu.server")
    buf = io.StringIO()
    handler = logging.StreamHandler(buf)
    handler.setLevel(logging.DEBUG)
    prev_level = logger.level
    logger.setLevel(logging.DEBUG)
    logger.addHandler(handler)
    try:
        _, resp, _ = _post(srv["base"], "/api/worker/register",
                           {"machineId": "m-log"}, bearer=BOOTSTRAP)
    finally:
        logger.removeHandler(handler)
        logger.setLevel(prev_level)
    token = resp["data"]["token"]
    assert token not in buf.getvalue()


def test_register_validation_rejects_bad_capabilities(srv, monkeypatch):
    monkeypatch.setenv(WORKER_BOOTSTRAP_ENV, BOOTSTRAP)
    # Not a 3-list.
    code, resp, _ = _post(srv["base"], "/api/worker/register",
                          {"machineId": "m-cap1", "capabilities": [["a", "b"]]},
                          bearer=BOOTSTRAP)
    assert code == 400, resp
    # Unknown platform.
    code, resp, _ = _post(srv["base"], "/api/worker/register",
                          {"machineId": "m-cap2", "capabilities": [[1, "myspace", "h"]]},
                          bearer=BOOTSTRAP)
    assert code == 400, resp
    # Valid trio round-trips.
    code, resp, _ = _post(srv["base"], "/api/worker/register",
                          {"machineId": "m-cap3",
                           "capabilities": [[1, "instagram", "acme"]]},
                          bearer=BOOTSTRAP)
    assert code == 200, resp
    row = _worker_row(srv["db"], "m-cap3")
    assert row["capabilities"] == [[1, "instagram", "acme"]]


def test_register_accepts_pool_wide_capabilities_with_null_handle(srv, monkeypatch):
    """A pool-wide/unpinned capability (org None, handle None) MUST register — the lease
    matcher is built to treat a None handle as unpinned, so the register validator must
    not forbid it (else a fleet run is rejected as 'no capable worker')."""
    monkeypatch.setenv(WORKER_BOOTSTRAP_ENV, BOOTSTRAP)
    code, resp, _ = _post(srv["base"], "/api/worker/register",
                          {"machineId": "m-cap-pool",
                           "capabilities": [[None, "instagram", None],
                                            [None, "x", None]]},
                          bearer=BOOTSTRAP)
    assert code == 200, resp
    row = _worker_row(srv["db"], "m-cap-pool")
    assert row["capabilities"] == [[None, "instagram", None], [None, "x", None]]


def test_register_first_requires_machine_id(srv, monkeypatch):
    monkeypatch.setenv(WORKER_BOOTSTRAP_ENV, BOOTSTRAP)
    code, resp, _ = _post(srv["base"], "/api/worker/register", {}, bearer=BOOTSTRAP)
    assert code == 400, resp


# ----- v22 enrolment tokens (BUILD-PLAN B8 fix) — real HTTP routes, not a unit test
# of the clamp function (§B4 lesson: a fix that never rides the served route is
# inert). Tokens are minted directly via the Store (the mint HTTP endpoint gets its
# own admin-plane tests in test_lifecycle_controls_server.py); everything AFTER the
# mint — register, the clamp, and (for the security-core case) a real lease — goes
# through the live ThreadingHTTPServer exactly as a real worker box would hit it. --

def _mint_enrolment_token(db_path, *, scope_kind, org_id=None, worker_id_hint="wet"):
    token = new_session_token()
    store = Store(db_path)
    try:
        store.create_worker_enrolment_token(
            token_id=f"wet-{worker_id_hint}", token=token, scope_kind=scope_kind,
            org_id=org_id, label=None, created_by_admin_id=None,
            expires_at=time.time() + 3600.0)
    finally:
        store.close()
    return token


def test_register_enrolment_token_clamps_org_scope_overriding_self_declaration(srv):
    """The security core of B8: an org-scoped enrolment token CLAMPS org_id and every
    capability's cap_org to the token's org, even when the request body declares a
    DIFFERENT orgId and pool-wide (cap_org=None) capabilities."""
    token = _mint_enrolment_token(srv["db"], scope_kind="org", org_id=1,
                                  worker_id_hint="clampA")
    code, resp, _ = _post(
        srv["base"], "/api/worker/register",
        {"machineId": "m-clamp-org", "orgId": 999,  # self-declared — must be overridden
         "capabilities": [[None, "instagram", None], [999, "youtube", None]]},
        bearer=token)
    assert code == 200, resp
    row = _worker_row(srv["db"], "m-clamp-org")
    assert row["orgId"] == 1
    assert row["capabilities"] == [[1, "instagram", None], [1, "youtube", None]]


def test_register_enrolment_token_pool_scope_leaves_capabilities_unclamped(srv):
    """A 'pool' enrolment token is the deliberate multi-org grant (PRD: one managed
    box serving ~10 companies) — org_id is set to None and capabilities pass through
    exactly as the box declared, unclamped."""
    token = _mint_enrolment_token(srv["db"], scope_kind="pool", org_id=None,
                                  worker_id_hint="poolB")
    code, resp, _ = _post(
        srv["base"], "/api/worker/register",
        {"machineId": "m-clamp-pool", "orgId": 42,
         "capabilities": [[7, "instagram", "acct"], [None, "youtube", None]]},
        bearer=token)
    assert code == 200, resp
    row = _worker_row(srv["db"], "m-clamp-pool")
    assert row["orgId"] is None
    assert row["capabilities"] == [[7, "instagram", "acct"], [None, "youtube", None]]


def test_register_reregister_cannot_escalate_org_scoped_worker(srv):
    """v22.1 regression (BUILD-PLAN B8 follow-up, memory/known-issues.md B8): the
    org-scoped enrolment token's clamp must hold on EVERY subsequent re-register of
    that same worker's OWN bearer token, not just the redemption call. Before this
    fix, a re-register trusted the request body's orgId/capabilities verbatim
    (worker is not None ⇒ enrolment_scope stayed None), so a box enrolled for org 1
    could immediately re-register itself into org 999's scope — and, via a real
    lease, receive org 999's decrypted platform credential — using nothing but its
    own already-issued bearer token. No shared secret, no second enrolment token."""
    token = _mint_enrolment_token(srv["db"], scope_kind="org", org_id=1,
                                  worker_id_hint="reesc")
    code, resp, _ = _post(
        srv["base"], "/api/worker/register",
        {"machineId": "m-reesc", "orgId": 1, "capabilities": [[1, "instagram", None]]},
        bearer=token)
    assert code == 200, resp
    worker_token = resp["data"]["token"]
    # Attacker/box re-registers with its OWN bearer, self-declaring a DIFFERENT org
    # and pool-wide (cap_org=None) capabilities — must be clamped right back to the
    # token's original org, exactly as the first register was.
    code2, resp2, _ = _post(
        srv["base"], "/api/worker/register",
        {"machineId": "ignored", "orgId": 999,
         "capabilities": [[None, "youtube", None]]},
        bearer=worker_token)
    assert code2 == 200, resp2
    row = _worker_row(srv["db"], "m-reesc")
    assert row["orgId"] == 1
    assert row["capabilities"] == [[1, "youtube", None]]


def test_register_reregister_pool_scoped_worker_org_id_stays_none(srv):
    """A pool-scoped worker's org_id (deliberately None — the multi-org grant)
    stays None on re-register too, instead of adopting a self-declared orgId;
    capabilities remain unclamped on re-register, exactly as at enrolment."""
    token = _mint_enrolment_token(srv["db"], scope_kind="pool", worker_id_hint="repool")
    code, resp, _ = _post(srv["base"], "/api/worker/register",
                          {"machineId": "m-repool"}, bearer=token)
    assert code == 200, resp
    worker_token = resp["data"]["token"]
    code2, resp2, _ = _post(
        srv["base"], "/api/worker/register",
        {"machineId": "ignored", "orgId": 42,
         "capabilities": [[7, "instagram", "acct"]]},
        bearer=worker_token)
    assert code2 == 200, resp2
    row = _worker_row(srv["db"], "m-repool")
    assert row["orgId"] is None
    assert row["capabilities"] == [[7, "instagram", "acct"]]


def test_register_enrolment_token_is_single_use(srv, monkeypatch):
    """A redeemed token cannot register a second box; a second attempt bearing the
    SAME (already-used) token is not a valid bootstrap secret either, so it 401s."""
    monkeypatch.delenv(WORKER_BOOTSTRAP_ENV, raising=False)
    token = _mint_enrolment_token(srv["db"], scope_kind="pool", worker_id_hint="once")
    first = _post(srv["base"], "/api/worker/register",
                  {"machineId": "m-once-a"}, bearer=token)
    assert first[0] == 200, first[1]
    second = _post(srv["base"], "/api/worker/register",
                   {"machineId": "m-once-b"}, bearer=token)
    assert second[0] == 401, second[1]


def test_register_enrolment_token_failure_falls_back_to_legacy_bootstrap(srv, monkeypatch):
    """An unredeemable bearer (garbage, or an already-used enrolment token) is not
    fatal by itself — the legacy shared-secret check still runs as a fallback while
    AIZU_WORKER_LEGACY_BOOTSTRAP_ENABLED is on (the default)."""
    monkeypatch.setenv(WORKER_BOOTSTRAP_ENV, BOOTSTRAP)
    monkeypatch.delenv(WORKER_LEGACY_BOOTSTRAP_ENV, raising=False)  # default = on
    code, resp, _ = _post(srv["base"], "/api/worker/register",
                          {"machineId": "m-legacy-fallback"}, bearer=BOOTSTRAP)
    assert code == 200, resp


def test_register_legacy_bootstrap_disabled_rejects_shared_secret(srv, monkeypatch):
    """(d) With the legacy flag OFF, the correct shared secret alone (no enrolment
    token) is no longer sufficient — 401. The identical request with the flag
    unset/'1' still succeeds (regression guard for the default-on migration
    promise)."""
    monkeypatch.setenv(WORKER_BOOTSTRAP_ENV, BOOTSTRAP)
    monkeypatch.setenv(WORKER_LEGACY_BOOTSTRAP_ENV, "0")
    code, resp, _ = _post(srv["base"], "/api/worker/register",
                          {"machineId": "m-legacy-off"}, bearer=BOOTSTRAP)
    assert code == 401, resp

    monkeypatch.setenv(WORKER_LEGACY_BOOTSTRAP_ENV, "1")
    code, resp, _ = _post(srv["base"], "/api/worker/register",
                          {"machineId": "m-legacy-on"}, bearer=BOOTSTRAP)
    assert code == 200, resp

    monkeypatch.delenv(WORKER_LEGACY_BOOTSTRAP_ENV, raising=False)  # unset = on
    code, resp, _ = _post(srv["base"], "/api/worker/register",
                          {"machineId": "m-legacy-unset"}, bearer=BOOTSTRAP)
    assert code == 200, resp


def test_register_existing_bearer_reregister_unaffected_by_enrolment_state(srv, monkeypatch):
    """(e) The steady-state re-register path (worker's OWN bearer) is untouched by
    ANY of this for a worker that was ITSELF enrolled via the deprecated legacy
    bootstrap fallback (no enrolment token ⇒ enrolment_scope_kind stays NULL) — it
    still overwrites org_id/capabilities from the request body verbatim, exactly as
    before. This is deliberately narrower than it used to be: an enrolment-token-
    scoped worker's re-register IS now re-clamped every time — see
    test_register_reregister_cannot_escalate_org_scoped_worker (v22.1, the fix for
    the self-escalation gap this test's old, blanket docstring used to paper over).
    Locks in the hard constraint so a future change can't silently regress it toward
    the C4 widening bug left deliberately alone."""
    monkeypatch.setenv(WORKER_BOOTSTRAP_ENV, BOOTSTRAP)
    _, resp, _ = _post(srv["base"], "/api/worker/register",
                       {"machineId": "m-steady", "orgId": 1,
                        "capabilities": [[1, "instagram", "h"]]},
                       bearer=BOOTSTRAP)
    token = resp["data"]["token"]
    code, resp2, _ = _post(
        srv["base"], "/api/worker/register",
        {"machineId": "ignored", "orgId": 2,
         "capabilities": [[None, "youtube", None]]},
        bearer=token)
    assert code == 200, resp2
    row = _worker_row(srv["db"], "m-steady")
    assert row["orgId"] == 2
    assert row["capabilities"] == [[None, "youtube", None]]


def test_register_deprecation_warning_fires_only_on_legacy_path(srv, monkeypatch):
    """(f) The deprecation WARNING fires exactly when the legacy shared-secret
    fallback is actually used, and NOT when a valid enrolment token redeems."""
    monkeypatch.setenv(WORKER_BOOTSTRAP_ENV, BOOTSTRAP)
    logger = logging.getLogger("aizu.server")
    buf = io.StringIO()
    handler = logging.StreamHandler(buf)
    handler.setLevel(logging.WARNING)
    prev_level = logger.level
    logger.setLevel(logging.WARNING)
    logger.addHandler(handler)
    try:
        # Enrolment-token path: no warning.
        token = _mint_enrolment_token(srv["db"], scope_kind="pool", worker_id_hint="nowarn")
        code, resp, _ = _post(srv["base"], "/api/worker/register",
                              {"machineId": "m-no-warn"}, bearer=token)
        assert code == 200, resp
        assert "DEPRECATED" not in buf.getvalue()
        # Legacy fallback path: warning fires.
        code, resp, _ = _post(srv["base"], "/api/worker/register",
                              {"machineId": "m-warn"}, bearer=BOOTSTRAP)
        assert code == 200, resp
        assert "DEPRECATED" in buf.getvalue()
    finally:
        logger.removeHandler(handler)
        logger.setLevel(prev_level)


# ----- heartbeat -------------------------------------------------------------

def _fresh_worker(srv, monkeypatch, worker_id):
    monkeypatch.setenv(WORKER_BOOTSTRAP_ENV, BOOTSTRAP)
    _, resp, _ = _post(srv["base"], "/api/worker/register",
                       {"machineId": worker_id, "maxSessions": 5}, bearer=BOOTSTRAP)
    return resp["data"]["token"]


def test_heartbeat_requires_valid_bearer(srv, monkeypatch):
    token = _fresh_worker(srv, monkeypatch, "m-hb")
    # No bearer ⇒ 401.
    assert _post(srv["base"], "/api/worker/heartbeat", {})[0] == 401
    # Garbage bearer ⇒ 401.
    assert _post(srv["base"], "/api/worker/heartbeat", {}, bearer="garbage")[0] == 401
    # Valid bearer ⇒ 200 with the placeholder control flags.
    code, resp, _ = _post(srv["base"], "/api/worker/heartbeat", {}, bearer=token)
    assert code == 200, resp
    assert resp["data"] == {"drain": False, "halt": False, "updateRequired": False}


def test_heartbeat_revoked_token_is_401(srv, monkeypatch):
    token = _fresh_worker(srv, monkeypatch, "m-revoked")
    store = Store(srv["db"])
    try:
        assert store.revoke_worker("m-revoked") is True
    finally:
        store.close()
    assert _post(srv["base"], "/api/worker/heartbeat", {}, bearer=token)[0] == 401


def test_heartbeat_ignores_body_workerid_uses_token(srv, monkeypatch):
    token_a = _fresh_worker(srv, monkeypatch, "m-a")
    _fresh_worker(srv, monkeypatch, "m-b")
    before_b = _worker_row(srv["db"], "m-b")["lastHeartbeatAt"]
    # A's token beats, but the body claims to be B — only A's row may move.
    code, _, _ = _post(srv["base"], "/api/worker/heartbeat",
                       {"workerId": "m-b", "currentSessions": 2}, bearer=token_a)
    assert code == 200
    after_a = _worker_row(srv["db"], "m-a")
    after_b = _worker_row(srv["db"], "m-b")
    assert after_a["currentSessions"] == 2
    assert after_b["lastHeartbeatAt"] == before_b  # untouched


def test_heartbeat_updates_current_sessions(srv, monkeypatch):
    token = _fresh_worker(srv, monkeypatch, "m-load")
    _post(srv["base"], "/api/worker/heartbeat", {"currentSessions": 3}, bearer=token)
    assert _worker_row(srv["db"], "m-load")["currentSessions"] == 3
    # Omitting it leaves the stored value unchanged.
    _post(srv["base"], "/api/worker/heartbeat", {}, bearer=token)
    assert _worker_row(srv["db"], "m-load")["currentSessions"] == 3


# ----- fleet -----------------------------------------------------------------

def test_fleet_401_without_session(srv):
    code, resp = _get(srv["base"], "/api/admin/fleet")
    assert code == 401, resp


def test_fleet_401_for_a_plain_org_cookie(srv):
    # An org user session is not an admin session — the real gate rejects it (401).
    code, resp = _get(srv["base"], "/api/admin/fleet", cookie=srv["normal_cookie"])
    assert code == 401, resp


def test_fleet_401_when_ip_allowlist_unset(srv, monkeypatch):
    # A valid admin cookie, but the IP-allowlist is unset ⇒ the gate fails closed.
    cookie = admin_cookie(srv["base"], srv["db"], email="fleet-a@x.io")
    monkeypatch.delenv(ADMIN_IP_ALLOWLIST_ENV, raising=False)
    code, resp = _get(srv["base"], "/api/admin/fleet", cookie=cookie)
    assert code == 401, resp


def test_fleet_200_for_admin_session(srv, monkeypatch):
    monkeypatch.setenv(WORKER_BOOTSTRAP_ENV, BOOTSTRAP)
    _post(srv["base"], "/api/worker/register",
          {"machineId": "m-fleet", "displayName": "Fleeter"}, bearer=BOOTSTRAP)
    cookie = admin_cookie(srv["base"], srv["db"], email="fleet-b@x.io")
    code, resp = _get(srv["base"], "/api/admin/fleet", cookie=cookie)
    assert code == 200, resp
    workers = resp["data"]["workers"]
    assert isinstance(workers, list)
    target = next(w for w in workers if w["id"] == "m-fleet")
    assert target["status"] in ("online", "stale", "offline")
    assert "lastSeenAgeSec" in target
    assert target["displayName"] == "Fleeter"


def test_fleet_envelope_shape(srv, monkeypatch):
    cookie = admin_cookie(srv["base"], srv["db"], email="fleet-c@x.io")
    code, resp = _get(srv["base"], "/api/admin/fleet", cookie=cookie)
    assert code == 200, resp
    assert set(resp.keys()) == {"ok", "data", "error"}
    assert resp["ok"] is True and resp["error"] is None
    assert "workers" in resp["data"]


# ----- dispatch ordering -----------------------------------------------------

def test_worker_routes_bypass_cookie_gate(srv, monkeypatch):
    # A worker request with NO cookie still reaches the bearer handler (proves the
    # worker block is matched before the session gate): garbage bearer → 401 from
    # the worker handler, NOT a generic "authentication required".
    code, resp, _ = _post(srv["base"], "/api/worker/heartbeat", {}, bearer="garbage")
    assert code == 401
    assert resp["error"] == "invalid or revoked worker token"


def test_fleet_does_not_require_bearer(srv, monkeypatch):
    # The fleet route is admin-session gated, NOT bearer — an admin cookie alone
    # reaches a 200 with no Authorization header present.
    cookie = admin_cookie(srv["base"], srv["db"], email="fleet-d@x.io")
    code, _ = _get(srv["base"], "/api/admin/fleet", cookie=cookie)
    assert code == 200
