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
import subprocess
import sys
import tempfile
import threading
import time
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
    # v27: campaign creation is plan-limited (free = 1) and this fixture needs two
    # (one CDP platform, one API-only) to tell the gated path from the ungated one.
    # The cap itself is covered by tests/test_run_redaction_server.py.
    store = Store(db_path)
    try:
        org_id = int(store._conn.execute(
            "SELECT id FROM organizations ORDER BY id LIMIT 1").fetchone()[0])
        store.upsert_subscription(org_id, last_event_ts=1.0, tier="pro",
                                  status="active")
    finally:
        store.close()
    ctx["ig"] = _make_campaign(ctx, "ig-gate", "instagram")
    ctx["yt"] = _make_campaign(ctx, "yt-gate", "youtube")
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


def _make_campaign(panel, campaign_id: str, platform: str) -> str:
    """Create a campaign and return the id the bridge ALLOCATED for it — a create
    lands in the caller's own org key namespace, which is not the requested slug."""
    code, resp = _post(panel, "/api/campaign", {
        "campaignId": campaign_id, "displayName": campaign_id, "status": "live",
        "brief": {"platform": platform, "threshold": 0.8,
                  "relevanceDef": "saas product", "matchDef": "buyer intent",
                  "extractDef": "- phone", "languageMix": ["en"],
                  "seedChannels": ["UC_x"] if platform == "youtube" else []},
    })
    assert code == 200
    return resp["data"]["campaign_id"]


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
    code, body = _post(panel, "/api/run", {"campaignId": panel["ig"], "mode": "live"})
    assert code == 409
    assert body["error"] == "agent_not_ready"
    assert body["detail"] == _NOT_READY["detail"]
    assert body["readiness"]["cdp"] == "unreachable"


def test_live_instagram_run_blocked_when_logged_out(panel):
    _PROBE["snapshot"] = dict(_LOGGED_OUT)
    code, body = _post(panel, "/api/run", {"campaignId": panel["ig"], "mode": "live"})
    assert code == 409 and body["error"] == "agent_not_ready"


def test_live_run_accepted_when_ready(panel):
    code, body = _post(panel, "/api/run", {"campaignId": panel["ig"], "mode": "live"})
    assert code == 202 and body["data"]["accepted"] is True


def test_dry_run_is_never_gated(panel):
    """A dry run walks a fake feed — no browser, nothing to be ready."""
    _PROBE["snapshot"] = dict(_NOT_READY)
    code, body = _post(panel, "/api/run", {"campaignId": panel["ig"], "mode": "dry"})
    assert code == 202 and body["data"]["mode"] == "dry"
    assert _PROBE["calls"] == []


def test_api_only_campaign_is_not_gated(panel):
    """YouTube runs against an API, not the shared browser — a down Chrome must not
    block it."""
    _PROBE["snapshot"] = dict(_NOT_READY)
    code, body = _post(panel, "/api/run", {"campaignId": panel["yt"], "mode": "live"})
    assert code == 202 and body["data"]["accepted"] is True
    assert _PROBE["calls"] == []


# ----- readiness.fleet_readiness -----

def _worker(**over) -> dict:
    """A fleet row in the shape `store.list_workers` emits. Default is the HEALTHY
    box — every test below breaks exactly one thing off this baseline, so what a test
    is actually about is the keyword it passes."""
    row = {"status": "online", "revokedAt": None,
           "capabilities": [[None, "instagram", None]], "preflight": None}
    row.update(over)
    return row


def test_fleet_readiness_ready_only_with_an_online_worker():
    assert readiness.fleet_readiness([], now=1.0)["ready"] is False
    assert readiness.fleet_readiness([_worker()], now=1.0)["ready"] is True
    for status in ("stale", "offline"):
        assert readiness.fleet_readiness(
            [_worker(status=status)], now=1.0)["ready"] is False


def test_fleet_readiness_is_not_ready_for_a_capability_less_worker():
    """Ledger F9.2, and the reason this test used to assert the opposite. A box that
    registered with `capabilities: []` (neither AIZU_WORKER_PLATFORMS nor
    AIZU_WORKER_CAPABILITIES set) can NEVER be leased to — `store._job_capability_covers`
    matches nothing — so counting it flipped the tenant's banner from an accurate
    ready:false to a false ready:true. Presence answers "is a PC switched on", not
    "can a run start"."""
    for caps in ([], None):
        snapshot = readiness.fleet_readiness([_worker(capabilities=caps)], now=1.0)
        assert snapshot["ready"] is False
        assert "none advertises a platform" in snapshot["detail"]
        # The remedy has to be actionable on the BOX, not in the panel — nobody can
        # SSH into these PCs (F12).
        assert "AIZU_WORKER_PLATFORMS" in snapshot["detail"]


def test_fleet_readiness_skips_capability_rows_the_dispatcher_could_never_match():
    """Same skip rules as `store._job_capability_covers`: a malformed row or an
    unsupported platform name must not count here either, or the banner promises work
    the lease scan can never place."""
    for caps in ([["instagram"]], [[None, "instagram"]], [[None, "myspace", None]],
                 ["instagram"], [None]):
        assert readiness.fleet_readiness(
            [_worker(capabilities=caps)], now=1.0)["ready"] is False


def test_fleet_readiness_narrows_to_the_platforms_the_caller_asked_for():
    fleet = [_worker(capabilities=[[None, "youtube", None]])]
    assert readiness.fleet_readiness(fleet, platforms=["youtube"], now=1.0)["ready"] is True
    narrowed = readiness.fleet_readiness(fleet, platforms=["instagram"], now=1.0)
    assert narrowed["ready"] is False and "for instagram" in narrowed["detail"]
    # No narrowing at all still counts it — the unfiltered banner asks "can ANY run
    # start", not "can an instagram run start".
    assert readiness.fleet_readiness(fleet, now=1.0)["ready"] is True


def test_fleet_readiness_ignores_revoked_workers():
    assert readiness.fleet_readiness([_worker(revokedAt=1700000000.0)],
                                     now=1.0)["ready"] is False


def test_fleet_readiness_does_not_count_a_worker_parked_by_its_own_preflight():
    """A blocking preflight means the box withheld its capabilities and parked its
    lease loop — it is online and useless, which is exactly the state the banner is
    supposed to catch. The detail must send the operator to the failing check rather
    than repeating the capability remedy, since the two have different fixes."""
    snapshot = readiness.fleet_readiness(
        [_worker(preflight={"ok": False, "blocking": True})], now=1.0)
    assert snapshot["ready"] is False
    assert "parked by" in snapshot["detail"] and "failing check" in snapshot["detail"]
    # A box that ALSO withheld its capabilities (the real wire shape of a parked box —
    # withholding is the actual enforcement) is still not ready; it just lands on the
    # capability branch, which is checked first.
    both = _worker(capabilities=[], preflight={"ok": False, "blocking": True})
    assert readiness.fleet_readiness([both], now=1.0)["ready"] is False


def test_fleet_readiness_treats_a_silent_or_clean_preflight_as_fine():
    """A pre-upgrade sidecar reports no preflight at all. Treating that silence as a
    failure would dark an entire existing fleet the moment this shipped."""
    for reported in (None, {}, {"ok": True, "blocking": False}, "nonsense", 0):
        assert readiness.fleet_readiness(
            [_worker(preflight=reported)], now=1.0)["ready"] is True


def test_fleet_readiness_never_guesses_the_login_state():
    """A box's Instagram session is only knowable on that box — the presence
    heartbeat drops chromeHealth, and the linkedin/x cookie signatures are
    unvalidated, so this stays 'unknown' even when ready."""
    assert readiness.fleet_readiness([_worker()], now=1.0)["instagram"] == "unknown"


def test_fleet_readiness_keeps_the_shared_contract_keys():
    """Same dict shape as check_readiness — the panel parses BOTH through one Zod
    schema, so a missing key here reads as a shape mismatch, not a fleet problem."""
    snapshot = readiness.fleet_readiness([_worker()], now=1.0)
    assert set(snapshot) == {"ready", "cdp", "instagram", "checkedAt", "cdpUrl",
                             "detail"}
    assert snapshot["checkedAt"] == 1.0


# ----- the CDP port (ledger F10) -----

def test_the_canonical_cdp_port_is_9333():
    """9333 is the resolution of F10, not a preference: warm_chrome.sh, engines.md §9
    and the desktop shell have always used it and 9222 survived only as a Python
    literal. Pinned here because `worker/config.py` and `cli.py` now derive their
    defaults from this constant — a silent flip back would re-open the ambiguity that
    had a sidecar probing a dead port on a box where Chrome was running."""
    assert readiness.DEFAULT_CDP_URL == "http://127.0.0.1:9333"


def test_alternate_cdp_url_maps_the_two_known_ports_both_ways():
    assert readiness.alternate_cdp_url("http://127.0.0.1:9333") == "http://127.0.0.1:9222"
    assert readiness.alternate_cdp_url("http://127.0.0.1:9222") == "http://127.0.0.1:9333"
    # Host, scheme and path survive — the sibling of a remote/attached Chrome must
    # still point at that same machine.
    assert readiness.alternate_cdp_url(
        "https://box-3.lan:9222/devtools") == "https://box-3.lan:9333/devtools"


def test_alternate_cdp_url_is_none_for_anything_else():
    """Two candidates, deliberately NOT a port scan — anything outside 9222/9333 has
    no sibling, so an operator who chose their own port is never second-guessed."""
    for url in ("http://127.0.0.1:9229", "http://127.0.0.1", "", "not a url",
                "http://127.0.0.1:99999"):
        assert readiness.alternate_cdp_url(url) is None


def test_alternate_cdp_url_keeps_an_ipv6_literal_bracketed():
    """Rebuilt from hostname:port rather than string-replacing the port, so an IPv6
    netloc cannot be silently mangled into an unparseable URL."""
    assert readiness.alternate_cdp_url("http://[::1]:9333") == "http://[::1]:9222"


def test_check_readiness_names_the_sibling_port_when_the_configured_one_is_dead():
    """The CLI/panel half of F10. A human reads this message, so — unlike the
    unattended worker, which adopts the sibling — we only DETECT and NAME the drift.
    Without it the operator is told to "start Chrome" on a box where Chrome is already
    running, which is how F10 cost an afternoon."""
    readiness.invalidate()
    probed = []

    def _cdp(url, timeout):
        probed.append(url)
        return "ok" if url.endswith(":9333") else "unreachable"

    snapshot = readiness.check_readiness("http://127.0.0.1:9222", probe_cdp_fn=_cdp,
                                         probe_login_fn=lambda *a: "logged_in")
    assert snapshot["ready"] is False and snapshot["cdp"] == "unreachable"
    assert "http://127.0.0.1:9333" in snapshot["detail"]
    assert "AIZU_CDP_URL=http://127.0.0.1:9333" in snapshot["detail"]
    assert probed == ["http://127.0.0.1:9222", "http://127.0.0.1:9333"]
    readiness.invalidate()


def test_check_readiness_does_not_probe_the_sibling_on_the_happy_path():
    """One extra short probe, and ONLY on the failure path — a healthy box must not
    pay for F10 detection on every poll."""
    readiness.invalidate()
    probed = []

    def _cdp(url, timeout):
        probed.append(url)
        return "ok"

    snapshot = readiness.check_readiness("http://127.0.0.1:9333", probe_cdp_fn=_cdp,
                                         probe_login_fn=lambda *a: "logged_in")
    assert snapshot["ready"] is True and snapshot["detail"] is None
    assert probed == ["http://127.0.0.1:9333"]
    readiness.invalidate()


def test_check_readiness_never_probes_login_over_a_dead_cdp():
    """Attaching Playwright to a refused endpoint just burns the 5s deadline; the
    verdict is already decided."""
    readiness.invalidate()
    logins = []
    snapshot = readiness.check_readiness(
        "http://127.0.0.1:9229",
        probe_cdp_fn=lambda url, timeout: "unreachable",
        probe_login_fn=lambda *a: logins.append(a) or "logged_in")
    assert logins == [] and snapshot["instagram"] == "unknown"
    readiness.invalidate()


# ----- _classify_login_state: the per-platform login signatures -----

def _cookie(name: str, domain: str, expires: float = -1) -> dict:
    return {"name": name, "domain": domain, "expires": expires}


def test_every_cdp_platform_has_a_login_signature():
    """The wizard's sign-in step and the worker preflight both emit one
    `login.<platform>` row per advertised CDP platform. A platform in CDP_PLATFORMS
    with no signature here classifies as 'unknown' forever — a silently missing check
    dressed as a passing one."""
    from aizu.core.config import CDP_PLATFORMS
    assert set(readiness.PLATFORM_LOGIN_SIGNATURES) == set(CDP_PLATFORMS)


@pytest.mark.parametrize("platform,cookie,domain", [
    ("instagram", "sessionid", ".instagram.com"),
    ("linkedin", "li_at", ".www.linkedin.com"),
    ("x", "auth_token", ".x.com"),
    ("x", "auth_token", ".twitter.com"),   # the pre-rename domain is still issued
])
def test_a_session_cookie_on_the_platforms_own_domain_means_logged_in(
        platform, cookie, domain):
    assert readiness._classify_login_state(
        [], [_cookie(cookie, domain)], platform=platform) == "logged_in"


def test_a_session_cookie_on_a_foreign_domain_does_not_count():
    """The warmed browser holds every platform's jar at once. Matching on the cookie
    NAME alone would let any site that happens to set `sessionid` declare Instagram
    signed in."""
    assert readiness._classify_login_state(
        [], [_cookie("sessionid", ".example.com")], platform="instagram") == "logged_out"


def test_no_cookie_at_all_is_logged_out_not_unknown():
    """A blank Chrome is the exact box this work exists to catch — it attaches fine and
    passes every check above the login one."""
    assert readiness._classify_login_state([], [], platform="instagram") == "logged_out"


def test_an_expired_session_cookie_is_logged_out():
    assert readiness._classify_login_state(
        [], [_cookie("sessionid", ".instagram.com", expires=100.0)],
        platform="instagram", now=200.0) == "logged_out"


@pytest.mark.parametrize("expires", [-1, None])
def test_a_session_cookie_with_no_explicit_expiry_is_not_expired(expires):
    """Playwright reports -1 for a session cookie. Reading that as a past timestamp
    would mark every genuinely-signed-in browser logged out — a permanent false red on
    a healthy box, which is worse than no check."""
    assert readiness._classify_login_state(
        [], [_cookie("sessionid", ".instagram.com", expires=expires)],
        platform="instagram", now=1e12) == "logged_in"


def test_a_login_wall_tab_beats_a_live_cookie():
    """The wall itself is the stronger signal (mirrors core/cdp.py's own
    _login_wall_reason): a cookie can be present and already rejected server-side."""
    assert readiness._classify_login_state(
        ["https://www.instagram.com/accounts/login/"],
        [_cookie("sessionid", ".instagram.com")], platform="instagram") == "logged_out"
    assert readiness._classify_login_state(
        ["https://www.instagram.com/challenge/xyz"],
        [_cookie("sessionid", ".instagram.com")], platform="instagram") == "logged_out"


def test_a_wall_url_only_counts_on_that_platforms_own_domain():
    """The cross-platform guard. "/login" is in LinkedIn's AND X's hint lists, so
    without requiring the URL to carry the platform's own domain an open Instagram
    login tab in the shared browser would mark X logged out — a false red on a box
    that is signed in, in the one browser that holds all three at once."""
    tabs = ["https://www.instagram.com/accounts/login/",
            "https://www.linkedin.com/login"]
    assert readiness._classify_login_state(
        tabs, [_cookie("auth_token", ".x.com")], platform="x") == "logged_in"
    # ...and the tab that DOES carry the platform's domain still bites.
    assert readiness._classify_login_state(
        tabs, [_cookie("li_at", ".linkedin.com")], platform="linkedin") == "logged_out"


def test_an_unsigned_platform_is_unknown_never_a_verdict():
    """youtube/telegram/reddit run against an API and have no browser session —
    claiming 'logged_out' for them would invent a failure."""
    for platform in ("youtube", "telegram", "reddit", "", None):
        assert readiness._classify_login_state(
            [], [_cookie("sessionid", ".instagram.com")],
            platform=platform) == "unknown"


def test_classify_tolerates_junk_tabs_and_cookies():
    """Tab URLs and cookies come off a live browser; a None url or a cookie dict
    missing a key must not raise inside a probe whose whole contract is 'never
    raises'."""
    assert readiness._classify_login_state(
        [None, ""], [{}, {"name": "sessionid"}, _cookie("sessionid", ".instagram.com")],
        platform="instagram") == "logged_in"


# ----- probe_browser: ONE attach, N platforms -----

def test_probe_browser_classifies_every_platform_off_a_single_attach():
    """This codebase allows exactly one CDP connection; checking three platforms must
    cost one attach, not three."""
    reads = []

    def _read():
        reads.append(1)
        return (["https://www.linkedin.com/login"],
                [_cookie("sessionid", ".instagram.com"),
                 _cookie("li_at", ".linkedin.com")])

    probe = readiness.probe_browser("http://127.0.0.1:9333",
                                    ("instagram", "linkedin", "x"), read_state=_read)
    assert reads == [1]
    assert probe.attached is True and probe.error is None
    assert probe.logins == {"instagram": "logged_in", "linkedin": "logged_out",
                            "x": "logged_out"}


def test_probe_browser_reports_a_refused_attach_without_raising():
    """Ledger B6/D3 — the degraded-Chrome case an HTTP 200 on /json/version cannot
    see. `attached` is the signal; the caller (worker preflight) decides it is fatal."""
    def _read():
        raise ConnectionRefusedError("nope")

    probe = readiness.probe_browser("http://127.0.0.1:9333", ("instagram",),
                                    read_state=_read)
    assert probe.attached is False
    assert probe.logins == {"instagram": "unknown"}
    # An exception TYPE NAME only: this rides the wire to the fleet console, and a
    # message can carry a path or a URL with credentials in it.
    assert probe.error == "ConnectionRefusedError"
    assert "nope" not in (probe.error or "")


def test_probe_browser_bounds_a_wedged_attach():
    """The TASK A failure mode: connect_over_cdp/cookies() can hang with no exception
    ever raised, so the deadline is call_bounded's, not Playwright's."""
    import time as _time

    def _read():
        _time.sleep(5.0)
        return ([], [])

    started = _time.time()
    probe = readiness.probe_browser("http://127.0.0.1:9333", ("instagram",),
                                    timeout=0.2, read_state=_read)
    assert _time.time() - started < 3.0
    assert probe.attached is False and probe.error == "ReadinessTimeout"
    assert probe.logins == {"instagram": "unknown"}


def test_probe_browser_with_no_platforms_still_reports_attachability():
    """`cdp_attachable` is checked on its own — a box may advertise no CDP platform and
    still want to know whether the browser it launched is usable."""
    probe = readiness.probe_browser("http://127.0.0.1:9333", (),
                                    read_state=lambda: ([], []))
    assert probe.attached is True and probe.logins == {}


def test_probe_instagram_login_facade_is_unchanged():
    """server.py / cli.py / check_readiness call this by its old signature and are
    owned elsewhere — it must stay a byte-compatible one-platform call into
    probe_browser."""
    assert readiness.probe_instagram_login(
        "http://127.0.0.1:9333",
        read_state=lambda: ([], [_cookie("sessionid", ".instagram.com")])) == "logged_in"

    def _boom():
        raise RuntimeError("wedged")

    assert readiness.probe_instagram_login("http://127.0.0.1:9333",
                                           read_state=_boom) == "unknown"


# ----- open_login_tab: the wizard's sign-in handoff -----

def test_open_login_tab_defaults_to_instagram_for_its_existing_callers():
    """`platform` is keyword-only with the old default so POST /api/agent/launch-login
    and the CLI stay byte-identical."""
    seen = []
    assert readiness.open_login_tab(
        "http://127.0.0.1:9333", opener=lambda: seen.append(1) or True) is True
    assert seen == [1]


def test_open_login_tab_never_raises():
    """False just means "could not open it" — callers degrade, they do not treat it as
    fatal, so a wedged or refused browser must not propagate."""
    def _boom():
        raise RuntimeError("no browser")

    assert readiness.open_login_tab("http://127.0.0.1:9333", opener=_boom) is False


def test_open_login_tab_refuses_a_platform_it_has_no_signature_for():
    """Reached without any Playwright/network: the default opener bails on the
    signature lookup. The control surface whitelists `platform` against CDP_PLATFORMS,
    so this is the second line of defence, not the first."""
    assert readiness.open_login_tab("http://127.0.0.1:9333", timeout=0.2,
                                    platform="myspace") is False


# ----- the seams other owners call POSITIONALLY -----

def test_the_probe_seams_keep_their_positional_signatures():
    """`worker/preflight.py`'s `_default_cdp_probe`/`_default_browser_probe` call these
    positionally — `probe_cdp(url, timeout)` and `probe_browser(url, platforms,
    timeout)` — and that file is owned elsewhere. Reordering or keyword-only-ing a
    parameter here would break the worker preflight silently, since preflight wraps
    every probe in a `_guarded` that turns an exception into a WARN."""
    import inspect
    assert [p.name for p in
            inspect.signature(readiness.probe_cdp).parameters.values()][:2] == \
        ["cdp_url", "timeout"]
    browser = list(inspect.signature(readiness.probe_browser).parameters.values())
    assert [p.name for p in browser[:3]] == ["cdp_url", "platforms", "timeout"]
    assert all(p.kind is not inspect.Parameter.KEYWORD_ONLY for p in browser[:3])


def test_default_cdp_url_prefers_the_environment_then_9333():
    """One resolution shared by the bridge, the CLI and the worker, so "which port"
    has exactly one answer per process."""
    previous = os.environ.get("AIZU_CDP_URL")
    try:
        os.environ.pop("AIZU_CDP_URL", None)
        assert readiness.default_cdp_url() == readiness.DEFAULT_CDP_URL
        os.environ["AIZU_CDP_URL"] = "http://10.0.0.4:9222"
        assert readiness.default_cdp_url() == "http://10.0.0.4:9222"
    finally:
        os.environ.pop("AIZU_CDP_URL", None)
        if previous is not None:
            os.environ["AIZU_CDP_URL"] = previous


# ----- check_readiness caching and the single-browser invariant -----

def test_check_readiness_reuses_the_last_known_state_while_a_run_is_active():
    """A live run owns the ONE CDP connection this architecture allows. Attaching a
    second Playwright client mid-run risks exactly the CDP hiccup TASK A hardened
    against, so an active run serves history instead of probing."""
    readiness.invalidate()
    readiness.check_readiness("http://127.0.0.1:9333",
                              probe_cdp_fn=lambda *a: "ok",
                              probe_login_fn=lambda *a: "logged_in")
    logins = []
    snapshot = readiness.check_readiness(
        "http://127.0.0.1:9333", run_active=lambda: True,
        probe_cdp_fn=lambda *a: "ok",
        probe_login_fn=lambda *a: logins.append(a) or "logged_out")
    assert logins == []                      # never a second attach
    assert snapshot["instagram"] == "logged_in"
    assert "a run is active" in snapshot["detail"]
    readiness.invalidate()


def test_check_readiness_mid_run_with_no_history_is_cdp_only():
    """No cached answer yet: the cheap HTTP probe is safe mid-run (no Playwright), the
    login state is honestly 'unknown' rather than guessed."""
    readiness.invalidate()
    logins = []
    snapshot = readiness.check_readiness(
        "http://127.0.0.1:9333", run_active=lambda: True,
        probe_cdp_fn=lambda *a: "ok",
        probe_login_fn=lambda *a: logins.append(a) or "logged_in")
    assert logins == []
    assert snapshot["cdp"] == "ok" and snapshot["instagram"] == "unknown"
    assert snapshot["ready"] is False
    readiness.invalidate()


def test_invalidate_drops_a_stale_ready_immediately():
    """Called the moment a mid-run login/checkpoint health flag is raised
    (engines/instagram/session.py) — a cached 'ready' must not survive up to
    CACHE_TTL_SEC past the moment the panel most needs a fresh answer."""
    readiness.invalidate()
    calls = []

    def _login(*a):
        calls.append(a)
        return "logged_in" if len(calls) == 1 else "logged_out"

    first = readiness.check_readiness("http://127.0.0.1:9333",
                                      probe_cdp_fn=lambda *a: "ok",
                                      probe_login_fn=_login)
    assert first["ready"] is True
    cached = readiness.check_readiness("http://127.0.0.1:9333",
                                       probe_cdp_fn=lambda *a: "ok",
                                       probe_login_fn=_login)
    assert cached["ready"] is True and len(calls) == 1     # served from cache
    readiness.invalidate()
    fresh = readiness.check_readiness("http://127.0.0.1:9333",
                                      probe_cdp_fn=lambda *a: "ok",
                                      probe_login_fn=_login)
    assert fresh["ready"] is False and len(calls) == 2
    readiness.invalidate()


def test_check_readiness_ready_is_exactly_cdp_ok_and_logged_in():
    for cdp_state, login_state, expected in (
            ("ok", "logged_in", True), ("ok", "logged_out", False),
            ("ok", "unknown", False), ("unreachable", "logged_in", False)):
        readiness.invalidate()
        snapshot = readiness.check_readiness(
            "http://127.0.0.1:9333", probe_cdp_fn=lambda *a, s=cdp_state: s,
            probe_login_fn=lambda *a, s=login_state: s)
        assert snapshot["ready"] is expected, (cdp_state, login_state)
    readiness.invalidate()


# ----- the "never raises" contract, exercised with browser-shaped junk -----

def test_classify_survives_an_unreadable_cookie_expiry():
    """The jar comes off a live browser. An unorderable `expires` must not raise out of
    a classifier whose callers (the panel banner, the worker preflight) both treat an
    exception as something other than what it is — a 500 on one, a silently DEMOTED
    fatal check on the other."""
    assert readiness._classify_login_state(
        [], [{"name": "sessionid", "domain": ".instagram.com", "expires": "soon"}],
        platform="instagram") == "logged_in"
    # A non-string tab URL is coerced, not concatenated into a TypeError.
    assert readiness._classify_login_state(
        [42, None], [_cookie("sessionid", ".instagram.com")],
        platform="instagram") == "logged_in"
    # A jar entry that is not a dict at all is skipped, not dereferenced.
    assert readiness._classify_login_state(
        [], ["sessionid", None, _cookie("sessionid", ".instagram.com")],
        platform="instagram") == "logged_in"


def test_probe_browser_stays_attached_when_only_classification_fails():
    """The attach failing and the classifier failing mean OPPOSITE things: the first is
    B6/D3 and is fatal, the second is cosmetic. Collapsing them would park a box whose
    Chrome is perfectly usable."""
    class _HostileJar(list):
        def __iter__(self):
            raise ValueError("unreadable cookie jar")

    probe = readiness.probe_browser(
        "http://127.0.0.1:9333", ("instagram", "x"),
        read_state=lambda: ([], _HostileJar()))
    assert probe.attached is True
    assert probe.logins == {"instagram": "unknown", "x": "unknown"}


def test_fleet_readiness_treats_an_empty_platform_filter_as_no_filter():
    """`platforms=[]` means "no platform in particular", not "narrow to nothing" —
    otherwise every fleet reads ready:false and the detail carries a dangling ' for '."""
    for empty in ([], ["", None]):
        snapshot = readiness.fleet_readiness([_worker()], platforms=empty, now=1.0)
        assert snapshot["ready"] is True
        assert " for " not in snapshot["detail"]


def test_probe_cdp_still_reaches_chrome_managers_http_pre_check():
    """`probe_cdp` borrows `worker.chrome_manager._default_http_probe` — a PRIVATE
    symbol in a file owned elsewhere. Pinned so a rename there fails here loudly
    instead of at runtime on a worker box, where the only diagnostic is a log line
    nobody reads."""
    from aizu.worker import chrome_manager
    assert callable(getattr(chrome_manager, "_default_http_probe", None))
    calls = []
    original = chrome_manager._default_http_probe
    chrome_manager._default_http_probe = lambda url, timeout: calls.append(
        (url, timeout)) or False
    try:
        assert readiness.probe_cdp("http://127.0.0.1:9333", 0.1) == "unreachable"
    finally:
        chrome_manager._default_http_probe = original
    assert calls == [("http://127.0.0.1:9333", 0.1)]


# ----- the browser reader's own budget and driver lifetime (F-1, F-7) -----

class _FakeDriverProc:
    """Stands in for playwright's `PipeTransport._proc`. Wraps a REAL child process so
    the hard stop's `os.kill` is genuinely exercised rather than recorded."""

    def __init__(self, popen):
        self.pid = popen.pid


class _FakeManager:
    """Mirrors `PlaywrightContextManager` closely enough for the hard stop: `_connection`
    exists only AFTER `start()`, exactly like the real one, so a driver spawn that never
    completes its handshake is modelled too."""

    def __init__(self, popen=None, *, connect=None, cookies=None, on_stop=None):
        self._popen = popen
        self._connect = connect
        self._cookies = cookies if cookies is not None else []
        self._on_stop = on_stop
        self._connection = None
        self.stopped = threading.Event()

    def start(self):
        if self._popen is not None:
            self._connection = type("C", (), {})()
            self._connection._transport = type("T", (), {})()
            self._connection._transport._proc = _FakeDriverProc(self._popen)
        return self

    # --- the sliver of the playwright surface read_browser_state touches ---
    @property
    def chromium(self):
        return self

    def connect_over_cdp(self, cdp_url, **kwargs):
        if self._connect is not None:
            return self._connect(cdp_url, **kwargs)
        ctx = type("Ctx", (), {"pages": [], "cookies": lambda _s: self._cookies})()
        return type("Browser", (), {"contexts": [ctx], "close": lambda _s: None})()

    def stop(self):
        self.stopped.set()
        if self._on_stop is not None:
            self._on_stop()


def _dead_within(popen, seconds: float) -> bool:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if popen.poll() is not None:
            return True
        time.sleep(0.05)
    return popen.poll() is not None


def test_the_attach_never_gets_the_callers_whole_budget():
    """F-1. `connect_over_cdp` used to be handed the SAME number call_bounded wrapped
    around the whole reader, so an attach landing at 4.9s of a 5.0s budget left 0.1s for
    `browser.contexts` + `ctx.pages` + `ctx.cookies()` — three more CDP round trips —
    and the OUTER deadline fired. A slow-but-alive Chrome then scored exactly like a
    dead one, which on a worker is a FATAL cdp_attachable and a parked box."""
    for budget in (5.0, readiness.ATTACH_FATAL_BUDGET_SEC):
        attach = readiness._attach_timeout_sec(budget)
        assert 0.0 < attach < budget
        # Something REAL has to be left for the reads, not a rounding crumb.
        assert budget - attach >= 1.0
    # A budget already spent cannot go negative — Playwright reads timeout=0 as "wait
    # forever", the exact opposite of what a zero budget means.
    assert readiness._attach_timeout_sec(0.0) == 0.0
    assert readiness._attach_timeout_sec(-3.0) == 0.0


def test_read_browser_state_splits_its_budget_between_attach_and_reads(monkeypatch):
    """The same invariant, pinned where it is actually applied — a caller reading the
    constants right and then passing `timeout * 1000` through anyway is the bug."""
    seen = {}

    def _connect(cdp_url, **kwargs):
        seen.update(kwargs)
        ctx = type("Ctx", (), {"pages": [], "cookies": lambda _s: []})()
        return type("Browser", (), {"contexts": [ctx], "close": lambda _s: None})()

    manager = _FakeManager(connect=_connect)
    monkeypatch.setattr(readiness, "sync_playwright", lambda: manager)
    readiness.read_browser_state("http://127.0.0.1:9333", 5.0)
    assert seen["timeout"] == pytest.approx(readiness._attach_timeout_sec(5.0) * 1000)
    assert seen["timeout"] < 5.0 * 1000


def test_the_fatal_grade_budget_is_not_stricter_than_the_job_it_gates():
    """F-1's root cause stated as an invariant: `worker/preflight.py` fails a FATAL
    check on a probe this budget bounds, while the REAL harvest run it stands in front
    of gives its own attach `nav_timeout_ms/1000 + 10s` on the owner thread and passes
    NO timeout to Playwright at all. A gate stricter than its job parks healthy boxes."""
    from aizu.core.cdp import CDPBaseConfig

    harvest_attach_budget = CDPBaseConfig.nav_timeout_ms / 1000.0 + 10.0
    assert readiness.ATTACH_FATAL_BUDGET_SEC >= harvest_attach_budget
    # And it must be strictly more generous than the INTERACTIVE default, whose whole
    # justification is that a human is waiting on an HTTP response and a wrong answer
    # only costs them a badge that refreshes.
    assert readiness.ATTACH_FATAL_BUDGET_SEC > readiness._LOGIN_PROBE_TIMEOUT_SEC


def test_read_browser_state_hard_stops_a_driver_its_caller_walked_away_from(monkeypatch):
    """F-7. `call_bounded` is a deadline, not a kill switch: on expiry the reader thread
    is abandoned mid-syscall, never reaches its own `finally: pw.stop()`, and keeps the
    node driver it spawned. `_park_for_preflight` re-probes every 30s for as long as the
    box stays parked — ~120 orphaned node processes an hour on a machine nobody can SSH
    into. The reader has to own a hard stop that outlives its caller."""
    driver = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    wedged = threading.Event()
    try:
        def _connect(cdp_url, **kwargs):
            wedged.set()
            threading.Event().wait()  # a CDP pipe gone quiet: no exception, ever

        manager = _FakeManager(driver, connect=_connect)
        monkeypatch.setattr(readiness, "sync_playwright", lambda: manager)
        monkeypatch.setattr(readiness, "_DRIVER_HARD_STOP_GRACE_SEC", 0.3)
        # A daemon thread nobody joins — precisely what call_bounded leaves behind.
        threading.Thread(target=readiness.read_browser_state,
                         args=("http://127.0.0.1:9333", 0.2), daemon=True).start()
        assert wedged.wait(5.0), "the reader should have reached the attach"
        assert _dead_within(driver, 5.0), (
            "the abandoned reader still owns its node driver — nothing else will ever "
            "release it, and the worker re-probes every 30s")
    finally:
        driver.kill()
        driver.wait()


def test_read_browser_state_does_not_hard_stop_a_read_that_finished(monkeypatch):
    """The other half, and the one that matters more: a false kill would take out a
    driver mid-read and manufacture the fatal it exists to prevent. The timer is
    cancelled on the normal path, and the reader's own `finished` latch means even a
    timer that already fired declines to signal a pid its owner is releasing."""
    driver = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    try:
        manager = _FakeManager(driver, cookies=[_cookie("sessionid", ".instagram.com")])
        monkeypatch.setattr(readiness, "sync_playwright", lambda: manager)
        monkeypatch.setattr(readiness, "_DRIVER_HARD_STOP_GRACE_SEC", 0.2)
        urls, cookies = readiness.read_browser_state("http://127.0.0.1:9333", 0.1)
        assert cookies and manager.stopped.is_set()
        assert not _dead_within(driver, 0.8), "a completed read must not kill anything"
    finally:
        driver.kill()
        driver.wait()


def test_a_teardown_failure_never_turns_a_good_read_into_a_false_fatal(monkeypatch):
    """`browser.close()` / `pw.stop()` throwing on the way OUT of a successful read used
    to propagate straight through probe_browser as attached=False — a FATAL
    cdp_attachable over a browser that had just answered every question we asked it."""
    def _connect(cdp_url, **kwargs):
        ctx = type("Ctx", (), {"pages": [], "cookies": lambda _s: [
            _cookie("sessionid", ".instagram.com")]})()

        def _close(_s):
            raise RuntimeError("close failed on the way out")

        return type("Browser", (), {"contexts": [ctx], "close": _close})()

    def _boom():
        raise RuntimeError("driver already gone")

    manager = _FakeManager(connect=_connect, on_stop=_boom)
    monkeypatch.setattr(readiness, "sync_playwright", lambda: manager)
    # This test is about the reader, so it must reach the reader on a box where the real
    # Playwright is not installed too (probe_browser short-circuits before it otherwise).
    monkeypatch.setattr(readiness, "PLAYWRIGHT_AVAILABLE", True)
    probe = readiness.probe_browser("http://127.0.0.1:9333", ("instagram",), 1.0)
    assert probe.attached is True
    assert probe.logins == {"instagram": "logged_in"}


# ----- fleet_readiness' platform narrowing (F-10, readiness half) -----

def test_fleet_readiness_narrowing_tolerates_how_the_two_sides_are_authored():
    """The narrowing comes from a campaign brief a human wrote; the capability comes
    from AIZU_WORKER_PLATFORMS on a box. Both arrive here as bare strings compared with
    `in`, and a missed match is a false ready:false that dark-banners a healthy fleet."""
    fleet = [_worker(capabilities=[[None, "youtube", None]])]
    for asked in (["YouTube"], [" youtube "], ("youtube",), {"youtube"}):
        assert readiness.fleet_readiness(fleet, platforms=asked, now=1.0)["ready"] is True
    narrowed = readiness.fleet_readiness(fleet, platforms=["  Instagram "], now=1.0)
    assert narrowed["ready"] is False
    # Normalised in the operator-facing copy too, not just in the comparison.
    assert "for instagram" in narrowed["detail"]


def test_fleet_readiness_does_not_normalise_a_workers_advertised_capability():
    """Deliberately asymmetric. `store._job_capability_covers` matches capabilities
    exactly, so quietly repairing a malformed one here would count a box the lease scan
    then refuses — the "banner promises work nothing can place" lie the capability
    filter exists to stop."""
    fleet = [_worker(capabilities=[[None, "Instagram", None]])]
    assert readiness.fleet_readiness(fleet, platforms=["instagram"], now=1.0)["ready"] is False
    assert readiness.fleet_readiness(fleet, now=1.0)["ready"] is False


def test_fleet_readiness_narrowing_survives_junk_without_darkening_the_fleet():
    """A platform list is caller-supplied data. An unstringable or blank element must
    drop out, not raise — this backs a banner polled every 60s."""
    fleet = [_worker(capabilities=[[None, "instagram", None]])]
    snapshot = readiness.fleet_readiness(fleet, platforms=[None, "", "instagram"], now=1.0)
    assert snapshot["ready"] is True
    assert "for instagram" in snapshot["detail"]


def test_fleet_readiness_scope_copy_is_stable_and_deduplicated():
    """The scope string rides into an operator-facing detail; duplicates and casing
    differences must not produce 'for instagram, Instagram'."""
    snapshot = readiness.fleet_readiness(
        [_worker(capabilities=[])], platforms=["instagram", "INSTAGRAM", "youtube"],
        now=1.0)
    assert snapshot["ready"] is False
    assert " for instagram, youtube" in snapshot["detail"]
    assert snapshot["detail"].count("instagram") == 1
