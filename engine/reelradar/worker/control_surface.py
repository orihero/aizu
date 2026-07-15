"""Loopback-ONLY local control/status surface for the desktop shell (Phase 6, D3).

A tiny stdlib ``ThreadingHTTPServer`` bound to 127.0.0.1 exposing ``GET /status`` and
``POST /command``, mirroring ``reelradar/server.py``'s idioms (``{ok,data,error}`` envelope,
constant-time bearer check) WITHOUT importing that cloud-facing module (the worker package
must not drag in RBAC/billing/auth). It reads live state from a :class:`ControlSurfaceSource`
(the Sidecar, adapted) and pushes operator commands back — never touching secrets or leads.

This is the ONLY inbound listener the worker ever opens, and only when explicitly enabled
(``WorkerConfig.control_surface_enabled``); it is loopback-only (defense-in-depth: the bind
host is validated and can never be anything but 127.0.0.1/::1) + shared-token gated, so it
is not reachable from the LAN and is never the job channel.
"""
from __future__ import annotations

import hmac
import json
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Optional

from ..core.logsetup import get_logger
from . import DEFAULT_CONTROL_SURFACE_PORT
from .control_state import ControlSurfaceSource, status_to_wire, validate_command

log = get_logger("reelradar.worker.control_surface")

_LOOPBACK_HOSTS = ("127.0.0.1", "::1", "localhost")
_MAX_BODY_BYTES = 64 * 1024  # commands are tiny; cap to bound a hostile local caller


@dataclass(frozen=True)
class ControlSurfaceConfig:
    """Frozen. ``bind_host`` is validated loopback-only — never overridable to the LAN."""
    auth_token: str
    port: int = DEFAULT_CONTROL_SURFACE_PORT
    bind_host: str = "127.0.0.1"

    def __post_init__(self):
        if self.bind_host not in _LOOPBACK_HOSTS:
            raise ValueError(
                f"control surface bind_host must be loopback, got {self.bind_host!r} — "
                "exposing pause/stop/focus to the LAN is never allowed")
        if not self.auth_token:
            raise ValueError("control surface requires a non-empty auth token")


def start_control_surface(cfg: ControlSurfaceConfig,
                          source: ControlSurfaceSource) -> ThreadingHTTPServer:
    """Bind and return a ThreadingHTTPServer (caller runs serve_forever on a daemon
    thread and calls shutdown()/server_close() on teardown)."""
    handler = _make_handler(cfg, source)
    server = ThreadingHTTPServer((cfg.bind_host, cfg.port), handler)
    log.info("Control surface listening on http://%s:%d (loopback only)",
             cfg.bind_host, cfg.port)
    return server


def _make_handler(cfg: ControlSurfaceConfig, source: ControlSurfaceSource):
    """Build a BaseHTTPRequestHandler subclass closed over cfg+source (http.server
    instantiates a fresh handler per request, so config rides on the class, not self)."""

    class ControlHandler(BaseHTTPRequestHandler):
        server_version = "reelradar-control/1"

        def log_message(self, *_a):  # silence the default stderr access log
            pass

        def do_GET(self):  # noqa: N802
            if not self._guard():
                return
            if self.path.rstrip("/") == "/status":
                self._send(200, {"ok": True, "data": status_to_wire(source.get_status())})
            else:
                self._send(404, {"ok": False, "error": "unknown endpoint"})

        def do_POST(self):  # noqa: N802
            if not self._guard():
                return
            if self.path.rstrip("/") != "/command":
                self._send(404, {"ok": False, "error": "unknown endpoint"})
                return
            body = self._read_json()
            if body is None:
                self._send(400, {"ok": False, "error": "invalid JSON body"})
                return
            clean, err = validate_command(body)
            if err:
                self._send(400, {"ok": False, "error": err})
                return
            self._dispatch(clean["action"])

        def _dispatch(self, action: str) -> None:
            if action == "pause":
                source.pause()
                self._send(200, {"ok": True, "data": {"accepted": True}})
            elif action == "resume":
                source.resume()
                self._send(200, {"ok": True, "data": {"accepted": True}})
            elif action == "stopCurrentJob":
                self._send(200, {"ok": True, "data": {"accepted": source.stop_current_job()}})
            elif action == "focusWarmedChrome":
                self._send(200, {"ok": True, "data": {"accepted": source.focus_warmed_chrome()}})

        # --- guards + io ---------------------------------------------------

        def _guard(self) -> bool:
            """Loopback peer check (defense-in-depth beneath the token) + constant-time
            bearer check. Returns False and sends the rejection when it fails."""
            peer = self.client_address[0] if self.client_address else ""
            if peer not in ("127.0.0.1", "::1"):
                self._send(403, {"ok": False, "error": "loopback only"})
                return False
            header = self.headers.get("Authorization", "")
            expected = f"Bearer {cfg.auth_token}"
            if not hmac.compare_digest(header, expected):
                self._send(401, {"ok": False, "error": "unauthorized"})
                return False
            return True

        def _read_json(self) -> Optional[object]:
            try:
                length = int(self.headers.get("Content-Length", "0"))
            except ValueError:
                return None
            if length <= 0 or length > _MAX_BODY_BYTES:
                return None
            try:
                return json.loads(self.rfile.read(length).decode("utf-8"))
            except (ValueError, UnicodeDecodeError):
                return None

        def _send(self, status: int, payload: dict) -> None:
            body = json.dumps(payload).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    return ControlHandler
