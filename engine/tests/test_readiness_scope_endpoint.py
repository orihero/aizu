"""`GET /api/agent/readiness?campaign=<id>` — the platform narrowing, ON THE WIRE (B4).

`readiness.fleet_readiness` grew a `platforms` filter, and for a while NOTHING passed
it: the only production caller was `server._readiness_snapshot`, which called it with
the worker list alone. The filter worked perfectly in unit tests and was inert in
production — a tenant whose only online box advertises youtube was told ready:true for
an instagram run, then watched the job sit unleased. That is this repo's third
inert-on-the-wire defect (B4), so every test in this file goes through the SERVED HTTP
endpoint. A test that calls `fleet_readiness` directly cannot catch the bug.

The local CDP probe is injected and asserted UNTOUCHED: on the distributed backend the
control plane has no browser, and consulting one would make the answer a lie about a
different machine.
"""
from __future__ import annotations

import json
import os
import shutil
import tempfile
import threading
import urllib.error
import urllib.request
from pathlib import Path

import pytest

from aizu.core.store import EXECUTION_DISTRIBUTED, Store
from aizu.server import serve

ROOT = Path(__file__).resolve().parents[1]
CONFIG = ROOT / "config"

_LANDING_HTML = "<!doctype html><html><body><div id='landing'></div></body></html>"
_APP_HTML = "<!doctype html><html><body><div id='root'></div></body></html>"

_LOCAL_PROBE_CALLS: list[dict] = []


def _probe(cdp_url: str, **kwargs) -> dict:
    _LOCAL_PROBE_CALLS.append({"cdpUrl": cdp_url, **kwargs})
    return {"ready": True, "cdp": "ok", "instagram": "logged_in",
            "checkedAt": 0.0, "cdpUrl": cdp_url, "detail": None}


@pytest.fixture(scope="module")
def panel():
    fd, db_path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    panel_dir = tempfile.mkdtemp(suffix="-spa")
    (Path(panel_dir) / "index.html").write_text(_LANDING_HTML, encoding="utf-8")
    app_dir = Path(panel_dir) / "app"
    app_dir.mkdir()
    (app_dir / "index.html").write_text(_APP_HTML, encoding="utf-8")
    httpd = serve(db_path, panel_dir, str(CONFIG), port=0, readiness_probe=_probe)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{httpd.server_address[1]}"
    ctx = {"base": base, "db": db_path}
    ctx["cookie"] = _signup_cookie(base, "scope-tester@aizu.test", "test-password-123")
    # v27: campaign creation is plan-limited (free = 1) and this fixture needs two
    # platforms under ONE tenant to prove the narrowing. The second tenant below
    # creates a single campaign and stays on Free.
    store = Store(db_path)
    try:
        org_id = int(store._conn.execute(
            "SELECT id FROM organizations ORDER BY id LIMIT 1").fetchone()[0])
        store.upsert_subscription(org_id, last_event_ts=1.0, tier="pro",
                                  status="active")
    finally:
        store.close()
    ctx["ig"] = _make_campaign(ctx, "ig-scope", "instagram")
    ctx["yt"] = _make_campaign(ctx, "yt-scope", "youtube")
    # A SECOND tenant, for the ownership check: its campaign id must never narrow (or
    # even confirm) anything for the first tenant.
    other = dict(ctx)
    other["cookie"] = _signup_cookie(base, "other-tenant@aizu.test", "test-password-123")
    ctx["foreign_ig"] = _make_campaign(other, "ig-foreign", "instagram")
    # Distributed: the cloud has no browser of its own, so the fleet IS the verdict.
    store = Store(db_path)
    try:
        store.set_execution_backend(EXECUTION_DISTRIBUTED)
        # One online box that can only ever run youtube jobs.
        store.register_worker(worker_id="w-youtube-only", token="tok-youtube-only",
                              display_name="YT-BOX", max_sessions=1,
                              capabilities=[[None, "youtube", None]])
    finally:
        store.close()
    yield ctx
    httpd.shutdown()
    os.unlink(db_path)
    shutil.rmtree(panel_dir, ignore_errors=True)


@pytest.fixture(autouse=True)
def _reset_probe():
    _LOCAL_PROBE_CALLS.clear()
    yield


def _signup_cookie(base: str, email: str, password: str) -> str:
    req = urllib.request.Request(
        base + "/api/auth/signup",
        data=json.dumps({"email": email, "password": password,
                         "companyName": "Scope Co"}).encode(),
        headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req) as resp:
        return resp.headers["Set-Cookie"].split(";", 1)[0]


def _get(panel, path: str) -> tuple[int, dict]:
    req = urllib.request.Request(panel["base"] + path)
    req.add_header("Cookie", panel["cookie"])
    try:
        with urllib.request.urlopen(req) as resp:
            return resp.status, json.loads(resp.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read())


def _make_campaign(panel, campaign_id: str, platform: str) -> str:
    """Create a campaign and return the id the bridge ALLOCATED (a create lands in the
    caller's own org key namespace, not the requested slug)."""
    req = urllib.request.Request(
        panel["base"] + "/api/campaign",
        data=json.dumps({
            "campaignId": campaign_id, "displayName": campaign_id, "status": "live",
            "brief": {"platform": platform, "threshold": 0.8,
                      "relevanceDef": "saas product", "matchDef": "buyer intent",
                      "extractDef": "- phone", "languageMix": ["en"],
                      "seedChannels": ["UC_x"] if platform == "youtube" else []},
        }).encode(),
        headers={"Content-Type": "application/json", "Cookie": panel["cookie"]},
        method="POST")
    with urllib.request.urlopen(req) as resp:
        return json.loads(resp.read())["data"]["campaign_id"]


def test_unscoped_answer_still_counts_the_whole_fleet(panel):
    """The baseline every other test is measured against: with no `campaign` the box
    counts, exactly as the global banner (which has no campaign in hand) expects."""
    code, body = _get(panel, "/api/agent/readiness")
    assert code == 200 and body["backend"] == EXECUTION_DISTRIBUTED
    assert body["ready"] is True
    assert _LOCAL_PROBE_CALLS == []


def test_a_campaign_scoped_request_narrows_to_that_campaigns_platform(panel):
    """The defect: a youtube-only fleet must NOT read as ready for an instagram
    campaign. The detail names the scope so the operator sees which platform is
    missing, not just "not ready"."""
    code, body = _get(panel, f"/api/agent/readiness?campaign={panel['ig']}")
    assert code == 200
    assert body["ready"] is False
    assert body["cdp"] == "unreachable"
    assert "for instagram" in body["detail"]
    assert _LOCAL_PROBE_CALLS == []


def test_a_campaign_the_fleet_can_actually_run_stays_ready(panel):
    """The other half of the narrowing — it must not simply darken every answer."""
    code, body = _get(panel, f"/api/agent/readiness?campaign={panel['yt']}")
    assert code == 200 and body["ready"] is True
    assert "for youtube" in body["detail"]


def test_refresh_and_campaign_compose(panel):
    """Both query params at once — `refresh=1` must not swallow the scope."""
    code, body = _get(panel, f"/api/agent/readiness?refresh=1&campaign={panel['ig']}")
    assert code == 200 and body["ready"] is False and "for instagram" in body["detail"]


def test_another_tenants_campaign_id_does_not_scope_anything(panel):
    """A campaign id is org-scoped data. Answering "that one is instagram" for a
    foreign id would leak across the tenant boundary, so ownership is checked first and
    a miss degrades to the unscoped answer."""
    code, body = _get(panel, f"/api/agent/readiness?campaign={panel['foreign_ig']}")
    assert code == 200 and body["ready"] is True
    assert "for instagram" not in (body["detail"] or "")


def test_an_unknown_campaign_id_loses_the_scope_not_the_verdict(panel):
    """This endpoint backs a banner polled every 60s: a stale id in a URL must never be
    the reason an operator gets no answer at all."""
    for path in ("/api/agent/readiness?campaign=no-such-campaign",
                 "/api/agent/readiness?campaign=",
                 "/api/agent/readiness?campaign=%20"):
        code, body = _get(panel, path)
        assert code == 200 and body["ready"] is True, path
