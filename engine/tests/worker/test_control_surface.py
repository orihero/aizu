"""Local control/status surface (control_state + control_surface + chrome_probe, D3)."""
from __future__ import annotations

import json
import threading
from http.client import HTTPConnection

import pytest

from aizu.worker.chrome_probe import cdp_status
from aizu.worker.control_state import (AccountHealth, ChromeStatusView,
                                            CurrentJobInfo, StatusSnapshot,
                                            status_to_wire, validate_command)
from aizu.worker.control_surface import (ControlSurfaceConfig,
                                              start_control_surface)


# ----- validate_command --------------------------------------------------------

@pytest.mark.parametrize("action", ["pause", "resume", "stopCurrentJob",
                                    "focusWarmedChrome", "runPreflight"])
def test_validate_accepts_each_zero_argument_action(action):
    clean, err = validate_command({"action": action})
    assert err is None and clean["action"] == action


def test_openLoginTab_requires_a_WHITELISTED_platform():
    """The one field on this surface with any reach: it is handed to a browser-driving
    seam, so it is whitelisted against CDP_PLATFORMS rather than merely type-checked."""
    clean, err = validate_command({"action": "openLoginTab", "platform": "instagram"})
    assert err is None and clean["platform"] == "instagram"
    assert validate_command({"action": "openLoginTab"})[0] is None          # missing
    assert validate_command({"action": "openLoginTab", "platform": "youtube"})[0] is None
    assert validate_command({"action": "openLoginTab", "platform": "../x"})[0] is None


def test_validate_rejects_unknown_and_non_dict():
    assert validate_command({"action": "nuke"})[0] is None
    assert validate_command("not-a-dict")[0] is None
    assert validate_command({"action": 5})[0] is None


def test_validate_caps_reason():
    clean, err = validate_command({"action": "pause", "reason": "x" * 9999})
    assert err is None and len(clean["reason"]) == 500


# ----- status_to_wire leak-safety ----------------------------------------------

def test_status_to_wire_shape_and_no_secret_leak():
    snap = StatusSnapshot(
        worker_id="w1",
        accounts=(AccountHealth(1, "instagram", "acme_ig", "busy"),),
        current_job=CurrentJobInfo("j1", "c1", "instagram", "running", "run-1",
                                   "/logs/run-run-1.log"),
        drain=False, halt=True, halt_reason="operator_stop", update_required=False,
        chrome=ChromeStatusView(True, "http://127.0.0.1:9333", "Chrome/140"),
        paused=True, generated_at=123.0)
    wire = status_to_wire(snap)
    assert wire["workerId"] == "w1"
    assert wire["currentJob"]["runId"] == "run-1"
    assert wire["controls"]["haltReason"] == "operator_stop"
    assert wire["controls"]["paused"] is True
    assert wire["chrome"]["connected"] is True
    blob = json.dumps(wire).lower()
    for banned in ("token", "secret", "soul", "password"):
        assert banned not in blob


def test_status_to_wire_null_job():
    snap = StatusSnapshot(worker_id="w1")
    assert status_to_wire(snap)["currentJob"] is None


def test_status_to_wire_carries_reenrolment_required():
    """B10: a revoked box must be VISIBLE to the desktop app — otherwise it reads as a
    plain idle worker that silently never leases again. Defaults False so an untouched
    snapshot (and the older desktop binary reading it) is unchanged."""
    assert status_to_wire(StatusSnapshot(worker_id="w1"))["controls"][
        "reenrolmentRequired"] is False
    revoked = StatusSnapshot(worker_id="w1", reenrolment_required=True)
    assert status_to_wire(revoked)["controls"]["reenrolmentRequired"] is True


def test_status_to_wire_carries_the_preflight_block_verbatim():
    """The ONLY channel the desktop shell has. It rides as an already-serialized dict so
    this pure read model stays import-free of preflight.py (which pulls in the
    readiness/Playwright seams)."""
    report = {"ok": False, "blocking": True, "enforced": True, "ranAt": 1.0,
              "durationMs": 8421,
              "checks": [{"id": "capabilities", "title": "Platforms this box advertises",
                          "severity": "fatal", "status": "fail",
                          "detail": "neither AIZU_WORKER_PLATFORMS nor "
                                    "AIZU_WORKER_CAPABILITIES is set",
                          "remedy": "Set AIZU_WORKER_PLATFORMS=all"}]}
    wire = status_to_wire(StatusSnapshot(worker_id="w1", preflight=report))
    assert wire["preflight"] == report


def test_status_to_wire_preflight_is_null_until_the_first_run_finishes():
    """null means 'checking…', NEVER 'healthy' — a box whose preflight has not finished
    has not been cleared of anything. Defaulting to None also keeps an older desktop
    binary reading this feed unchanged."""
    assert status_to_wire(StatusSnapshot(worker_id="w1"))["preflight"] is None


# ----- ControlSurfaceConfig loopback guard -------------------------------------

def test_config_rejects_non_loopback_host():
    with pytest.raises(ValueError):
        ControlSurfaceConfig(auth_token="t", bind_host="0.0.0.0")


def test_config_rejects_empty_token():
    with pytest.raises(ValueError):
        ControlSurfaceConfig(auth_token="", bind_host="127.0.0.1")


# ----- chrome_probe -------------------------------------------------------------

def test_cdp_status_connected_when_probe_returns_version():
    got = cdp_status("http://127.0.0.1:9333",
                     http_get=lambda u, t: {"Browser": "Chrome/140.0"})
    assert got.connected is True and got.browser_version == "Chrome/140.0"


def test_cdp_status_disconnected_when_probe_none_or_raises():
    assert cdp_status("http://127.0.0.1:9", http_get=lambda u, t: None).connected is False

    def boom(u, t):
        raise OSError("closed")
    assert cdp_status("http://127.0.0.1:9", http_get=boom).connected is False


# ----- HTTP round trip against a fake source -----------------------------------

class FakeSource:
    def __init__(self):
        self.calls = []
        self.done = threading.Event()   # the two DETACHED actions signal completion here

    def get_status(self):
        return StatusSnapshot(worker_id="wX", generated_at=1.0)

    def pause(self):
        self.calls.append("pause")

    def resume(self):
        self.calls.append("resume")

    def stop_current_job(self):
        self.calls.append("stop")
        return True

    def focus_warmed_chrome(self):
        self.calls.append("focus")
        return False

    def run_preflight(self):
        self.calls.append("preflight")
        self.done.set()
        return True

    def open_login_tab(self, platform):
        self.calls.append(f"login:{platform}")
        self.done.set()
        return True


@pytest.fixture
def live_surface():
    source = FakeSource()
    cfg = ControlSurfaceConfig(auth_token="secret-local", port=0)  # port 0 = ephemeral
    server = start_control_surface(cfg, source)
    port = server.server_address[1]
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    yield port, source
    server.shutdown()
    server.server_close()


def _req(port, method, path, *, token="secret-local", body=None):
    conn = HTTPConnection("127.0.0.1", port, timeout=5)
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    payload = None
    if body is not None:
        payload = json.dumps(body)
        headers["Content-Type"] = "application/json"
    conn.request(method, path, body=payload, headers=headers)
    resp = conn.getresponse()
    data = json.loads(resp.read().decode())
    conn.close()
    return resp.status, data


def test_status_endpoint_returns_snapshot(live_surface):
    port, _ = live_surface
    status, data = _req(port, "GET", "/status")
    assert status == 200 and data["ok"] is True
    assert data["data"]["workerId"] == "wX"


def test_missing_token_is_unauthorized(live_surface):
    port, _ = live_surface
    status, data = _req(port, "GET", "/status", token=None)
    assert status == 401 and "workerId" not in json.dumps(data)


def test_wrong_token_is_unauthorized(live_surface):
    port, _ = live_surface
    status, _ = _req(port, "GET", "/status", token="wrong")
    assert status == 401


def test_each_command_dispatches(live_surface):
    port, source = live_surface
    for action, expected in [("pause", "pause"), ("resume", "resume"),
                             ("stopCurrentJob", "stop"), ("focusWarmedChrome", "focus")]:
        status, data = _req(port, "POST", "/command", body={"action": action})
        assert status == 200 and data["ok"] is True
    assert source.calls == ["pause", "resume", "stop", "focus"]


@pytest.mark.parametrize("body,expected", [
    ({"action": "runPreflight"}, "preflight"),
    ({"action": "openLoginTab", "platform": "linkedin"}, "login:linkedin"),
])
def test_the_detached_commands_answer_ACCEPTED_and_still_reach_the_source(
        live_surface, body, expected):
    """Both do real I/O (a CDP probe, a Playwright attach) that comfortably outlives the
    desktop client's 3s timeout, so the contract is 'accepted', not 'completed' — answering
    inline would time the caller out and make a WORKING command look broken. The operator's
    feedback is the next 1500ms /status poll."""
    port, source = live_surface

    status, data = _req(port, "POST", "/command", body=body)

    assert status == 200 and data["data"]["accepted"] is True
    assert source.done.wait(5.0), "the detached thread never reached the source"
    assert source.calls == [expected]


def test_a_raising_detached_command_never_escapes_to_stderr(live_surface):
    """`_swallow` exists because threading's default excepthook writes to stderr, which a
    GUI operator never sees — the exact failure mode this whole preflight work ends."""
    port, source = live_surface

    def _boom():
        source.done.set()
        raise RuntimeError("probe exploded")

    source.run_preflight = _boom
    status, data = _req(port, "POST", "/command", body={"action": "runPreflight"})

    assert status == 200 and data["data"]["accepted"] is True
    assert source.done.wait(5.0)
    # ...and the surface is still serving afterwards.
    assert _req(port, "GET", "/status")[0] == 200


def test_invalid_command_is_rejected(live_surface):
    port, source = live_surface
    status, data = _req(port, "POST", "/command", body={"action": "explode"})
    assert status == 400 and data["ok"] is False
    assert source.calls == []


def test_unknown_endpoint_404(live_surface):
    port, _ = live_surface
    status, _ = _req(port, "GET", "/nope")
    assert status == 404
