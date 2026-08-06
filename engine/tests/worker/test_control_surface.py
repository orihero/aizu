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
                                    "focusWarmedChrome"])
def test_validate_accepts_each_action(action):
    clean, err = validate_command({"action": action})
    assert err is None and clean["action"] == action


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


def test_invalid_command_is_rejected(live_surface):
    port, source = live_surface
    status, data = _req(port, "POST", "/command", body={"action": "explode"})
    assert status == 400 and data["ok"] is False
    assert source.calls == []


def test_unknown_endpoint_404(live_surface):
    port, _ = live_surface
    status, _ = _req(port, "GET", "/nope")
    assert status == 404
