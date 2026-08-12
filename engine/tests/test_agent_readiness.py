"""Agent readiness: the contract the panel's global banner polls and the gate
POST /api/run puts in front of a live run.

Both HTTP surfaces (`GET /api/agent/readiness`, `POST /api/agent/launch-login`) are
what `readiness.py` was written for — the panel has shipped a banner, Zod schemas and
a 409 `agent_not_ready` parser against them all along, so these tests pin the shape
the client already expects rather than inventing a new one.
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

from aizu import readiness, server
from aizu.core.store import EXECUTION_DISTRIBUTED, EXECUTION_IN_PROCESS, Store
from aizu.runner import RunManager
from aizu.server import serve

ROOT = Path(__file__).resolve().parents[1]
CONFIG = ROOT / "config"

_LANDING_HTML = "<!doctype html><html><body><div id='landing'></div></body></html>"
_APP_HTML = "<!doctype html><html><body><div id='root'></div></body></html>"

# The injected probe's answer, swapped per test. `calls` records every invocation so a
# test can assert the gate did NOT probe at all (an API-only campaign must not be
# gated on a browser it never touches).
_PROBE = {"snapshot": None, "calls": []}

_READY = {"ready": True, "cdp": "ok", "instagram": "logged_in",
          "checkedAt": 0.0, "cdpUrl": "http://127.0.0.1:9222", "detail": None}
_NOT_READY = {"ready": False, "cdp": "unreachable", "instagram": "unknown",
              "checkedAt": 0.0, "cdpUrl": "http://127.0.0.1:9222",
              "detail": "CDP not reachable at http://127.0.0.1:9222"}
_LOGGED_OUT = {"ready": False, "cdp": "ok", "instagram": "logged_out",
               "checkedAt": 0.0, "cdpUrl": "http://127.0.0.1:9222",
               "detail": "instagram session is logged_out"}


def _probe(cdp_url: str, **kwargs) -> dict:
    _PROBE["calls"].append({"cdpUrl": cdp_url, **kwargs})
    return dict(_PROBE["snapshot"] or _READY)


class _FakeProc:
    def __init__(self):
        self.pid = 4242
        self.returncode = None

    def wait(self) -> int:
        self.returncode = 0
        return 0

    def terminate(self) -> None:
        self.returncode = -15


class _FakeSpawner:
    """Injected into RunManager so an accepted run never spawns a real engine."""
    def __init__(self):
        self.calls = []

    def __call__(self, argv, cwd, env, log_path):
        self.calls.append(argv)
        return _FakeProc()


@pytest.fixture(scope="module")
def panel():
    fd, db_path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    panel_dir = tempfile.mkdtemp(suffix="-spa")
    (Path(panel_dir) / "index.html").write_text(_LANDING_HTML, encoding="utf-8")
    app_dir = Path(panel_dir) / "app"
    app_dir.mkdir()
    (app_dir / "index.html").write_text(_APP_HTML, encoding="utf-8")
    manager = RunManager(db_path=db_path, config_dir=str(CONFIG),
                         engine_root=panel_dir, log_dir=Path(panel_dir) / "run-logs",
                         spawner=_FakeSpawner(), python_exe="py")
    httpd = serve(db_path, panel_dir, str(CONFIG), port=0, run_manager=manager,
                  readiness_probe=_probe, login_opener=lambda: True)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{httpd.server_address[1]}"
    cookie = _signup_cookie(base, "readiness-tester@aizu.test", "test-password-123")
    ctx = {"base": base, "db": db_path, "cookie": cookie, "manager": manager}
    _make_campaign(ctx, "ig-gate", "instagram")
    _make_campaign(ctx, "yt-gate", "youtube")
    yield ctx
    httpd.shutdown()
    os.unlink(db_path)
    shutil.rmtree(panel_dir, ignore_errors=True)


@pytest.fixture(autouse=True)
def _reset_probe(panel):
    """Every test starts from a ready agent, the in-process backend and an idle
    runner — the module-scoped server is shared, so state must not leak between them."""
    _PROBE["snapshot"] = dict(_READY)
    _PROBE["calls"] = []
    store = Store(panel["db"])
    try:
        store.set_execution_backend(EXECUTION_IN_PROCESS)
    finally:
        store.close()
    yield
    _wait_run_idle(panel)


def _signup_cookie(base: str, email: str, password: str) -> str:
    req = urllib.request.Request(
        base + "/api/auth/signup",
        data=json.dumps({"email": email, "password": password,
                         "companyName": "Readiness Co"}).encode(),
        headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req) as resp:
        return resp.headers["Set-Cookie"].split(";", 1)[0]


def _get(panel, path: str, *, cookie: bool = True) -> tuple[int, dict]:
    req = urllib.request.Request(panel["base"] + path)
    if cookie:
        req.add_header("Cookie", panel["cookie"])
    try:
        with urllib.request.urlopen(req) as resp:
            return resp.status, json.loads(resp.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read())


def _post(panel, path: str, body: dict) -> tuple[int, dict]:
    req = urllib.request.Request(
        panel["base"] + path, data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json", "Cookie": panel["cookie"]},
        method="POST")
    try:
        with urllib.request.urlopen(req) as resp:
            return resp.status, json.loads(resp.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read())


def _make_campaign(panel, campaign_id: str, platform: str) -> None:
    code, _ = _post(panel, "/api/campaign", {
        "campaignId": campaign_id, "displayName": campaign_id, "status": "live",
        "brief": {"platform": platform, "threshold": 0.8,
                  "relevanceDef": "saas product", "matchDef": "buyer intent",
                  "extractDef": "- phone", "languageMix": ["en"],
                  "seedChannels": ["UC_x"] if platform == "youtube" else []},
    })
    assert code == 200


def _wait_run_idle(panel, timeout: float = 5.0) -> None:
    import time
    deadline = time.time() + timeout
    while time.time() < deadline and panel["manager"].is_active():
        time.sleep(0.05)


# ----- GET /api/agent/readiness -----

def test_readiness_requires_a_session(panel):
    code, body = _get(panel, "/api/agent/readiness", cookie=False)
    assert code == 401 and body["ok"] is False


def test_readiness_returns_the_raw_contract_dict(panel):
    """Raw, NOT the {ok,data,error} envelope — the panel parses this straight into
    agentReadinessSchema, so an envelope here reads as a shape mismatch."""
    code, body = _get(panel, "/api/agent/readiness")
    assert code == 200
    assert set(body) >= {"ready", "cdp", "instagram", "checkedAt", "cdpUrl", "detail"}
    assert body["ready"] is True and body["cdp"] == "ok"
    assert body["backend"] == EXECUTION_IN_PROCESS


def test_readiness_reports_a_down_agent(panel):
    _PROBE["snapshot"] = dict(_NOT_READY)
    code, body = _get(panel, "/api/agent/readiness")
    assert code == 200 and body["ready"] is False
    assert body["cdp"] == "unreachable" and "not reachable" in body["detail"]


def test_refresh_query_forces_a_live_probe(panel):
    _get(panel, "/api/agent/readiness")
    assert _PROBE["calls"][-1]["force_refresh"] is False
    _get(panel, "/api/agent/readiness?refresh=1")
    assert _PROBE["calls"][-1]["force_refresh"] is True


def test_distributed_backend_reads_the_fleet_not_local_chrome(panel):
    """With no browser on the control plane, worker presence is the real gate — an
    empty fleet is 'not ready' even though the injected local probe says ready."""
    store = Store(panel["db"])
    try:
        store.set_execution_backend(EXECUTION_DISTRIBUTED)
    finally:
        store.close()
    code, body = _get(panel, "/api/agent/readiness")
    assert code == 200 and body["backend"] == EXECUTION_DISTRIBUTED
    assert body["ready"] is False and "no worker is online" in body["detail"]
    # The local CDP probe is irrelevant in this mode and must not have been consulted.
    assert _PROBE["calls"] == []


# ----- POST /api/agent/launch-login -----

def test_launch_login_is_gated_on_fix_agent(panel):
    assert server._ROUTE_ACTIONS[server.AGENT_LAUNCH_LOGIN_PATH] == "fix_agent"


def test_launch_login_reports_launched_with_a_fresh_snapshot(panel):
    _PROBE["snapshot"] = dict(_LOGGED_OUT)
    code, body = _post(panel, "/api/agent/launch-login", {})
    assert code == 200 and body["launched"] is True
    assert body["readiness"]["instagram"] == "logged_out"
    # Re-probed after opening the tab, not echoing a pre-launch snapshot.
    assert all(call["force_refresh"] for call in _PROBE["calls"])


def test_launch_login_does_nothing_in_distributed_mode(panel):
    """The warmed Chrome lives on the worker PC; there is no browser here to open."""
    store = Store(panel["db"])
    try:
        store.set_execution_backend(EXECUTION_DISTRIBUTED)
    finally:
        store.close()
    code, body = _post(panel, "/api/agent/launch-login", {})
    assert code == 200 and body["launched"] is False
    assert body["readiness"]["backend"] == EXECUTION_DISTRIBUTED


# ----- the POST /api/run gate -----

def test_live_run_blocked_when_the_agent_is_not_ready(panel):
    _PROBE["snapshot"] = dict(_NOT_READY)
    code, body = _post(panel, "/api/run", {"campaignId": "ig-gate", "mode": "live"})
    assert code == 409
    assert body["error"] == "agent_not_ready"
    assert body["detail"] == _NOT_READY["detail"]
    assert body["readiness"]["cdp"] == "unreachable"


def test_live_instagram_run_blocked_when_logged_out(panel):
    _PROBE["snapshot"] = dict(_LOGGED_OUT)
    code, body = _post(panel, "/api/run", {"campaignId": "ig-gate", "mode": "live"})
    assert code == 409 and body["error"] == "agent_not_ready"


def test_live_run_accepted_when_ready(panel):
    code, body = _post(panel, "/api/run", {"campaignId": "ig-gate", "mode": "live"})
    assert code == 202 and body["data"]["accepted"] is True


def test_dry_run_is_never_gated(panel):
    """A dry run walks a fake feed — no browser, nothing to be ready."""
    _PROBE["snapshot"] = dict(_NOT_READY)
    code, body = _post(panel, "/api/run", {"campaignId": "ig-gate", "mode": "dry"})
    assert code == 202 and body["data"]["mode"] == "dry"
    assert _PROBE["calls"] == []


def test_api_only_campaign_is_not_gated(panel):
    """YouTube runs against an API, not the shared browser — a down Chrome must not
    block it."""
    _PROBE["snapshot"] = dict(_NOT_READY)
    code, body = _post(panel, "/api/run", {"campaignId": "yt-gate", "mode": "live"})
    assert code == 202 and body["data"]["accepted"] is True
    assert _PROBE["calls"] == []


# ----- readiness.fleet_readiness -----

def test_fleet_readiness_ready_only_with_an_online_worker():
    assert readiness.fleet_readiness([], now=1.0)["ready"] is False
    online = [{"status": "online", "revokedAt": None}]
    assert readiness.fleet_readiness(online, now=1.0)["ready"] is True
    for status in ("stale", "offline"):
        assert readiness.fleet_readiness(
            [{"status": status, "revokedAt": None}], now=1.0)["ready"] is False


def test_fleet_readiness_ignores_revoked_workers():
    revoked = [{"status": "online", "revokedAt": 1700000000.0}]
    assert readiness.fleet_readiness(revoked, now=1.0)["ready"] is False


def test_fleet_readiness_never_guesses_the_login_state():
    """A box's Instagram session is only knowable on that box — the presence
    heartbeat drops chromeHealth, so this stays 'unknown' even when ready."""
    online = [{"status": "online", "revokedAt": None}]
    assert readiness.fleet_readiness(online, now=1.0)["instagram"] == "unknown"
