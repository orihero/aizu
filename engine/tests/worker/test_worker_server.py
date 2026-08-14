"""HTTP-level worker plane (v14): the bearer-gated registry + presence endpoints and
the interim allowlist-gated /api/admin/fleet view. Mirrors the live-server harness in
tests/test_multitenancy_server.py — a real ThreadingHTTPServer on an ephemeral port,
exercised over the wire so the dispatch order, the bearer gate, and the fail-closed
allowlist are the things actually under test."""
import io
import json
import logging
import os
import sqlite3
import tempfile
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

import pytest

from aizu import readiness
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


def _req(method, base, path, body=None, *, cookie=None, bearer=None, raw=None):
    # `raw` sends exact bytes (e.g. b"null") so a test can drive a body shape json.dumps
    # of a Python object cannot express as "present but null".
    data = raw if raw is not None else (
        json.dumps(body).encode() if body is not None else None)
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


# ----- B10: revocation must survive the box ----------------------------------
# The sidecar retires its persisted token when the dispatch confirms a 401. That moves
# its next start off the re-register branch and onto FIRST register, where it presents
# the still-configured shared AIZU_WORKER_BOOTSTRAP_TOKEN — and `register_worker` UPSERTs
# `revoked_at = NULL`. Without the guard below, every revoked box resurrects itself on
# the next reboot / Tauri watchdog relaunch / desktop "Restart worker" click, and the
# panel's Revoke button means nothing while the legacy fallback is on.

def test_a_revoked_worker_cannot_resurrect_itself_with_the_shared_bootstrap_token(
        srv, monkeypatch, caplog):
    monkeypatch.setenv(WORKER_BOOTSTRAP_ENV, BOOTSTRAP)
    monkeypatch.delenv(WORKER_LEGACY_BOOTSTRAP_ENV, raising=False)   # default = on
    code, resp, _ = _post(srv["base"], "/api/worker/register",
                          {"machineId": "m-revoked-boot"}, bearer=BOOTSTRAP)
    assert code == 200, resp

    store = Store(srv["db"])
    try:
        assert store.revoke_worker("m-revoked-boot") is True
    finally:
        store.close()

    # Exactly what a restarted, token-less box does: first-register on the shared secret.
    with caplog.at_level(logging.WARNING, logger="aizu.server"):
        code, resp, _ = _post(srv["base"], "/api/worker/register",
                              {"machineId": "m-revoked-boot"}, bearer=BOOTSTRAP)
    assert code == 401, resp
    assert "revoked" in resp["error"]
    assert _worker_row(srv["db"], "m-revoked-boot")["revokedAt"] is not None


def test_a_revoked_worker_IS_brought_back_by_a_fresh_enrolment_token(srv, monkeypatch):
    """The recovery path B10 exists to open stays open — and it is the ONLY one. An
    admin-minted, single-use enrolment token is a deliberate act by a human with panel
    access, unlike the shared secret already sitting in the box's own environment."""
    monkeypatch.setenv(WORKER_BOOTSTRAP_ENV, BOOTSTRAP)
    code, resp, _ = _post(srv["base"], "/api/worker/register",
                          {"machineId": "m-revoked-reenrol"}, bearer=BOOTSTRAP)
    assert code == 200, resp
    store = Store(srv["db"])
    try:
        assert store.revoke_worker("m-revoked-reenrol") is True
    finally:
        store.close()

    token = _mint_enrolment_token(srv["db"], scope_kind="pool",
                                  worker_id_hint="reenrol")
    code, resp, _ = _post(srv["base"], "/api/worker/register",
                          {"machineId": "m-revoked-reenrol"}, bearer=token)

    assert code == 200, resp
    assert _worker_row(srv["db"], "m-revoked-reenrol")["revokedAt"] is None


def test_an_unknown_machine_still_enrols_normally(srv, monkeypatch):
    """Companion guard: the revocation check must key on a REVOKED row, never on the
    mere absence of one — a fresh box, or one whose row a DB reset removed (C3), has to
    keep enrolling on the documented path."""
    monkeypatch.setenv(WORKER_BOOTSTRAP_ENV, BOOTSTRAP)
    code, resp, _ = _post(srv["base"], "/api/worker/register",
                          {"machineId": "m-never-seen"}, bearer=BOOTSTRAP)
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


# ----- v23 launch preflight: register/heartbeat → fleet console (B4 trap) -----
#
# EVERY test in this block goes over the WIRE. The store already accepted a `preflight`
# column before this diff landed and the field was still silently dropped, because
# _validate_worker_register builds its `out` dict key-by-key — exactly the B4 shape that
# shipped inert twice. A store-level test proves nothing about that; only a real POST
# followed by a real GET /api/admin/fleet does.

_GOOD_PREFLIGHT = {
    "ok": False, "blocking": True, "enforced": True, "ranAt": 1786800000.12,
    "failed": [
        {"id": "token_persistence", "severity": "fatal", "status": "fail",
         "detail": "encrypted-file backend: SecretCipherError: AIZU_SECRET_KEY is not set"},
        {"id": "capabilities", "severity": "fatal", "status": "fail",
         "detail": "neither AIZU_WORKER_PLATFORMS nor AIZU_WORKER_CAPABILITIES is set"},
        {"id": "login.instagram", "severity": "warn", "status": "fail",
         "detail": "logged_out"},
    ],
}


def _fleet_worker(base, db, worker_id, email):
    cookie = admin_cookie(base, db, email=email)
    code, resp = _get(base, "/api/admin/fleet", cookie=cookie)
    assert code == 200, resp
    return next(w for w in resp["data"]["workers"] if w["id"] == worker_id)


def test_register_preflight_reaches_the_fleet_console_over_the_wire(srv, monkeypatch):
    """THE B4 test. Register carrying a preflight summary, then read it back out of the
    served admin fleet endpoint — the two ends of the only channel an admin has into a
    box nobody can SSH into (F12)."""
    monkeypatch.setenv(WORKER_BOOTSTRAP_ENV, BOOTSTRAP)
    code, resp, _ = _post(srv["base"], "/api/worker/register",
                          {"machineId": "m-pf", "displayName": "PF box",
                           "capabilities": [], "preflight": _GOOD_PREFLIGHT},
                          bearer=BOOTSTRAP)
    assert code == 200, resp
    target = _fleet_worker(srv["base"], srv["db"], "m-pf", "fleet-pf1@x.io")
    assert target["preflight"] == _GOOD_PREFLIGHT
    # The enforcement half: a blocking box registers normally but advertises nothing.
    assert target["capabilities"] == []


def test_register_without_preflight_reads_as_null_not_missing(srv, monkeypatch):
    """A pre-v23 sidecar must render as "never reported one", never as healthy and never
    as a crash — the whole fleet predates this field."""
    monkeypatch.setenv(WORKER_BOOTSTRAP_ENV, BOOTSTRAP)
    assert _post(srv["base"], "/api/worker/register", {"machineId": "m-pf-none"},
                 bearer=BOOTSTRAP)[0] == 200
    target = _fleet_worker(srv["base"], srv["db"], "m-pf-none", "fleet-pf2@x.io")
    assert "preflight" in target and target["preflight"] is None


def test_heartbeat_preflight_updates_then_coalesces_over_the_wire(srv, monkeypatch):
    """§4.4: the sidecar only re-sends on change (or every 10th beat), so a beat WITHOUT
    the field must leave the stored summary alone. Getting this backwards blanks the
    console between changes, which both UIs render as "checking…" forever."""
    monkeypatch.setenv(WORKER_BOOTSTRAP_ENV, BOOTSTRAP)
    _, resp, _ = _post(srv["base"], "/api/worker/register",
                       {"machineId": "m-pf-hb", "preflight": _GOOD_PREFLIGHT},
                       bearer=BOOTSTRAP)
    token = resp["data"]["token"]
    healed = {"ok": True, "blocking": False, "enforced": True, "ranAt": 1786800900.0,
              "failed": []}
    assert _post(srv["base"], "/api/worker/heartbeat", {"preflight": healed},
                 bearer=token)[0] == 200
    assert _fleet_worker(srv["base"], srv["db"], "m-pf-hb",
                         "fleet-pf3@x.io")["preflight"] == healed
    # A beat with no preflight key at all: COALESCE keeps the stored one.
    assert _post(srv["base"], "/api/worker/heartbeat", {"currentSessions": 1},
                 bearer=token)[0] == 200
    assert _fleet_worker(srv["base"], srv["db"], "m-pf-hb",
                         "fleet-pf4@x.io")["preflight"] == healed


def test_null_body_heartbeat_still_succeeds_with_preflight_wired(srv, monkeypatch):
    """_validate_worker_heartbeat's `payload is None` early return has to carry the new
    key too, or the handler KeyErrors into a 500 on every JSON-`null` beat — the exact
    regression shape a key-by-key validator invites. (`None` reaches the validator only
    from a literal `null` body; a truly empty body is rejected earlier at the length
    check, so this is the branch that must be exercised.)"""
    monkeypatch.setenv(WORKER_BOOTSTRAP_ENV, BOOTSTRAP)
    _, resp, _ = _post(srv["base"], "/api/worker/register", {"machineId": "m-pf-empty"},
                       bearer=BOOTSTRAP)
    token = resp["data"]["token"]
    code, body, _ = _req("POST", srv["base"], "/api/worker/heartbeat", None,
                         bearer=token, raw=b"null")
    assert code == 200, body
    # And it did not blank the stored summary on the way through.
    assert _fleet_worker(srv["base"], srv["db"], "m-pf-empty",
                         "fleet-pf8@x.io")["preflight"] is None


def test_malformed_preflight_is_dropped_never_a_400(srv, monkeypatch):
    """B9 rule: a diagnostic hint must NEVER be the reason a workable box cannot
    register. Every one of these bodies is garbage; every one still enrols the box."""
    monkeypatch.setenv(WORKER_BOOTSTRAP_ENV, BOOTSTRAP)
    for i, junk in enumerate([
        "not-an-object", 42, [], {"failed": "not-a-list"},
        {"ok": "yes", "failed": [None, "x", {"id": 7}]},
        # Unknown ids, bad severities and an unsupported login platform are dropped ROW
        # BY ROW — one bad row never discards a report that also carries a real cause.
        {"ok": False, "blocking": True, "enforced": True,
         "failed": [{"id": "rm -rf", "severity": "fatal", "detail": "x"},
                    {"id": "capabilities", "severity": "critical", "detail": "x"},
                    {"id": "login.myspace", "severity": "warn", "detail": "x"},
                    {"id": "cdp_reachable", "severity": "fatal", "detail": "real"}]},
    ]):
        wid = f"m-pf-junk-{i}"
        code, resp, _ = _post(srv["base"], "/api/worker/register",
                              {"machineId": wid, "preflight": junk}, bearer=BOOTSTRAP)
        assert code == 200, (junk, resp)
    survivor = _fleet_worker(srv["base"], srv["db"], "m-pf-junk-5", "fleet-pf5@x.io")
    assert survivor["preflight"]["failed"] == [
        # `status` is absent from every junk row above and defaults to "fail" — the
        # reading that never under-reports a problem from an older sidecar.
        {"id": "cdp_reachable", "severity": "fatal", "status": "fail", "detail": "real"}]


def test_oversized_preflight_is_dropped_and_the_register_still_succeeds(srv, monkeypatch):
    monkeypatch.setenv(WORKER_BOOTSTRAP_ENV, BOOTSTRAP)
    fat = {"ok": False, "blocking": True, "enforced": True,
           "failed": [{"id": "cdp_reachable", "severity": "fatal", "detail": "x" * 5000}]
                     * 40}
    code, resp, _ = _post(srv["base"], "/api/worker/register",
                          {"machineId": "m-pf-fat", "preflight": fat}, bearer=BOOTSTRAP)
    assert code == 200, resp
    target = _fleet_worker(srv["base"], srv["db"], "m-pf-fat", "fleet-pf6@x.io")
    # Rows are capped at 16 and details at 200 chars, so this one survives trimmed
    # rather than being dropped whole — either outcome is fine, a 400 is not.
    assert target["preflight"] is None or (
        len(target["preflight"]["failed"]) <= 16
        and all(len(r["detail"]) <= 200 for r in target["preflight"]["failed"]))


def test_preflight_cannot_be_used_to_smuggle_extra_keys(srv, monkeypatch):
    """`detail` is worker-authored text rendered in the SUPERADMIN console (E1/E2/F18).
    The validator rebuilds the object key-by-key rather than passing the dict through,
    so nothing the box invents reaches that surface."""
    monkeypatch.setenv(WORKER_BOOTSTRAP_ENV, BOOTSTRAP)
    _post(srv["base"], "/api/worker/register",
          {"machineId": "m-pf-smuggle",
           "preflight": {"ok": True, "blocking": False, "enforced": True, "failed": [
               {"id": "playwright", "severity": "warn", "detail": "d",
                "remedy": "<img src=x onerror=alert(1)>", "title": "pwned"}],
               "html": "<script>", "extra": {"deep": 1}}},
          bearer=BOOTSTRAP)
    pf = _fleet_worker(srv["base"], srv["db"], "m-pf-smuggle",
                       "fleet-pf7@x.io")["preflight"]
    assert set(pf) == {"ok", "blocking", "enforced", "failed"}
    assert set(pf["failed"][0]) == {"id", "severity", "status", "detail"}


def test_a_blocking_preflight_reaches_the_tenant_readiness_banner(srv, monkeypatch):
    """The second consumer of the stored summary: `readiness.fleet_readiness`, which
    `GET /api/agent/readiness` runs over `store.list_workers()` on the distributed
    backend. A box that is online AND capable but parked by its own preflight must NOT
    count as ready — a false `ready:true` is the tenant-visible half of F9.2/F10, and it
    is worse than no banner because it tells an operator to expect leads from a fleet
    that cannot produce any.

    The REGISTER half goes over the wire, then the verdict is computed from the rows the
    server actually persisted — not a hand-built dict, which is the whole point. It is
    NOT driven through `GET /api/agent/readiness` because (a) readiness is a property of
    the WHOLE fleet and this module-scoped DB already holds a dozen workers from earlier
    tests, and (b) a second `serve()` in one process is unsafe: `_configure_and_start`
    rebinds `PanelHandler.db_path` as a CLASS attribute, so spinning up an isolated
    server silently repoints the module's existing server at the new database.
    """
    monkeypatch.setenv(WORKER_BOOTSTRAP_ENV, BOOTSTRAP)
    caps = [[None, "instagram", None]]

    def _rows(*worker_ids):
        store = Store(srv["db"])
        try:
            by_id = {w["id"]: w for w in store.list_workers()}
        finally:
            store.close()
        return [by_id[w] for w in worker_ids]

    # A capability-less box: online, and still not ready (F9.2 — this is the assertion
    # the original fleet_readiness test had backwards).
    assert _post(srv["base"], "/api/worker/register", {"machineId": "m-rdy-bare"},
                 bearer=BOOTSTRAP)[0] == 200
    verdict = readiness.fleet_readiness(_rows("m-rdy-bare"))
    assert verdict["ready"] is False and "none advertises a platform" in verdict["detail"]

    # Capable, but parked by its own preflight ⇒ still not ready, with the reason.
    assert _post(srv["base"], "/api/worker/register",
                 {"machineId": "m-rdy-parked", "capabilities": caps,
                  "preflight": {"ok": False, "blocking": True, "enforced": True,
                                "failed": [{"id": "cdp_attachable", "severity": "fatal",
                                            "detail": "refuses a DevTools attach"}]}},
                 bearer=BOOTSTRAP)[0] == 200
    verdict = readiness.fleet_readiness(_rows("m-rdy-bare", "m-rdy-parked"))
    assert verdict["ready"] is False and "parked" in verdict["detail"]

    # Heal it: same box, same capability, a clean report ⇒ ready. This is the
    # re-register-on-heal path, so it also proves a stale red report is REPLACED.
    assert _post(srv["base"], "/api/worker/register",
                 {"machineId": "m-rdy-parked", "capabilities": caps,
                  "preflight": {"ok": True, "blocking": False, "enforced": True,
                                "failed": []}}, bearer=BOOTSTRAP)[0] == 200
    assert readiness.fleet_readiness(_rows("m-rdy-parked"))["ready"] is True


def test_preflight_check_id_whitelist_covers_the_preflight_module():
    """Drift guard. server.py keeps the id whitelist as literals so the bridge takes no
    static dependency on the sidecar package — this test is what makes that safe: add a
    check id in worker/preflight.py without adding it here and the row would be silently
    dropped on the wire, which is B4 all over again."""
    from aizu import server
    from aizu.worker import preflight

    module_ids = {v for k, v in vars(preflight).items()
                  if k.startswith("CHECK_") and isinstance(v, str)
                  and k != "CHECK_LOGIN_PREFIX"}
    assert module_ids, "no CHECK_* constants found — did preflight.py move?"
    assert module_ids <= server._WORKER_PREFLIGHT_CHECK_IDS
    assert preflight.CHECK_LOGIN_PREFIX == server._WORKER_PREFLIGHT_LOGIN_PREFIX
    # And the caps the worker trims to are the caps the server enforces.
    assert preflight.MAX_UPSTREAM_FAILED == server._WORKER_MAX_PREFLIGHT_FAILED
    assert preflight.MAX_UPSTREAM_DETAIL == server._WORKER_MAX_PREFLIGHT_DETAIL
    assert preflight.MAX_UPSTREAM_BYTES == server._WORKER_MAX_PREFLIGHT_BYTES


def test_a_real_report_survives_the_validator_unchanged():
    """End-to-end shape agreement without a live box: build a real PreflightReport,
    take its to_upstream_wire(), and assert the server keeps it byte-for-byte. If the
    two shapes ever drift, this fails before an operator sees a blank health cell."""
    from aizu.server import _validate_preflight_summary
    from aizu.worker import preflight as pf

    report = pf.PreflightReport(
        checks=(
            pf.CheckResult(pf.CHECK_STATE_DIR, "t", pf.SEVERITY_FATAL, pf.STATUS_PASS),
            pf.CheckResult(pf.CHECK_CAPABILITIES, "t", pf.SEVERITY_FATAL, pf.STATUS_FAIL,
                           "neither var is set", "remedy text"),
            pf.CheckResult("login.instagram", "t", pf.SEVERITY_WARN, pf.STATUS_UNKNOWN,
                           "unreadable", "remedy text"),
            pf.CheckResult(pf.CHECK_CDP_ATTACHABLE, "t", pf.SEVERITY_FATAL,
                           pf.STATUS_SKIP, "skipped"),
        ),
        ran_at=1786800000.12, duration_ms=8421)
    wire = report.to_upstream_wire()
    assert _validate_preflight_summary(wire) == wire
    # skip/pass rows never ride the wire; the two real problems do.
    assert [r["id"] for r in wire["failed"]] == ["capabilities", "login.instagram"]
    # remedy/title stay client-side copy.
    assert all(set(r) == {"id", "severity", "status", "detail"} for r in wire["failed"])


# ----- dispatch ordering -----------------------------------------------------

def test_worker_routes_bypass_cookie_gate(srv, monkeypatch):
    # A worker request with NO cookie still reaches the bearer handler (proves the
    # worker block is matched before the session gate): garbage bearer → 401 from
    # the worker handler, NOT a generic "authentication required".
    code, resp, _ = _post(srv["base"], "/api/worker/heartbeat", {}, bearer="garbage")
    assert code == 401
    assert resp["error"] == "invalid or revoked worker token"


def test_a_store_failure_answers_503_not_401_on_the_worker_plane(srv, monkeypatch):
    """B10 blast-radius guard, server side. `_current_worker` fails CLOSED on ANY store
    error, and every worker route answered that with the same 401 a revoked token gets.
    A bridge restarted before its DB volume mounted therefore told an entire fleet of
    perfectly valid tokens "you are revoked" — and the boxes acted on it. A server fault
    must look like a server fault: 503, which the sidecar backs off on."""
    import aizu.server as server_mod

    real_store = server_mod.Store

    class _ExplodingStore:
        def __init__(self, *a, **k):
            raise sqlite3.OperationalError("unable to open database file")

    monkeypatch.setattr(server_mod, "Store", _ExplodingStore)
    try:
        code, resp, _ = _post(srv["base"], "/api/worker/heartbeat", {},
                              bearer="a-perfectly-valid-token")
    finally:
        monkeypatch.setattr(server_mod, "Store", real_store)

    assert code == 503, resp
    assert "temporarily unavailable" in resp["error"]


def test_fleet_does_not_require_bearer(srv, monkeypatch):
    # The fleet route is admin-session gated, NOT bearer — an admin cookie alone
    # reaches a 200 with no Authorization header present.
    cookie = admin_cookie(srv["base"], srv["db"], email="fleet-d@x.io")
    code, _ = _get(srv["base"], "/api/admin/fleet", cookie=cookie)
    assert code == 200
