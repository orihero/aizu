"""Launch preflight ↔ sidecar wiring (ledger F9/F10/F12, B6).

``preflight.py``'s CHECKS are unit-tested in ``test_preflight.py``; this file pins the
part that decides what a failing check DOES to a live box:

  - the composition order — control surface binds, THEN everything that touches the
    network (F10 port resolution, then the preflight), THEN register, so a box with a red
    preflight AND an unreachable cloud still tells its operator why;
  - the fatal policy — nothing exits, nothing refuses to register; a fatal failure
    WITHHOLDS capabilities and parks the lease loop until it heals — at launch AND
    mid-life, since the report can turn blocking long after the box started working;
  - the precedence — re-enrolment (B10) always beats preflight (§3.4);
  - the two control-surface commands the transport already routed to methods that did not
    exist, so a real sidecar answered ``200 {"accepted": true}`` and did nothing.

Everything is injected: no browser, no network, no keychain.
"""
from __future__ import annotations

import json
import threading
import time
from http.client import HTTPConnection
from typing import Optional

import pytest

from aizu.worker import preflight
from aizu.worker import sidecar as sidecar_module
from aizu.worker.config import WorkerConfig
from aizu.worker.lease_client import Result
from aizu.worker.preflight import CheckResult, PreflightReport
from aizu.worker.sidecar import Sidecar, SidecarControlSource

_WAIT_TIMEOUT_SEC = 5.0
_POLL_SEC = 0.01

CAP_YT = (None, "youtube", None)


# ----- scaffolding -------------------------------------------------------------

def _wait_until(predicate, timeout: float = _WAIT_TIMEOUT_SEC) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(_POLL_SEC)
    return predicate()


def _report(*checks: CheckResult, enforced: bool = True) -> PreflightReport:
    return PreflightReport(checks=tuple(checks), ran_at=1234.5, duration_ms=7,
                           enforced=enforced)


def _fatal(check_id: str = preflight.CHECK_CAPABILITIES,
           detail: str = "neither AIZU_WORKER_PLATFORMS nor AIZU_WORKER_CAPABILITIES "
                         "is set") -> CheckResult:
    return CheckResult(check_id, "Platforms this box advertises", preflight.SEVERITY_FATAL,
                       preflight.STATUS_FAIL, detail, "set AIZU_WORKER_PLATFORMS=all")


def _warn(check_id: str = "login.instagram", status: str = preflight.STATUS_FAIL
          ) -> CheckResult:
    return CheckResult(check_id, "Chrome is signed in to instagram",
                       preflight.SEVERITY_WARN, status, "logged_out", "open a login tab")


def _unknown_fatal() -> CheckResult:
    """A fatal check that could not TELL. Must never block (rule: unknown never blocks)."""
    return CheckResult(preflight.CHECK_CDP_ATTACHABLE, "Chrome accepts a DevTools attach",
                       preflight.SEVERITY_FATAL, preflight.STATUS_UNKNOWN,
                       "Playwright is unavailable — cannot attach to Chrome")


class _Scripted:
    """Returns each report in turn; the LAST one repeats forever (a box that stays broken,
    or stays fixed). Counts calls so 'once per park tick' is assertable."""

    def __init__(self, *reports: PreflightReport):
        self._reports = list(reports)
        self.calls = 0

    def __call__(self, cfg) -> PreflightReport:
        self.calls += 1
        if len(self._reports) > 1:
            return self._reports.pop(0)
        return self._reports[0]


class _FakeClient:
    """Records register bodies (capabilities + preflight are the whole point here) and
    scripts lease responses."""

    token = "worker-token-1"

    def __init__(self, *, leases: Optional[list] = None,
                 register_result: Optional[Result] = None):
        self._leases = list(leases or [])
        self._register_result = register_result
        self.registers: list[dict] = []
        self.lease_calls = 0
        self.on_register = None      # optional callback: sees the world at register time

    def with_token(self, token):
        return self

    def register(self, body):
        self.registers.append(dict(body))
        if self.on_register is not None:
            self.on_register()
        return self._register_result or Result(
            ok=True, data={"workerId": "w1", "heartbeatIntervalSec": 0.05})

    def lease(self, body):
        self.lease_calls += 1
        return self._leases.pop(0) if self._leases else Result(ok=True, data=None)

    def heartbeat(self, job_id, body):
        return Result(ok=True, data={})

    def presence(self, body):
        return Result(ok=True, data={})

    def ack(self, job_id, body):
        return Result(ok=True, data={})

    def nack(self, job_id, body):
        return Result(ok=True, data={})

    def credential(self, job_id):
        return Result(ok=True, data={"credential": None})

    def close(self):
        pass


class _DummyStore:
    def close(self):
        pass


def _sidecar(cfg: WorkerConfig, client: _FakeClient, preflight_fn) -> Sidecar:
    return Sidecar(cfg, client=client, store=_DummyStore(), sleep=lambda t: None,
                   preflight_fn=preflight_fn)


@pytest.fixture
def cdp_cfg(cfg: WorkerConfig) -> WorkerConfig:
    """The conftest cfg re-pointed at a CDP platform, for the capability-withholding
    assertions (the default fixture is API-only youtube on purpose)."""
    from dataclasses import replace
    return replace(cfg, capabilities=((1, "instagram", "acme_ig"), CAP_YT))


# ----- composition order (§3.2) -------------------------------------------------

def test_the_control_surface_binds_before_the_preflight_and_the_preflight_before_register(
        cfg: WorkerConfig):
    """The ordering property the whole local UI depends on. The surface must be serving
    before anything network-facing, so a box whose dispatch is down still reflects itself;
    the preflight must land before register, so registration carries HONEST capabilities on
    the very first call instead of advertising work this box cannot do."""
    from dataclasses import replace
    cfg = replace(cfg, control_surface_enabled=True, control_surface_token="t",
                  control_surface_port=0)
    seen: list[str] = []
    client = _FakeClient()

    def _preflight_fn(_cfg):
        # The surface is already bound by the time the FIRST preflight runs.
        seen.append("surface" if sc._control_server is not None else "no-surface")
        seen.append("preflight")
        return _report()

    sc = _sidecar(cfg, client, _preflight_fn)
    client.on_register = lambda: seen.append(
        "register(preflight=%s)" % ("known" if sc.preflight is not None else "MISSING"))
    try:
        sc.run(max_iterations=1)
    finally:
        sc.close()

    assert seen == ["surface", "preflight", "register(preflight=known)"]


def test_cdp_port_resolution_runs_BEHIND_the_control_surface_bind(cfg: WorkerConfig,
                                                                  monkeypatch):
    """F10 port adoption probes TWO ports with real socket timeouts (3.0s + 1.5s), and it
    used to run in `main()` — before the Sidecar object existed, so before anything could
    bind. That put up to 4.5 seconds of blocking network I/O in front of the local UI on
    exactly the box whose Chrome is down, i.e. the one whose operator most needs to be told
    why. Asserted the strong way: the surface really SERVES a request while resolution is
    still in flight. It must still run BEFORE the preflight, so the checks read the adopted
    URL rather than the port literal that did not answer."""
    from dataclasses import replace
    cfg = replace(cfg, control_surface_enabled=True, control_surface_token="tok",
                  control_surface_port=0)
    adopted = "http://127.0.0.1:9333"
    order: list[str] = []
    box: dict = {}
    served: dict = {}

    def _resolve(c: WorkerConfig) -> WorkerConfig:
        order.append("resolve")
        # The whole point: the desktop app can already talk to this box.
        port = box["sc"]._control_server.server_address[1]
        served["status"], served["body"] = _req(port, "GET", "/status")
        return replace(c, cdp_url=adopted)

    def _preflight_fn(c: WorkerConfig) -> PreflightReport:
        order.append("preflight")
        order.append(c.cdp_url)
        return _report()

    monkeypatch.setattr(sidecar_module, "_resolve_cdp_url", _resolve)
    sc = _sidecar(cfg, _FakeClient(), _preflight_fn)
    box["sc"] = sc
    try:
        sc.run(max_iterations=1)
    finally:
        sc.close()

    assert served["status"] == 200, "the local UI must be served DURING port resolution"
    assert order == ["resolve", "preflight", adopted]
    assert sc._cfg.cdp_url == adopted, "the adopted URL must stick on the live config"


# ----- fatal policy: withhold capabilities, park, never exit (§3.1/§3.3) --------

def test_a_blocking_preflight_registers_with_NO_capabilities(cdp_cfg: WorkerConfig):
    """It registers — an unregistered box is invisible to BOTH the admin and the operator
    (F12: nobody can SSH into these PCs). It just holds no capabilities, which is what
    makes `_job_capability_covers` unable to match it."""
    client = _FakeClient()
    sc = _sidecar(cdp_cfg, client, _Scripted(_report(_fatal())))

    sc.run(max_iterations=1)

    assert client.registers, "a blocking preflight must never refuse registration"
    assert client.registers[0]["capabilities"] == []


def test_a_blocking_preflight_parks_the_lease_loop_instead_of_leasing(
        cdp_cfg: WorkerConfig):
    client = _FakeClient()
    sc = _sidecar(cdp_cfg, client, _Scripted(_report(_fatal())))

    sc.run(max_iterations=3)

    assert client.lease_calls == 0, "a parked box must never take work"
    assert client.registers and len(client.registers) == 1   # and never re-registers blind


def test_the_park_heals_and_re_registers_with_this_box_s_REAL_capabilities(
        cdp_cfg: WorkerConfig):
    """The self-healing half: the box is not condemned, it is paused. Chrome comes up (or
    an env var is fixed and the sidecar is restarted-free), the next 30s tick clears, and
    the box re-registers with its real capabilities and starts leasing — unattended."""
    client = _FakeClient()
    fn = _Scripted(_report(_fatal()), _report())
    sc = _sidecar(cdp_cfg, client, fn)

    sc.run(max_iterations=2)

    assert len(client.registers) == 2
    assert client.registers[0]["capabilities"] == []
    assert client.registers[1]["capabilities"] == list(cdp_cfg.capabilities)
    assert client.lease_calls >= 1, "it must resume leasing once the report clears"


def test_the_park_re_registers_on_the_TRANSITION_only_not_every_tick(
        cdp_cfg: WorkerConfig):
    """`register_worker` ROTATES worker_token_hash. A Chrome restarting in a loop must not
    rotate this box's credential every 30 seconds."""
    client = _FakeClient()
    sc = _sidecar(cdp_cfg, client, _Scripted(_report(_fatal())))

    sc.run(max_iterations=5)

    assert len(client.registers) == 1


def test_the_park_re_probes_once_per_tick(cdp_cfg: WorkerConfig):
    client = _FakeClient()
    fn = _Scripted(_report(_fatal()))
    sc = _sidecar(cdp_cfg, client, fn)

    sc.run(max_iterations=4)

    assert fn.calls == 1 + 4      # the launch pass, then one per bounded park tick


def test_a_WARN_only_report_never_withholds_anything(cdp_cfg: WorkerConfig):
    """The governing split: the wizard blocks on a login row, the launch preflight only
    informs. A red `login.instagram` at 4am must not dark a fleet over a cookie name
    nobody has validated against a live session."""
    client = _FakeClient()
    sc = _sidecar(cdp_cfg, client, _Scripted(_report(_warn())))

    sc.run(max_iterations=1)

    assert client.registers[0]["capabilities"] == list(cdp_cfg.capabilities)
    assert client.lease_calls == 1


def test_a_fatal_check_that_returned_UNKNOWN_never_blocks(cdp_cfg: WorkerConfig):
    """A probe that could not TELL must never dark a box — enforced by
    `CheckResult.blocking` requiring status == fail, asserted here end to end."""
    client = _FakeClient()
    sc = _sidecar(cdp_cfg, client, _Scripted(_report(_unknown_fatal())))

    sc.run(max_iterations=1)

    assert client.registers[0]["capabilities"] == list(cdp_cfg.capabilities)
    assert client.lease_calls == 1


def test_break_glass_demotes_at_the_REPORT_level(cdp_cfg: WorkerConfig):
    """AIZU_PREFLIGHT_ENFORCE=0 must not rewrite severities — the operator still sees WHICH
    checks are fatal, and `enforced:false` rides upstream so a fleet quietly running
    unenforced is visible rather than invisible."""
    client = _FakeClient()
    sc = _sidecar(cdp_cfg, client, _Scripted(_report(_fatal(), enforced=False)))

    sc.run(max_iterations=1)

    assert client.registers[0]["capabilities"] == list(cdp_cfg.capabilities)
    assert client.registers[0]["preflight"]["enforced"] is False
    assert client.registers[0]["preflight"]["blocking"] is False
    assert sc.preflight.get(preflight.CHECK_CAPABILITIES).severity == preflight.SEVERITY_FATAL


def test_OUR_OWN_BUG_leaves_the_box_leasing_at_FULL_capabilities(cdp_cfg: WorkerConfig,
                                                                 caplog):
    """Belt-and-braces over `run_preflight`'s per-check guards: even a crash in the
    composition itself must never park a healthy machine."""
    def _boom(_cfg):
        raise RuntimeError("preflight composition exploded")

    client = _FakeClient()
    sc = _sidecar(cdp_cfg, client, _boom)

    sc.run(max_iterations=1)

    assert client.registers[0]["capabilities"] == list(cdp_cfg.capabilities)
    assert client.lease_calls == 1
    error = sc.preflight.get(preflight.CHECK_PREFLIGHT_ERROR)
    assert error is not None and error.severity == preflight.SEVERITY_WARN
    # The exception TYPE only — never its message, which can carry a path or a secret.
    assert "RuntimeError" in (error.detail or "")
    assert "exploded" not in json.dumps(sc.preflight_wire())


# ----- the report can turn blocking MID-LIFE (§3.1, the one-way door) -----------

class _FlipsThePreflightOnFirstLease(_FakeClient):
    """Stands in for the operator's `runPreflight` (the wizard's Re-check button) landing
    between two lease polls: `request_preflight` publishes from a DETACHED thread, so the
    loop can find a different report on any iteration."""

    def __init__(self, box: dict, report: PreflightReport):
        super().__init__()
        self._box = box
        self._report = report

    def lease(self, body):
        if self.lease_calls == 0:
            self._box["sc"]._preflight = self._report
        return super().lease(body)


def test_a_report_that_turns_BLOCKING_mid_life_parks_the_loop_and_heals(
        cdp_cfg: WorkerConfig):
    """`_effective_capabilities` is read at REGISTER time and nowhere else, so a report
    that went blocking after the box was already working changed NOTHING — the loop leased
    on regardless, and whichever `_register` fired next (the B10 re-probe's) advertised an
    empty capability set with no park and no tick that would ever look at the report again.
    The box then leased nothing until someone restarted it, on a machine nobody can SSH
    into. So: the loop re-checks, withdraws, parks, and heals itself."""
    box: dict = {}
    client = _FlipsThePreflightOnFirstLease(box, _report(_fatal()))
    sc = _sidecar(cdp_cfg, client, _Scripted(_report()))
    box["sc"] = sc

    sc.run(max_iterations=2)

    assert client.lease_calls == 1, "it must not keep leasing on a blocking report"
    assert [r["capabilities"] for r in client.registers] == [
        list(cdp_cfg.capabilities),   # launch: healthy
        [],                           # the mid-life block: withdrawn
        list(cdp_cfg.capabilities),   # the park's next tick cleared it: back to work
    ]


def test_a_mid_life_withdrawal_registers_ONCE_not_once_per_park_tick(
        cdp_cfg: WorkerConfig):
    """Same rule the park's heal register obeys: `register_worker` ROTATES
    worker_token_hash, so a Chrome restarting in a loop must not rotate this box's
    credential every 30 seconds (F9.1)."""
    box: dict = {}
    client = _FlipsThePreflightOnFirstLease(box, _report(_fatal()))
    sc = _sidecar(cdp_cfg, client, _Scripted(_report(), _report(_fatal())))
    box["sc"] = sc

    sc.run(max_iterations=6)

    assert len(client.registers) == 2          # launch, then ONE withdrawal
    assert client.registers[1]["capabilities"] == []
    assert client.lease_calls == 1


def test_resume_leasing_after_a_re_enrolment_park_re_checks_the_preflight(
        cdp_cfg: WorkerConfig):
    """The exact one-way door, at the exact seam it opened. `_reprobe_registration` builds
    its register body from `_effective_capabilities`, so a report that was blocking while
    the box sat parked on B10 brings it back with NOTHING — and `resume_leasing` walked
    straight into the lease loop, which never consulted the report, and stayed there.
    Every ENTRY into the loop must re-check, not just `run()`."""
    client = _FakeClient()
    sc = _sidecar(cdp_cfg, client, _Scripted(_report()))
    sc._worker_id = "w1"
    sc._registered_capabilities = []       # the B10 re-probe registered it empty...
    sc._preflight = _report(_fatal())      # ...because the report was blocking

    sc.resume_leasing(max_iterations=1)

    assert client.lease_calls == 0, "a blocking report must not lease, however it got there"
    assert [r["capabilities"] for r in client.registers] == [list(cdp_cfg.capabilities)]


def test_a_re_register_REPLACES_the_presence_thread_instead_of_stacking_one(
        cdp_cfg: WorkerConfig):
    """`_register` is no longer once per process — the park's heal, a mid-life withdrawal
    and the B10 re-probe all call it — and each success started ANOTHER presence beat while
    the previous one kept posting with a superseded client. `close()` only ever stopped the
    last of them."""
    client = _FakeClient()
    sc = _sidecar(cdp_cfg, client, _Scripted(_report()))
    try:
        sc.run(max_iterations=1)
        first = sc._presence_thread
        assert first is not None and not first._stop_event.is_set()

        assert sc._register() is True

        assert sc._presence_thread is not first
        assert first._stop_event.is_set(), "the superseded beat must be stopped"
    finally:
        sc.close()


# ----- precedence: B10 always wins (§3.4) ---------------------------------------

def test_reenrolment_beats_preflight_park(cdp_cfg: WorkerConfig, cipher):
    """The single most likely integration bug. Revocation is TERMINAL until a human acts;
    preflight is self-healing. Backwards, this either spins a revoked box on preflight
    rechecks forever, or makes a merely-misconfigured box log the misleading re-enrolment
    CRITICAL. `main()`'s `while sidecar.reenrolment_required:` loop must get control."""
    from aizu.worker.token_store import TokenStore
    client = _FakeClient()
    fn = _Scripted(_report(_fatal()))
    sc = _sidecar(cdp_cfg, client, fn)
    sc._tokens = TokenStore(cdp_cfg.state_dir, cipher=cipher)
    sc._worker_id = "w1"
    sc._preflight = _report(_fatal())      # blocking, as if the launch pass just ran
    sc._on_auth_revoked("lease")           # ...and the box is ALSO revoked

    parked = sc._park_for_preflight(max_waits=5)

    assert parked is False
    assert fn.calls == 0, "the park must return BEFORE re-probing a revoked box"
    assert sc.reenrolment_required is True


def test_the_park_gives_up_on_a_drain(cdp_cfg: WorkerConfig):
    client = _FakeClient()
    sc = _sidecar(cdp_cfg, client, _Scripted(_report(_fatal())))
    sc._stop_leasing.set()

    assert sc._park_for_preflight(max_waits=3) is False


# ----- a raising register PARKS, it never kills the process ---------------------

def test_a_register_that_RAISES_parks_instead_of_crashing(cfg: WorkerConfig):
    """Found in a live shakedown, not in review.

    `state_dir_writable` exists to pre-empt exactly one crash: `cfg.machine_id` WRITES
    into the state dir, so an unwritable AIZU_WORKER_STATE raises PermissionError from
    inside `_register` — several frames below `run()`. With the check fatal but the park
    happening AFTER register, a real box printed the correct fatal AND its remedy and then
    died on the very next line with a traceback, taking down the control surface that was
    the only way to SHOW that report, and handing its supervisor a crash-loop.

    So: a raising register must be a False return and a park, never an exit.
    """
    class _Exploding(_FakeClient):
        def register(self, body):        # never even reached — machine_id raises first
            raise AssertionError("register should not have been called")

    def _boom():
        # Stand in for the unwritable state dir, at the same frame the real one raises
        # from: cfg.machine_id, reached from _register_unguarded.
        raise PermissionError(13, "Permission denied", "machine-id")

    client = _Exploding()
    sc = _sidecar(cfg, client, _Scripted(_report(_fatal(preflight.CHECK_STATE_DIR))))
    # Instance attribute, NOT the class: patching the type here leaks into every later
    # test in the module (it did, on the first draft of this test).
    sc._register_unguarded = _boom

    sc.run(max_iterations=1)              # must RETURN, not raise

    assert sc._worker_id is None          # it never registered...
    assert client.registers == []         # ...and never got a body out
    # ...but it went through the park path rather than exiting, so the control surface
    # stayed up and the box re-checks itself back to health with no restart.
    assert sc._preflight_blocking() is True


# ----- what rides upstream (§4.4/§5.1) ------------------------------------------

def test_the_register_body_carries_the_COMPACT_preflight(cdp_cfg: WorkerConfig):
    client = _FakeClient()
    sc = _sidecar(cdp_cfg, client, _Scripted(_report(_fatal(), _warn())))

    sc.run(max_iterations=1)

    body = client.registers[0]["preflight"]
    assert body["blocking"] is True and body["ok"] is False
    assert [f["id"] for f in body["failed"]] == [preflight.CHECK_CAPABILITIES,
                                                 "login.instagram"]
    # title/remedy are UI copy the console resolves from the id — they must NOT ride the
    # wire (it halves the body and keeps operator-facing text under our control).
    assert all(set(f) == {"id", "severity", "status", "detail"} for f in body["failed"])


def test_an_oversized_report_is_simply_omitted_never_sent(cdp_cfg: WorkerConfig):
    """A diagnostic hint must never be the reason a workable box cannot register (B9's
    rule, applied here): over budget, the key just disappears."""
    # Over budget via long ids, sized relative to the cap — rows and details are both
    # capped already, so a literal here would quietly stop being oversized when the
    # budget next changes.
    _pad = "capabilities-" * (preflight.MAX_UPSTREAM_BYTES // 16)
    huge = tuple(_fatal(check_id=f"{_pad}{i}",
                        detail="x" * preflight.MAX_UPSTREAM_DETAIL)
                 for i in range(preflight.MAX_UPSTREAM_FAILED))
    client = _FakeClient()
    sc = _sidecar(cdp_cfg, client, _Scripted(_report(*huge)))

    sc.run(max_iterations=1)

    assert "preflight" not in client.registers[0]
    assert client.registers[0]["capabilities"] == []   # ...but the enforcement still lands


# ----- the control surface (§4.5/§4.6) ------------------------------------------

def test_status_preflight_is_null_before_the_first_run(cfg: WorkerConfig):
    """None means 'checking…', NEVER 'healthy'. A box whose preflight has not finished has
    not been cleared of anything."""
    sc = _sidecar(cfg, _FakeClient(), _Scripted(_report()))
    assert sc.preflight_wire() is None
    assert SidecarControlSource(sc).get_status().preflight is None


def test_status_carries_the_FULL_report_with_remedies(cfg: WorkerConfig):
    """Unlike the upstream body, this payload never leaves the box — so it carries the
    remedy text the operator on this PC has to read."""
    sc = _sidecar(cfg, _FakeClient(), _Scripted(_report(_fatal(), _warn())))
    sc._refresh_preflight()

    wire = SidecarControlSource(sc).get_status().preflight

    assert wire["blocking"] is True and wire["enforced"] is True
    ids = [c["id"] for c in wire["checks"]]
    assert ids == [preflight.CHECK_CAPABILITIES, "login.instagram"]
    assert wire["checks"][0]["remedy"] == "set AIZU_WORKER_PLATFORMS=all"


def test_SidecarControlSource_really_implements_both_new_commands(cfg: WorkerConfig):
    """The landed latent bug: `control_surface._dispatch` already routed `runPreflight` and
    `openLoginTab` to these, and `_detach` wraps the call in `_swallow` — so while this
    adapter lacked them, a real sidecar answered 200 {"accepted": true} and then silently
    did nothing, the AttributeError eaten on a daemon thread. Asserted on the REAL adapter;
    a fake source could never have caught it."""
    source = SidecarControlSource(_sidecar(cfg, _FakeClient(), _Scripted(_report())))
    assert callable(getattr(source, "run_preflight", None))
    assert callable(getattr(source, "open_login_tab", None))


def test_run_preflight_over_HTTP_answers_accepted_and_actually_re_probes(
        cfg: WorkerConfig):
    """'accepted', not 'completed' — a full pass takes ~9.5s, far past the desktop's 3s
    timeout, so the operator's feedback is the next /status poll."""
    from dataclasses import replace
    cfg = replace(cfg, control_surface_enabled=True, control_surface_token="tok",
                  control_surface_port=0)
    ran = threading.Event()

    def _preflight_fn(_cfg):
        ran.set()
        return _report(_warn())

    sc = _sidecar(cfg, _FakeClient(), _preflight_fn)
    sc.start_control_surface()
    port = sc._control_server.server_address[1]
    threading.Thread(target=sc._control_server.serve_forever, daemon=True).start()
    try:
        status, data = _req(port, "POST", "/command", body={"action": "runPreflight"})
        assert status == 200 and data["data"]["accepted"] is True
        assert ran.wait(_WAIT_TIMEOUT_SEC), "the command must really reach the sidecar"
        assert _wait_until(lambda: sc.preflight is not None)
        _, status_body = _req(port, "GET", "/status")
        assert status_body["data"]["preflight"]["checks"][0]["id"] == "login.instagram"
    finally:
        sc.close()


def test_open_login_tab_over_HTTP_reaches_readiness_with_the_platform(
        cfg: WorkerConfig, monkeypatch):
    """The LOCAL half of the handoff `/api/agent/launch-login` has always promised in
    distributed mode: the warmed Chrome is on THIS PC and there is no inbound path to it
    from the cloud, so the desktop app is the only thing that can open the tab."""
    from dataclasses import replace
    from aizu import readiness
    cfg = replace(cfg, control_surface_enabled=True, control_surface_token="tok",
                  control_surface_port=0)
    seen: dict = {}
    opened = threading.Event()

    def _fake_open(cdp_url, timeout=5.0, *, platform="instagram", opener=None):
        seen["cdp_url"] = cdp_url
        seen["platform"] = platform
        opened.set()
        return True

    monkeypatch.setattr(readiness, "open_login_tab", _fake_open)
    sc = _sidecar(cfg, _FakeClient(), _Scripted(_report()))
    sc.start_control_surface()
    port = sc._control_server.server_address[1]
    threading.Thread(target=sc._control_server.serve_forever, daemon=True).start()
    try:
        status, data = _req(port, "POST", "/command",
                            body={"action": "openLoginTab", "platform": "linkedin"})
        assert status == 200 and data["data"]["accepted"] is True
        assert opened.wait(_WAIT_TIMEOUT_SEC)
    finally:
        sc.close()

    assert seen["platform"] == "linkedin"
    assert seen["cdp_url"] == cfg.cdp_url


def test_open_login_tab_refuses_while_a_job_is_running(cfg: WorkerConfig, monkeypatch):
    """Same single-browser invariant `check_browser` honours: a live run owns the one CDP
    connection this architecture allows, and navigating its browser mid-harvest is worse
    than making the operator wait."""
    from aizu import readiness
    calls: list = []
    monkeypatch.setattr(readiness, "open_login_tab",
                        lambda *a, **k: calls.append(a) or True)
    sc = _sidecar(cfg, _FakeClient(), _Scripted(_report()))
    with sc._active_jobs_lock:
        sc._active_jobs = 1

    assert sc.open_login_tab("instagram") is True    # still 'accepted' at the boundary
    time.sleep(0.05)
    assert calls == []


def test_a_manual_re_check_COALESCES_instead_of_stacking_attaches(cfg: WorkerConfig):
    """The wizard's Sign-in step polls this while a human logs in. Stacking would put
    several Playwright clients on one Chrome — the very thing check_browser refuses to do
    itself."""
    started = threading.Event()
    release = threading.Event()
    calls: list = []

    def _slow(_cfg):
        calls.append(1)
        started.set()
        release.wait(_WAIT_TIMEOUT_SEC)
        return _report()

    sc = _sidecar(cfg, _FakeClient(), _slow)
    try:
        assert sc.request_preflight() is True
        assert started.wait(_WAIT_TIMEOUT_SEC)
        for _ in range(5):
            assert sc.request_preflight() is True     # accepted, but folded into the first
    finally:
        release.set()
    assert _wait_until(lambda: sc.preflight is not None)
    assert calls == [1]


# ----- the real preflight against a real (fake-free) box ------------------------

def test_the_launch_preflight_leaves_an_ALREADY_STORED_token_alone(cfg: WorkerConfig,
                                                                   cipher):
    """`token_persistence` probes the MECHANISM, and when a credential is already stored,
    loading it IS the proof — the probe must never overwrite or delete a live token."""
    from aizu.worker.token_store import TokenStore
    tokens = TokenStore(cfg.state_dir, cipher=cipher)
    tokens.save("the-live-worker-token")

    sc = Sidecar(cfg, client=_FakeClient(), store=_DummyStore(), sleep=lambda t: None)
    report = sc._refresh_preflight()

    assert tokens.load() == "the-live-worker-token"
    assert report.get(preflight.CHECK_TOKEN_PERSISTENCE).status == preflight.STATUS_PASS


def test_the_default_preflight_wires_this_box_s_LIVE_run_active(cfg: WorkerConfig,
                                                                monkeypatch):
    """`check_browser` must SKIP while a job is running rather than open a SECOND
    connect_over_cdp against the browser that run already owns. `run_preflight(cfg)` alone
    cannot know that — only a live Sidecar can — so the default callable is a bound method,
    not the bare module function."""
    seen: dict = {}

    def _capture(cfg_arg, *, probes, **_):
        seen["run_active"] = probes.run_active()
        return _report()

    monkeypatch.setattr(preflight, "run_preflight", _capture)
    sc = Sidecar(cfg, client=_FakeClient(), store=_DummyStore(), sleep=lambda t: None)
    with sc._active_jobs_lock:
        sc._active_jobs = 1

    sc._refresh_preflight()

    assert seen["run_active"] is True


def test_an_api_only_box_runs_a_network_free_sub_millisecond_preflight(cfg: WorkerConfig):
    """The property the whole worker suite depends on: a youtube-only box advertises no
    CDP platform, so every Chrome check SKIPs and nothing touches the network."""
    sc = Sidecar(cfg, client=_FakeClient(), store=_DummyStore(), sleep=lambda t: None)

    report = sc._refresh_preflight()

    assert report.blocking is False
    for check_id in (preflight.CHECK_CDP_REACHABLE, preflight.CHECK_CDP_ATTACHABLE,
                     preflight.CHECK_CDP_PORT_DRIFT):
        assert report.get(check_id).status == preflight.STATUS_SKIP
    assert report.duration_ms < 500


def test_a_failed_token_save_never_crashes_after_the_identity_is_minted(
        cfg: WorkerConfig, caplog):
    """F9.1's observed shape: the server mints the identity, THEN the box crashes storing
    it — so the fleet row reads 'online' over a dead process and a supervisor crash-loop
    rotates worker_token_hash every iteration. It must degrade to 're-registers each
    start', never to a crash."""
    class _BrokenTokens:
        def load(self):
            return None

        def save(self, token):
            raise RuntimeError("AIZU_SECRET_KEY is not set")

        def clear(self):
            pass

    client = _FakeClient(register_result=Result(
        ok=True, data={"workerId": "w1", "token": "minted", "heartbeatIntervalSec": 5}))
    sc = _sidecar(cfg, client, _Scripted(_report()))
    sc._tokens = _BrokenTokens()

    sc.run(max_iterations=1)

    assert sc._worker_id == "w1"
    assert client.lease_calls == 1, "the box must keep working with the in-memory token"


# ----- http helper --------------------------------------------------------------

def _req(port, method, path, *, token="tok", body=None):
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
