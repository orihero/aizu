"""Launch-time preflight (worker/preflight.py) — ledger F9/F10/F12, B6/D3.

Every failure this module exists to catch produced a box that read PERFECTLY HEALTHY in
the fleet console and could not work, so the tests here are written against the two
properties that make the preflight safe to ship rather than against its happy path:

  1. **It can never brick a working box.** A fatal verdict withholds capabilities and
     parks the lease loop; it never exits, never refuses registration, and self-heals.
     So the tests that matter most are the ones pinning what may NOT block: an
     ``unknown`` result, a check that raised, a probe that raised, a warn-severity row.
  2. **It never leaks a secret.** ``detail`` rides the wire up to the superadmin fleet
     console; ``test_no_secret_value_in_any_wire_field`` walks every wire field AND
     every log line with real-looking secret values planted in the environment.

Nothing here touches a real browser, a real network or a real keychain: every side
effect goes through :class:`preflight.Probes` (or ``token_store.TokenStore``, which is
monkeypatched at the module attribute the function imports it from). The one deliberate
exception is the FernetFileBackend round-trip, which runs against a real 0600 file in
tmp_path because that is precisely the F9.1 mechanism under test.

``FakeCfg`` is used instead of a real ``WorkerConfig`` for most cases on purpose:
preflight reads cfg only through ``getattr``, and the two F10 fields
(``cdp_url_explicit``/``cdp_url_drift_note``) land in another owner's file. Testing
against the attribute contract keeps this suite honest during that migration — plus it
lets a test model an OLD config that lacks the note field, which is the version-skew
branch ``resolve_cdp_url`` guards. ``test_run_preflight_against_a_real_worker_config``
closes the loop against the real dataclass.
"""
from __future__ import annotations

import dataclasses
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import pytest

from aizu.secrets import SecretCipher, SecretCipherError
from aizu.worker import preflight
from aizu.worker.config import WorkerConfig
from aizu.worker.preflight import (BrowserProbe, CheckResult, PreflightReport, Probes,
                                   SEVERITY_FATAL, SEVERITY_WARN, STATUS_FAIL,
                                   STATUS_PASS, STATUS_SKIP, STATUS_UNKNOWN)

CDP_9222 = "http://127.0.0.1:9222"
CDP_9333 = "http://127.0.0.1:9333"

IG = (None, "instagram", None)
X = (None, "x", None)
LI = (None, "linkedin", None)
YT = (None, "youtube", None)


# ---- test doubles ----

@dataclass(frozen=True)
class FakeCfg:
    """The attribute surface preflight actually reads off a WorkerConfig, and nothing
    else. Frozen + a dataclass so ``dataclasses.replace`` (resolve_cdp_url) works."""

    state_dir: Path
    capabilities: tuple = ()
    dispatch_base_url: str = "http://dispatch.local"
    bootstrap_token: Optional[str] = None
    cdp_url: str = CDP_9333
    cdp_url_explicit: bool = False
    cdp_url_drift_note: Optional[str] = None


@dataclass(frozen=True)
class OldCfg:
    """A pre-F10 config: no ``cdp_url_drift_note``. Models a partially-upgraded install
    (the ``TypeError`` fallback in ``resolve_cdp_url``) — a sidecar that adopted the
    sibling port must still work when it cannot stamp the receipt."""

    state_dir: Path
    capabilities: tuple = ()
    dispatch_base_url: str = "http://dispatch.local"
    cdp_url: str = CDP_9333
    cdp_url_explicit: bool = False


class RecordingLogger:
    """``log_to`` takes its logger as an argument, so assertions never depend on
    caplog's propagation (configure_logging sets propagate=False on the aizu tree and
    any earlier test in the session may have called it)."""

    def __init__(self):
        self.errors: list = []
        self.warnings: list = []

    def _fmt(self, msg, args) -> str:
        try:
            return msg % args if args else msg
        except TypeError:  # pragma: no cover - a malformed format is itself a failure
            return f"{msg!r} % {args!r}"

    def error(self, msg, *args) -> None:
        self.errors.append(self._fmt(msg, args))

    def warning(self, msg, *args) -> None:
        self.warnings.append(self._fmt(msg, args))

    @property
    def lines(self) -> list:
        return self.errors + self.warnings


def _probes(*, env: Optional[dict] = None, cdp: Optional[dict] = None,
            browser=None, token_error: Optional[str] = None,
            token_present: Optional[bool] = True,
            run_active: bool = False, playwright: bool = True,
            chrome_profile: Optional[object] = None,
            calls: Optional[dict] = None) -> Probes:
    """Every probe inert by default. ``cdp`` maps url -> 'ok'|'unreachable' (absent =
    unreachable); ``calls`` (when given) records what was invoked so a test can assert
    a probe was NOT run — "the API-only box never touches the network" is a property,
    not an implementation detail.

    ``chrome_profile`` defaults to None — i.e. "this box has no worker-managed Chrome
    profile", which is what a desktop-shell box looks like from the sidecar. Inert matters
    more here than anywhere else: the REAL default stats the operator's home directory, so
    a Probes() built without it would make this suite machine-dependent."""
    env_map = dict(env or {})
    cdp_map = dict(cdp or {})
    log: dict = calls if calls is not None else {}
    log.setdefault("cdp", [])
    log.setdefault("browser", [])
    log.setdefault("token", [])
    log.setdefault("present", [])
    log.setdefault("chrome_profile", [])

    def _cdp(url: str, timeout: float = 3.0) -> str:
        log["cdp"].append(url)
        return cdp_map.get(url, "unreachable")

    def _browser(url: str, platforms: tuple, timeout: float = 5.0) -> BrowserProbe:
        log["browser"].append((url, tuple(platforms)))
        if browser is None:
            return BrowserProbe(attached=True,
                                logins={p: "logged_in" for p in platforms})
        if callable(browser):
            return browser(url, platforms, timeout)
        return browser

    def _token(cfg) -> Optional[str]:
        log["token"].append(cfg)
        return token_error

    def _present(cfg) -> Optional[bool]:
        log["present"].append(cfg)
        return token_present

    def _chrome_profile() -> Optional[str]:
        log["chrome_profile"].append(True)
        return None if chrome_profile is None else str(chrome_profile)

    return Probes(cdp=_cdp, browser=_browser, token_roundtrip=_token,
                  token_present=_present,
                  env=lambda name: env_map.get(name, ""),
                  run_active=lambda: run_active,
                  playwright_available=lambda: playwright,
                  chrome_profile_base=_chrome_profile)


def _green_env() -> dict:
    return {"OPENROUTER_API_KEY": "sk-test"}


# ---- CheckResult: the severity model ----

@pytest.mark.parametrize("severity,status,expected", [
    (SEVERITY_FATAL, STATUS_FAIL, True),
    (SEVERITY_FATAL, STATUS_UNKNOWN, False),   # rule 3: unknown NEVER blocks
    (SEVERITY_FATAL, STATUS_SKIP, False),
    (SEVERITY_FATAL, STATUS_PASS, False),
    (SEVERITY_WARN, STATUS_FAIL, False),       # a warning only ever annotates
    (SEVERITY_WARN, STATUS_UNKNOWN, False),
])
def test_only_a_fatal_failure_blocks(severity, status, expected):
    assert CheckResult("x", "X", severity, status).blocking is expected


def test_check_result_wire_shape_is_exactly_six_keys():
    wire = CheckResult("x", "X", SEVERITY_WARN, STATUS_FAIL, "d", "r").to_wire()
    assert wire == {"id": "x", "title": "X", "severity": SEVERITY_WARN,
                    "status": STATUS_FAIL, "detail": "d", "remedy": "r"}


# ---- PreflightReport ----

def _report(*checks, enforced: bool = True) -> PreflightReport:
    return PreflightReport(checks=tuple(checks), ran_at=1786800000.0, duration_ms=7,
                           enforced=enforced)


def test_report_blocking_and_ok():
    fatal = CheckResult("a", "A", SEVERITY_FATAL, STATUS_FAIL)
    warn = CheckResult("b", "B", SEVERITY_WARN, STATUS_FAIL)
    assert _report(fatal, warn).blocking is True
    assert _report(fatal, warn).ok is False          # ok tracks ANY fail, not blocking
    assert _report(warn).blocking is False
    assert _report(warn).ok is False
    assert _report(CheckResult("c", "C", SEVERITY_FATAL, STATUS_UNKNOWN)).ok is True
    assert _report().blocking is False and _report().ok is True


def test_enforcement_off_demotes_at_the_report_level_not_the_severity():
    """The break-glass must stay honest: the operator still sees WHICH checks are
    fatal, and enforced:false rides upstream (rule 7)."""
    fatal = CheckResult("a", "A", SEVERITY_FATAL, STATUS_FAIL)
    report = _report(fatal, enforced=False)
    assert report.blocking is False
    assert report.checks[0].severity == SEVERITY_FATAL and report.checks[0].blocking
    assert report.to_wire()["enforced"] is False
    assert report.to_upstream_wire()["enforced"] is False


def test_report_get_and_blocking_checks():
    fatal = CheckResult("a", "A", SEVERITY_FATAL, STATUS_FAIL)
    ok = CheckResult("b", "B", SEVERITY_FATAL, STATUS_PASS)
    report = _report(fatal, ok)
    assert report.get("a") is fatal and report.get("zzz") is None
    assert report.blocking_checks() == (fatal,)


def test_report_wire_carries_full_detail_and_remedy():
    """/status never leaves the box, so it gets the whole operator-facing payload."""
    check = CheckResult("a", "A", SEVERITY_FATAL, STATUS_FAIL, "detail", "remedy")
    wire = _report(check).to_wire()
    assert set(wire) == {"ok", "blocking", "enforced", "ranAt", "durationMs", "checks"}
    assert wire["ranAt"] == 1786800000.0 and wire["durationMs"] == 7
    assert wire["checks"][0]["remedy"] == "remedy"


def test_upstream_wire_omits_pass_and_skip_rows():
    report = _report(
        CheckResult("a", "A", SEVERITY_FATAL, STATUS_PASS),
        CheckResult("b", "B", SEVERITY_FATAL, STATUS_SKIP),
        CheckResult("c", "C", SEVERITY_WARN, STATUS_UNKNOWN, "d"),
        CheckResult("d", "D", SEVERITY_FATAL, STATUS_FAIL, "d"))
    assert [row["id"] for row in report.to_upstream_wire()["failed"]] == ["c", "d"]


def test_upstream_wire_never_carries_title_or_remedy():
    """Operator copy is resolved client-side from the id (§5.1) — halves the body and
    keeps the console's text under our control rather than a worker's."""
    check = CheckResult("a", "A title", SEVERITY_FATAL, STATUS_FAIL, "detail", "remedy")
    body = _report(check).to_upstream_wire()
    assert set(body) == {"ok", "blocking", "enforced", "ranAt", "failed"}
    assert set(body["failed"][0]) == {"id", "severity", "status", "detail"}
    assert "remedy" not in json.dumps(body) and "A title" not in json.dumps(body)


def test_upstream_wire_capped_at_sixteen_rows_and_two_hundred_chars():
    checks = [CheckResult(f"c{i}", "T", SEVERITY_WARN, STATUS_FAIL, "x" * 500)
              for i in range(40)]
    body = _report(*checks).to_upstream_wire()
    assert len(body["failed"]) == preflight.MAX_UPSTREAM_FAILED == 16
    assert all(len(row["detail"]) == preflight.MAX_UPSTREAM_DETAIL == 200
               for row in body["failed"])


def test_upstream_wire_is_dropped_rather_than_sent_oversized():
    """B9's rule: a diagnostic hint must never be the reason a register is rejected."""
    # Sized RELATIVE to the cap, not to a literal: the row count and the detail length are
    # both already capped, so the only way over budget is long ids — and a test that
    # hardcodes "how long" silently stops testing anything the next time the cap moves.
    pad = "check-id-number-" * (preflight.MAX_UPSTREAM_BYTES // 16)
    checks = [CheckResult(f"{pad}{i}", "T", SEVERITY_WARN, STATUS_FAIL,
                          "y" * preflight.MAX_UPSTREAM_DETAIL)
              for i in range(preflight.MAX_UPSTREAM_FAILED)]
    report = _report(*checks)
    assert report.to_upstream_wire() is None
    # ...and the un-truncated /status shape is unaffected: only the wire is capped.
    assert len(report.to_wire()["checks"]) == preflight.MAX_UPSTREAM_FAILED


def test_upstream_wire_blank_detail_becomes_null():
    body = _report(CheckResult("a", "A", SEVERITY_WARN, STATUS_FAIL, "")).to_upstream_wire()
    assert body["failed"][0]["detail"] is None


def test_log_to_errors_on_blocking_and_warns_on_everything_else():
    logger = RecordingLogger()
    _report(
        CheckResult("a", "A", SEVERITY_FATAL, STATUS_FAIL, "bad", "fix it"),
        CheckResult("b", "B", SEVERITY_WARN, STATUS_FAIL, "meh"),
        CheckResult("c", "C", SEVERITY_FATAL, STATUS_PASS, "fine"),
        CheckResult("d", "D", SEVERITY_FATAL, STATUS_SKIP, "n/a"),
    ).log_to(logger)
    assert len(logger.errors) == 1 and "bad" in logger.errors[0]
    assert "fix it" in logger.errors[0]        # the remedy is the whole point on a headless box
    assert len(logger.warnings) == 1 and "meh" in logger.warnings[0]
    # pass/skip rows are silent — the log is the operator's diagnostic, not a dump.
    assert "fine" not in "".join(logger.lines) and "n/a" not in "".join(logger.lines)


def test_log_to_shouts_every_pass_when_enforcement_is_off():
    logger = RecordingLogger()
    report = _report(CheckResult("a", "A", SEVERITY_FATAL, STATUS_FAIL, "bad", "fix"),
                     enforced=False)
    report.log_to(logger)
    assert any("PREFLIGHT ENFORCEMENT IS OFF" in line for line in logger.warnings)
    # A demoted fatal logs as a warning, not an error — it is not parking anything.
    assert logger.errors == []


# ---- check_state_dir ----

def test_state_dir_passes_and_leaves_no_probe_file(tmp_path: Path):
    target = tmp_path / "nested" / "worker-state"
    result = preflight.check_state_dir(FakeCfg(state_dir=target))
    assert (result.severity, result.status) == (SEVERITY_FATAL, STATUS_PASS)
    assert target.is_dir() and list(target.iterdir()) == []
    assert result.detail == str(target)


def test_state_dir_fails_when_the_path_is_a_file(tmp_path: Path):
    blocked = tmp_path / "not-a-dir"
    blocked.write_text("x", encoding="utf-8")
    result = preflight.check_state_dir(FakeCfg(state_dir=blocked))
    assert (result.severity, result.status) == (SEVERITY_FATAL, STATUS_FAIL)
    assert str(blocked) in result.detail and str(blocked) in result.remedy
    assert "Error" in result.detail or "error" in result.detail.lower()


def test_state_dir_fails_when_the_directory_is_read_only(tmp_path: Path):
    """The real ENOSPC/permissions shape: mkdir succeeds (exist_ok) but the 0600 probe
    write cannot land. Today this first surfaces as a crash inside ``cfg.machine_id``
    during _register — i.e. AFTER the server minted an identity."""
    target = tmp_path / "ro-state"
    target.mkdir()
    target.chmod(0o500)
    try:
        result = preflight.check_state_dir(FakeCfg(state_dir=target))
    finally:
        target.chmod(0o700)          # always restore, or tmp_path cleanup fails
    assert result.status == STATUS_FAIL and result.blocking


# ---- check_token_persistence (F9.1) ----

def test_token_persistence_passes_and_names_the_backend():
    result = preflight.check_token_persistence(
        FakeCfg(state_dir=Path(".")), _probes(env={}))
    assert (result.severity, result.status) == (SEVERITY_FATAL, STATUS_PASS)
    assert result.detail == "encrypted-file backend" and result.remedy is None


def test_token_persistence_reports_the_keyring_backend_when_selected():
    result = preflight.check_token_persistence(
        FakeCfg(state_dir=Path(".")), _probes(env={"AIZU_TOKEN_BACKEND": "KeyRing"}))
    assert result.detail == "keyring backend"


def test_token_persistence_failure_carries_the_backend_error_into_the_remedy():
    result = preflight.check_token_persistence(
        FakeCfg(state_dir=Path(".")),
        _probes(token_error="SecretCipherError: AIZU_SECRET_KEY is not set"))
    assert result.blocking is True
    assert result.detail == ("encrypted-file backend: SecretCipherError: "
                             "AIZU_SECRET_KEY is not set")
    assert "AIZU_SECRET_KEY" in result.remedy and "AIZU_TOKEN_BACKEND=keyring" in result.remedy


# ---- _default_token_roundtrip: the real mechanism, never the env-var name ----

class _FakeTokenStore:
    """Records every call so the "never overwrite a live credential" property is
    asserted on behaviour, not on a return value."""

    instances: list = []

    def __init__(self, state_dir, *, stored=None, save_raises=None,
                 first_load_raises=None, loses_writes=False):
        self.state_dir = state_dir
        self.stored = stored
        self.save_raises = save_raises
        # One-shot on purpose: the corrupt-blob case is a bad blob ALREADY on disk, and
        # the whole point of the fall-through is that a FRESH write reads back fine.
        self.first_load_raises = first_load_raises
        self.loses_writes = loses_writes
        self.saved: list = []
        self.loads = 0
        self.cleared = 0

    def save(self, token: str) -> None:
        self.saved.append(token)
        if self.save_raises:
            raise self.save_raises
        if not self.loses_writes:
            self.stored = token

    def load(self):
        self.loads += 1
        if self.first_load_raises and self.loads == 1:
            raise self.first_load_raises
        return self.stored

    def clear(self) -> None:
        self.cleared += 1
        self.stored = None


def _patch_token_store(monkeypatch, *, probe: Optional[dict] = None, **kwargs) -> list:
    """``_default_token_roundtrip`` imports TokenStore INSIDE the function, so patching
    the module attribute is what takes effect.

    TWO stores are constructed now and keeping them separately configurable is the whole
    point: ``made[0]`` is the LIVE store (``state_dir``) and ``made[1]`` — present only
    when the round-trip actually runs — is the disjoint probe store under its own
    subdirectory. ``probe=`` configures the second; every assertion on ``made[0]`` is an
    assertion that the live credential was never touched."""
    made: list = []
    probe_kwargs = dict(probe or {})

    def _factory(state_dir):
        store = _FakeTokenStore(state_dir, **(probe_kwargs if made else kwargs))
        made.append(store)
        return store

    monkeypatch.setattr("aizu.worker.token_store.TokenStore", _factory)
    return made


def test_token_roundtrip_never_overwrites_a_stored_token(monkeypatch, tmp_path: Path):
    """A token already on disk IS the proof the backend works. Writing a probe over a
    live credential would be a preflight that breaks the box it is checking."""
    made = _patch_token_store(monkeypatch, stored="a-real-worker-token")
    assert preflight._default_token_roundtrip(FakeCfg(state_dir=tmp_path)) is None
    assert len(made) == 1                 # no probe store even constructed
    assert made[0].saved == [] and made[0].cleared == 0
    assert made[0].stored == "a-real-worker-token"


def test_token_roundtrip_probes_a_disjoint_store_never_the_live_one(
        monkeypatch, tmp_path: Path):
    """F-3: the probe writes into its OWN state dir, so the live location is never
    written and never cleared — not even in the window between this probe's read and
    its cleanup, which is where a concurrent ``_register()`` lands."""
    made = _patch_token_store(monkeypatch, stored=None)
    assert preflight._default_token_roundtrip(FakeCfg(state_dir=tmp_path)) is None
    live, probe = made
    assert live.state_dir == tmp_path
    assert live.saved == [] and live.cleared == 0     # the live store was only READ
    assert probe.state_dir == tmp_path / preflight._PROBE_SUBDIR
    assert probe.saved == [preflight._PROBE_TOKEN] and probe.cleared == 1


def test_token_roundtrip_treats_a_corrupt_blob_as_recoverable(monkeypatch, tmp_path: Path):
    """A corrupt blob is not a broken backend — the sidecar already clears and
    re-registers on it. The real question is "can we write a fresh one?"."""
    made = _patch_token_store(monkeypatch,
                              first_load_raises=SecretCipherError("undecryptable"))
    result = preflight._default_token_roundtrip(FakeCfg(state_dir=tmp_path))
    assert result is None            # the round-trip answered, despite the bad load
    assert made[1].saved == [preflight._PROBE_TOKEN]
    assert made[0].saved == [] and made[0].cleared == 0


def test_token_roundtrip_reports_a_backend_that_loses_writes(monkeypatch, tmp_path: Path):
    _patch_token_store(monkeypatch, stored=None, probe={"loses_writes": True})
    result = preflight._default_token_roundtrip(FakeCfg(state_dir=tmp_path))
    assert "did not read back" in result


def test_token_roundtrip_reports_a_save_failure_by_type_name(monkeypatch, tmp_path: Path):
    _patch_token_store(
        monkeypatch, stored=None,
        probe={"save_raises": SecretCipherError("AIZU_SECRET_KEY is not set")})
    result = preflight._default_token_roundtrip(FakeCfg(state_dir=tmp_path))
    assert result.startswith("SecretCipherError: ")


def test_token_roundtrip_never_deletes_a_blob_it_could_not_write_over(
        monkeypatch, tmp_path: Path):
    """THE credential-destruction guard. A box whose AIZU_SECRET_KEY went missing has a
    perfectly good token blob it simply cannot decrypt right now: load() raises, save()
    raises at the cipher BEFORE touching the file, and an unconditional cleanup would
    then unlink the intact blob. That turns "set the key back and it works" into a
    hand-minted enrolment token and an operator visit (B10)."""
    made = _patch_token_store(
        monkeypatch,
        stored="a-real-worker-token",
        first_load_raises=SecretCipherError("AIZU_SECRET_KEY is not set"),
        probe={"save_raises": SecretCipherError("AIZU_SECRET_KEY is not set")})
    result = preflight._default_token_roundtrip(FakeCfg(state_dir=tmp_path))
    assert result.startswith("SecretCipherError: ")   # still caught, still named (F9.1)
    assert made[0].cleared == 0                       # ...and the credential survived
    assert made[0].stored == "a-real-worker-token"


def test_token_roundtrip_cannot_clobber_a_token_a_register_persisted_mid_probe(
        monkeypatch, tmp_path: Path):
    """F-3, the race itself, against the REAL FernetFileBackend.

    ``request_preflight`` runs this on a DETACHED thread while a human works the wizard,
    so a ``_register()`` can persist the real token at any instant. This models the one
    interleaving that mattered: the register lands after the probe has written, i.e.
    inside the window the shipped ``wrote_probe`` guard did not cover. Sharing the store
    location makes the probe's cleanup delete a credential that is minutes old — and
    recovery from that is a hand-minted enrolment token plus an operator visit (B10)."""
    monkeypatch.delenv("AIZU_TOKEN_BACKEND", raising=False)
    monkeypatch.setenv("AIZU_SECRET_KEY", SecretCipher.generate_key())
    from aizu.worker import token_store as ts

    real_store_cls = ts.TokenStore
    registered: list = []

    class _RacingStore(real_store_cls):
        def save(self, token: str) -> None:
            super().save(token)
            if token == preflight._PROBE_TOKEN and not registered:
                registered.append(True)
                real_store_cls(tmp_path).save("a-real-worker-token")

    monkeypatch.setattr(ts, "TokenStore", _RacingStore)
    assert preflight._default_token_roundtrip(FakeCfg(state_dir=tmp_path)) is None
    assert registered == [True]                            # the race really happened
    assert real_store_cls(tmp_path).load() == "a-real-worker-token"
    assert not (tmp_path / preflight._PROBE_SUBDIR).exists()


def test_token_roundtrip_against_a_real_blob_survives_a_missing_key(
        monkeypatch, tmp_path: Path):
    """The same property against the REAL FernetFileBackend, since the ordering that
    makes it safe (encrypt-then-open) lives in that backend, not in the probe."""
    monkeypatch.delenv("AIZU_TOKEN_BACKEND", raising=False)
    monkeypatch.setenv("AIZU_SECRET_KEY", SecretCipher.generate_key())
    from aizu.worker.token_store import TokenStore
    TokenStore(tmp_path).save("a-real-worker-token")
    blob = (tmp_path / "worker-token.enc").read_bytes()

    monkeypatch.delenv("AIZU_SECRET_KEY", raising=False)
    failure = preflight._default_token_roundtrip(FakeCfg(state_dir=tmp_path))
    assert failure and "AIZU_SECRET_KEY" in failure
    assert (tmp_path / "worker-token.enc").read_bytes() == blob


def test_token_roundtrip_reports_an_unconstructable_store(monkeypatch, tmp_path: Path):
    """A bad AIZU_TOKEN_BACKEND lands here — the constructor, not the save."""
    def _boom(state_dir):
        raise ValueError("unknown token backend 'nope'")
    monkeypatch.setattr("aizu.worker.token_store.TokenStore", _boom)
    result = preflight._default_token_roundtrip(FakeCfg(state_dir=tmp_path))
    assert result == "ValueError: unknown token backend 'nope'"


def test_token_roundtrip_against_the_real_fernet_file_backend(monkeypatch, tmp_path: Path):
    """The F9.1 mechanism itself: a real 0600 encrypted-file round-trip with, and then
    without, AIZU_SECRET_KEY. No keychain is ever touched (AIZU_TOKEN_BACKEND unset =
    'auto' = always the file backend)."""
    monkeypatch.delenv("AIZU_TOKEN_BACKEND", raising=False)
    monkeypatch.setenv("AIZU_SECRET_KEY", SecretCipher.generate_key())
    cfg = FakeCfg(state_dir=tmp_path)
    assert preflight._default_token_roundtrip(cfg) is None
    assert not (tmp_path / "worker-token.enc").exists()   # never written where it counts
    assert not (tmp_path / preflight._PROBE_SUBDIR).exists()   # the probe cleaned up

    monkeypatch.delenv("AIZU_SECRET_KEY", raising=False)
    failure = preflight._default_token_roundtrip(cfg)
    assert failure and "AIZU_SECRET_KEY" in failure


# ---- check_dispatch_credential ----

@pytest.mark.parametrize("token_ok,present,bootstrap,expected", [
    (True, True, None, STATUS_PASS),        # a token really is stored
    (False, None, "boot-token", STATUS_PASS),   # first register is still possible
    (True, True, "boot-token", STATUS_PASS),
    (True, False, None, STATUS_FAIL),       # the F-11 case: register WILL 401 forever
    (False, False, None, STATUS_UNKNOWN),   # the store is broken — token_persistence owns that
    (True, None, None, STATUS_UNKNOWN),
])
def test_dispatch_credential_accepts_either_credential(token_ok, present, bootstrap,
                                                       expected):
    result = preflight.check_dispatch_credential(
        FakeCfg(state_dir=Path("."), bootstrap_token=bootstrap),
        _probes(token_present=present), token_ok=token_ok)
    assert result.status == expected
    assert result.severity == SEVERITY_WARN and result.blocking is False


def test_dispatch_credential_fires_on_a_brand_new_box_with_no_credential():
    """F-11: the check that could not fire. A fresh box has an EMPTY store, which
    round-trips a probe token perfectly, so ``token_ok`` is True — gating the "no
    credential" branch on it made the branch reachable only when ``token_persistence``
    had already failed (which is fatal on its own), i.e. never on the one box shape this
    check exists for. Presence is read from the store instead."""
    calls: dict = {}
    result = preflight.check_dispatch_credential(
        FakeCfg(state_dir=Path("."), bootstrap_token=None),
        _probes(token_present=False, calls=calls), token_ok=True)
    assert result.status == STATUS_FAIL
    assert result.detail.endswith("no stored worker token and no bootstrap token")
    assert "Fleet → Add worker" in result.remedy
    assert len(calls["present"]) == 1


@pytest.mark.parametrize("status,extra", [
    (STATUS_FAIL, {"token_present": False}),
    (STATUS_PASS, {"token_present": True}),
])
def test_dispatch_credential_redacts_userinfo_from_the_url_it_publishes(status, extra):
    """This module's docstring promises no secret VALUES in a detail, a log line or a
    wire field — and a detail is published all three ways (control surface, sidecar log,
    and ``to_upstream_wire()`` into the cloud fleet console). A dispatch URL may
    legitimately carry userinfo, so the password must never survive into the row on
    EITHER branch. Fails if ``_redact_userinfo`` is dropped from ``check_dispatch_credential``."""
    result = preflight.check_dispatch_credential(
        FakeCfg(state_dir=Path("."), bootstrap_token=None,
                dispatch_base_url="https://svc:hunter2@dispatch.example.com:8443"),
        _probes(**extra), token_ok=True)
    assert result.status == status
    assert "hunter2" not in result.detail and "svc" not in result.detail
    # Redacted, not deleted: the host still has to be readable or the row is useless.
    assert "dispatch.example.com:8443" in result.detail


def test_dispatch_credential_leaves_an_ordinary_url_byte_identical():
    """Redaction must not rewrite the common case — an operator comparing this row
    against their config should see exactly what they set."""
    url = "https://dispatch.example.com/base"
    result = preflight.check_dispatch_credential(
        FakeCfg(state_dir=Path("."), bootstrap_token="tok", dispatch_base_url=url),
        _probes(), token_ok=True)
    assert result.detail.startswith(url + " ")


def test_redact_userinfo_never_raises_on_a_malformed_url():
    """A detail string is never worth an exception: a redaction helper that throws would
    take the whole check down with it, and check failures are only warn-level."""
    assert preflight._redact_userinfo("http://[oops") == "<redacted>"
    assert preflight._redact_userinfo("") == ""


def test_dispatch_credential_never_reads_the_store_when_a_bootstrap_token_exists():
    """A bootstrap token settles it without touching the credential at all — and the
    detail names the VARIABLE, never the value."""
    calls: dict = {}
    result = preflight.check_dispatch_credential(
        FakeCfg(state_dir=Path("."), bootstrap_token="LEAKED-BOOTSTRAP-VALUE"),
        _probes(calls=calls), token_ok=False)
    assert result.status == STATUS_PASS and calls["present"] == []
    assert "AIZU_WORKER_BOOTSTRAP_TOKEN" in result.detail
    assert "LEAKED-BOOTSTRAP-VALUE" not in json.dumps(result.to_wire())


@pytest.mark.parametrize("url", ["", "not-a-url", "ftp://host", "://broken"])
def test_dispatch_credential_rejects_a_non_http_url(url):
    result = preflight.check_dispatch_credential(
        FakeCfg(state_dir=Path("."), dispatch_base_url=url), _probes(), token_ok=True)
    assert result.status == STATUS_FAIL and "AIZU_DISPATCH_URL" in result.detail


def test_default_token_present_reads_without_writing(monkeypatch, tmp_path: Path):
    """The presence probe is strictly read-only: no save, no clear, and an unreadable
    store answers None ("cannot tell") rather than False ("no credential")."""
    monkeypatch.delenv("AIZU_TOKEN_BACKEND", raising=False)
    monkeypatch.setenv("AIZU_SECRET_KEY", SecretCipher.generate_key())
    cfg = FakeCfg(state_dir=tmp_path)
    assert preflight._default_token_present(cfg) is False

    from aizu.worker.token_store import TokenStore
    TokenStore(tmp_path).save("a-real-worker-token")
    blob = (tmp_path / "worker-token.enc").read_bytes()
    assert preflight._default_token_present(cfg) is True
    assert (tmp_path / "worker-token.enc").read_bytes() == blob

    monkeypatch.delenv("AIZU_SECRET_KEY", raising=False)
    assert preflight._default_token_present(cfg) is None


# ---- check_capabilities (F9.2) ----

def test_capabilities_passes_and_lists_the_platforms():
    result = preflight.check_capabilities(
        FakeCfg(state_dir=Path("."), capabilities=(X, IG, IG)), _probes())
    assert (result.severity, result.status) == (SEVERITY_FATAL, STATUS_PASS)
    assert result.detail == "instagram, x"        # sorted + deduped


@pytest.mark.parametrize("env,fragment", [
    ({}, "neither AIZU_WORKER_PLATFORMS nor AIZU_WORKER_CAPABILITIES is set"),
    ({"AIZU_WORKER_CAPABILITIES": "[[1,'oops',null]]"},
     "AIZU_WORKER_CAPABILITIES is set but did not parse"),
    ({"AIZU_WORKER_PLATFORMS": "myspace"},
     "AIZU_WORKER_PLATFORMS is set but named no supported platform"),
])
def test_capabilities_separates_the_two_indistinguishable_causes(env, fragment):
    """"Nothing set" and "set but the tolerant parser dropped every entry" need
    different fixes, and cfg alone cannot tell them apart — hence the raw env read,
    FOR DIAGNOSIS ONLY."""
    result = preflight.check_capabilities(FakeCfg(state_dir=Path(".")), _probes(env=env))
    assert result.blocking is True and fragment in result.detail
    assert "AIZU_WORKER_PLATFORMS=all" in result.remedy


# ---- check_llm_backend (F9.3) ----

@pytest.mark.parametrize("env,status,detail", [
    ({"AIZU_LLM_BASE_URL": "http://localhost:11434"}, STATUS_PASS,
     "AIZU_LLM_BASE_URL is set"),
    ({"OPENROUTER_API_KEY": "sk-x"}, STATUS_PASS, "OPENROUTER_API_KEY is set"),
    ({"AIZU_LLM_BASE_URL": "   "}, STATUS_FAIL, "neither"),
    ({}, STATUS_FAIL, "neither"),
])
def test_llm_backend_accepts_a_local_endpoint_or_a_cloud_key(env, status, detail):
    result = preflight.check_llm_backend(FakeCfg(state_dir=Path(".")), _probes(env=env))
    assert result.status == status and detail in result.detail
    assert result.severity == SEVERITY_FATAL


def test_llm_backend_is_demoted_to_warn_only_on_a_declared_warming_only_box():
    """``_build_warming_io`` needs no LLM — parking a box that only ever warms would be
    a fabricated failure. The declaration is per box and explicit."""
    result = preflight.check_llm_backend(
        FakeCfg(state_dir=Path(".")), _probes(env={preflight.WARMING_ONLY_ENV: "1"}))
    assert result.severity == SEVERITY_WARN and result.status == STATUS_FAIL
    assert result.blocking is False and "warning only" in result.detail


def test_llm_backend_stays_fatal_on_a_box_that_merely_has_warming_enabled():
    """F-12. ``AIZU_WARMING_ENABLED`` is the GLOBAL layer-1 warming hard-stop — "warming
    is permitted", never "this box declines harvest work" — and a box that warms AND
    leases harvest jobs has every reason to set it. Demoting on it put the F9.3
    pathology back: every live job dead-letters at attempt 5, with the real cause never
    leaving the machine, behind an amber row nobody reads."""
    result = preflight.check_llm_backend(
        FakeCfg(state_dir=Path(".")), _probes(env={"AIZU_WARMING_ENABLED": "1"}))
    assert result.severity == SEVERITY_FATAL and result.status == STATUS_FAIL
    assert result.blocking is True
    assert preflight.WARMING_ONLY_ENV in result.remedy   # ...with the way out named


@pytest.mark.parametrize("value,warn", [
    ("1", True), ("true", True), (" on ", True),
    ("", False), ("0", False), ("no", False), ("maybe", False),
])
def test_llm_backend_demotion_needs_an_affirmative_declaration(value, warn):
    """Fail safe: anything that is not an explicit yes leaves the check fatal."""
    result = preflight.check_llm_backend(
        FakeCfg(state_dir=Path(".")), _probes(env={preflight.WARMING_ONLY_ENV: value}))
    assert (result.severity == SEVERITY_WARN) is warn


def test_llm_backend_never_echoes_the_key():
    result = preflight.check_llm_backend(
        FakeCfg(state_dir=Path(".")), _probes(env={"OPENROUTER_API_KEY": "sk-leak-me"}))
    assert "sk-leak-me" not in json.dumps(result.to_wire())


@pytest.mark.parametrize("env", [
    {}, {"OPENROUTER_API_KEY": "sk-x"}, {"AIZU_LLM_BASE_URL": "http://localhost:11434"},
    {"AIZU_LLM_BASE_URL": "  "}, {"AIZU_LLM_BASE_URL": " ", "OPENROUTER_API_KEY": "sk-x"},
])
def test_llm_predicate_matches_cli_build_run_io(monkeypatch, env):
    """The preflight must never disagree with the runtime it is predicting. Drives the
    REAL ``cli._build_run_io`` over the same env matrix and asserts it raises its
    "No LLM backend configured" RuntimeError exactly when this check fails."""
    from aizu import cli

    for name in ("AIZU_LLM_BASE_URL", "OPENROUTER_API_KEY"):
        monkeypatch.delenv(name, raising=False)
    for name, value in env.items():
        monkeypatch.setenv(name, value)

    check_fails = preflight.check_llm_backend(
        FakeCfg(state_dir=Path(".")), _probes(env=env)).status == STATUS_FAIL

    # campaign/store/args are only reached AFTER the LLM guard, so None is fine here:
    # we assert on which error comes out, never on a successful build.
    try:
        cli._build_run_io(None, None, False, None, "harvest")
        cli_fails = False
    except RuntimeError as e:
        cli_fails = "No LLM backend configured" in str(e)
    except Exception:  # noqa: BLE001 — it got PAST the LLM guard and died later
        cli_fails = False
    assert check_fails is cli_fails


# ---- check_playwright ----

def test_playwright_skips_on_an_api_only_box():
    result = preflight.check_playwright(
        FakeCfg(state_dir=Path("."), capabilities=(YT,)), _probes(playwright=False))
    assert result.status == STATUS_SKIP and result.blocking is False


@pytest.mark.parametrize("available,status", [(True, STATUS_PASS), (False, STATUS_FAIL)])
def test_playwright_reports_importability_as_a_warning(available, status):
    result = preflight.check_playwright(
        FakeCfg(state_dir=Path("."), capabilities=(IG,)),
        _probes(playwright=available))
    assert result.status == status
    assert result.severity == SEVERITY_WARN and result.blocking is False


# ---- check_cdp_reachable / check_cdp_port_drift (F10) — both pure ----

def test_cdp_checks_skip_on_an_api_only_box():
    cfg = FakeCfg(state_dir=Path("."), capabilities=(YT,))
    for result in (preflight.check_cdp_reachable(cfg, cdp_state="unreachable",
                                                 alternate_alive=None),
                   preflight.check_cdp_port_drift(cfg, cdp_state="unreachable",
                                                  alternate_alive=None)):
        assert result.status == STATUS_SKIP and result.blocking is False


def test_cdp_reachable_passes_when_the_configured_port_answers():
    cfg = FakeCfg(state_dir=Path("."), capabilities=(IG,), cdp_url=CDP_9333)
    result = preflight.check_cdp_reachable(cfg, cdp_state="ok", alternate_alive=None)
    assert result.status == STATUS_PASS and result.detail == CDP_9333


def test_cdp_reachable_names_the_sibling_port_that_actually_answered():
    """The whole F10 fix for an operator who provisioned Chrome per the warming runbook
    (9333) while this sidecar was configured for 9222 — one sentence instead of "start
    Chrome" on a machine where Chrome is already running."""
    cfg = FakeCfg(state_dir=Path("."), capabilities=(IG,), cdp_url=CDP_9222)
    result = preflight.check_cdp_reachable(cfg, cdp_state="unreachable",
                                           alternate_alive=CDP_9333)
    assert result.blocking is True
    assert result.detail == "Chrome is on 9333 but this worker is configured for 9222"
    assert CDP_9222 in result.remedy


def test_cdp_reachable_fails_plainly_when_nothing_answers_anywhere():
    cfg = FakeCfg(state_dir=Path("."), capabilities=(IG,), cdp_url=CDP_9333)
    result = preflight.check_cdp_reachable(cfg, cdp_state="unreachable",
                                           alternate_alive=None)
    assert result.blocking is True and result.detail == f"nothing answers CDP at {CDP_9333}"


def test_cdp_reachable_shows_the_adoption_receipt_when_it_passes_on_a_sibling():
    cfg = FakeCfg(state_dir=Path("."), capabilities=(IG,), cdp_url=CDP_9222,
                  cdp_url_drift_note="adopted 9222: ...")
    result = preflight.check_cdp_reachable(cfg, cdp_state="ok", alternate_alive=None)
    assert result.status == STATUS_PASS and result.detail == "adopted 9222: ..."


def test_cdp_port_drift_is_red_when_the_sibling_is_the_live_one():
    cfg = FakeCfg(state_dir=Path("."), capabilities=(IG,), cdp_url=CDP_9222)
    result = preflight.check_cdp_port_drift(cfg, cdp_state="unreachable",
                                            alternate_alive=CDP_9333)
    assert result.severity == SEVERITY_WARN and result.status == STATUS_FAIL
    assert result.blocking is False              # cdp_reachable already blocks on this
    assert CDP_9333 in result.detail and 'Use 9333' in result.remedy


def test_cdp_port_drift_is_red_after_an_auto_adoption():
    """The second drift shape: the box works, but only because probe order happened to
    save it. Pin the port or the next relaunch is a coin flip."""
    cfg = FakeCfg(state_dir=Path("."), capabilities=(IG,), cdp_url=CDP_9333,
                  cdp_url_drift_note="adopted http://127.0.0.1:9333: ...")
    result = preflight.check_cdp_port_drift(cfg, cdp_state="ok", alternate_alive=None)
    assert result.status == STATUS_FAIL and 'Use 9333' in result.remedy


def test_cdp_port_drift_passes_on_a_plain_healthy_box():
    cfg = FakeCfg(state_dir=Path("."), capabilities=(IG,), cdp_url=CDP_9333)
    result = preflight.check_cdp_port_drift(cfg, cdp_state="ok", alternate_alive=None)
    assert result.status == STATUS_PASS


# ---- check_chrome_profile: the profile left behind by the per-brand split ----
#
# Profiles are now `<base>/<brand>` (chrome_manager.profile_dir_for), so two browsers can
# never open one directory and there is no brand conflict left to report. What the split
# leaves is the profile a box warmed BEFORE it — a `Default/` sitting directly in the base.
# It is inert and untouched, and it is also the entire explanation for a box that was
# signed in last week and now is not. On these PCs nobody is watching a log (F12), so the
# explanation has to ride the wire or it has not been given.


def _base(tmp_path: Path, *, legacy: bool = False, split_brand: Optional[str] = None,
          name: str = "cft-profile") -> Path:
    """A profile base. ``legacy`` puts a pre-split `Default/` directly in it;
    ``split_brand`` puts one in the NEW place, `<base>/<brand>/Default`."""
    base = tmp_path / name
    base.mkdir(parents=True, exist_ok=True)
    if legacy:
        (base / "Default").mkdir(exist_ok=True)
    if split_brand is not None:
        (base / split_brand / "Default").mkdir(parents=True, exist_ok=True)
    return base


def _profile_check(tmp_path: Path, *, capabilities=(IG,), **probe_kw):
    cfg = FakeCfg(state_dir=tmp_path / "state", capabilities=capabilities)
    return preflight.check_chrome_profile(cfg, _probes(**probe_kw))


def test_profile_row_skips_on_an_api_only_box(tmp_path: Path):
    result = _profile_check(tmp_path, capabilities=(YT,),
                            chrome_profile=_base(tmp_path, legacy=True))
    assert result.status == STATUS_SKIP


def test_profile_row_skips_when_this_box_manages_no_chrome_profile(tmp_path: Path):
    """A desktop-shell box: the shell owns its own profile elsewhere and never tells the
    sidecar where. Inventing a path would report on a directory this box never touches."""
    result = _profile_check(tmp_path, chrome_profile=None)
    assert result.status == STATUS_SKIP


def test_profile_row_skips_when_the_configured_base_is_not_on_disk_yet(tmp_path: Path):
    result = _profile_check(tmp_path, chrome_profile=tmp_path / "never-created")
    assert result.status == STATUS_SKIP


def test_profile_row_passes_on_a_base_with_nothing_left_behind(tmp_path: Path):
    result = _profile_check(tmp_path, chrome_profile=_base(tmp_path))
    assert result.status == STATUS_PASS


def test_profile_row_passes_on_the_new_layout_however_warm_it_is(tmp_path: Path):
    """`Default/` one level down is the split working correctly. Reading that as "legacy"
    would nag every healthy box forever, which is how an amber row stops being read."""
    result = _profile_check(tmp_path,
                            chrome_profile=_base(tmp_path, split_brand="chrome-for-testing"))
    assert result.status == STATUS_PASS


def test_profile_row_names_both_per_brand_directories_when_it_passes(tmp_path: Path):
    """The row is the only place an operator learns where their logins now live."""
    base = _base(tmp_path)
    result = _profile_check(tmp_path, chrome_profile=base)
    assert str(base / "chrome-for-testing") in result.detail
    assert re.search(re.escape(str(base / "chrome")) + r"(?![-\w])", result.detail)


def test_profile_row_reports_a_pre_split_profile_at_the_base(tmp_path: Path):
    base = _base(tmp_path, legacy=True)
    result = _profile_check(tmp_path, chrome_profile=base)
    assert result.status == STATUS_FAIL
    assert str(base) in result.detail
    assert "left untouched" in result.detail
    assert str(base / "chrome-for-testing") in result.remedy
    # Bounded: `<base>/chrome-for-testing` contains `<base>/chrome`, so a plain `in` would
    # pass on a remedy that names only one of the two destinations — the guess we forbid.
    assert re.search(re.escape(str(base / "chrome")) + r"(?![-\w])", result.remedy)


def test_profile_row_is_never_fatal_in_any_state(tmp_path: Path):
    """A leftover directory sitting still is not a reason to park a box that leases,
    attaches and runs perfectly — and it would park it FOREVER, because nothing clears it
    but a human decision we are not allowed to make for them."""
    for base in (None,
                 tmp_path / "missing",
                 _base(tmp_path, name="clean"),
                 _base(tmp_path, legacy=True, name="legacy"),
                 _base(tmp_path, split_brand="chrome", name="split")):
        result = _profile_check(tmp_path, chrome_profile=base)
        assert result.severity == SEVERITY_WARN
        assert result.blocking is False


def test_profile_row_survives_a_probe_that_raises(tmp_path: Path):
    cfg = FakeCfg(state_dir=tmp_path / "state", capabilities=(IG,))
    probes = dataclasses.replace(
        _probes(), chrome_profile_base=lambda: (_ for _ in ()).throw(OSError("nope")))
    assert preflight.check_chrome_profile(cfg, probes).status == STATUS_SKIP


def test_profile_row_never_touches_the_profile_it_reports_on(tmp_path: Path):
    """Never moved, copied, renamed, deleted or opened — the cookies in there are the
    operator's, and guessing at them is what three earlier rounds got wrong."""
    base = _base(tmp_path, legacy=True)
    (base / "Default" / "Cookies").write_bytes(b"warmed-by-someone")
    _profile_check(tmp_path, chrome_profile=base)
    assert sorted(p.name for p in base.iterdir()) == ["Default"]
    assert (base / "Default" / "Cookies").read_bytes() == b"warmed-by-someone"


def test_the_legacy_row_publishes_the_same_paragraph_the_launch_logs(tmp_path: Path):
    """Drift guard, and a strict one: it is the SAME string, not two texts that agree
    today. This paragraph tells an operator where to move a directory holding live logins,
    and a second copy of it is a second chance to name a different destination."""
    from aizu.worker import chrome_manager

    base = _base(tmp_path, legacy=True)
    result = _profile_check(tmp_path, chrome_profile=base)
    assert result.remedy == chrome_manager.legacy_profile_note(base)


def test_the_profile_row_inspects_the_directory_the_shipped_launcher_warms(monkeypatch):
    """CARRY-OVER 1. The probe used to resolve AIZU_CHROME_PROFILE_DIR defaulting to
    ~/.aizu-chrome-profile, while `scripts/warm_chrome.sh` warms AIZU_CHROME_PROFILE
    defaulting to ~/.aizu-cft-profile — a different name AND a different default, so the
    row reported on a directory nothing on the box ever wrote. A row about a directory
    nothing warms is worse than no row at all."""
    monkeypatch.delenv("AIZU_CHROME_PROFILE", raising=False)
    monkeypatch.setenv("AIZU_CHROME_PROFILE_DIR", "/data/retired-spelling")
    assert preflight._default_chrome_profile_base() == str(
        Path.home() / ".aizu-cft-profile")

    monkeypatch.setenv("AIZU_CHROME_PROFILE", "/data/prof")
    assert preflight._default_chrome_profile_base() == "/data/prof"


def test_the_profile_row_reaches_the_fleet_console_not_just_the_log(tmp_path: Path):
    """The whole reason the row exists: nobody can SSH into these PCs, so a leftover
    profile that only ever appears as a log line on the box has not been reported at all.
    It has to survive BOTH wire shapes — the local control surface and the compact
    register body."""
    cfg = FakeCfg(state_dir=tmp_path / "state", capabilities=(IG,))
    base = _base(tmp_path, legacy=True)
    report = preflight.run_preflight(cfg, probes=_probes(
        env=_green_env(), cdp={CDP_9333: "ok"}, chrome_profile=base))
    row = report.get("chrome_profile")
    assert row.status == STATUS_FAIL

    local = [c for c in report.to_wire()["checks"] if c["id"] == "chrome_profile"]
    assert local and str(base / "chrome-for-testing") in local[0]["remedy"]

    upstream = report.to_upstream_wire()
    assert "chrome_profile" in [f["id"] for f in upstream["failed"]]
    # ...and the bridge must actually accept the id, or the row is dropped on arrival.
    from aizu.server import _validate_preflight_summary
    kept = _validate_preflight_summary(upstream)
    assert "chrome_profile" in [f["id"] for f in kept["failed"]]


# ---- check_browser: cdp_attachable (B6/D3) + login.* ----

def _browser_ids(results) -> list:
    return [r.id for r in results]


def test_browser_skips_entirely_on_an_api_only_box():
    results = preflight.check_browser(
        FakeCfg(state_dir=Path("."), capabilities=(YT,)), _probes(),
        cdp_ok=False, platforms=())
    assert _browser_ids(results) == ["cdp_attachable"]
    assert results[0].status == STATUS_SKIP and results[0].blocking is False


def test_browser_never_opens_a_second_cdp_connection_mid_job():
    """The single-browser invariant: a live run already owns the one CDP connection
    this architecture allows. A manual re-check from the desktop UI must not break it."""
    calls: dict = {}
    results = preflight.check_browser(
        FakeCfg(state_dir=Path("."), capabilities=(IG,)),
        _probes(run_active=True, calls=calls), cdp_ok=True, platforms=("instagram",))
    assert [r.status for r in results] == [STATUS_SKIP, STATUS_SKIP]
    assert calls["browser"] == []
    assert all(r.remedy is None for r in results)   # nothing for the operator to do


def test_browser_is_skipped_but_logins_are_unknown_when_cdp_is_down():
    calls: dict = {}
    results = preflight.check_browser(
        FakeCfg(state_dir=Path("."), capabilities=(IG,)), _probes(calls=calls),
        cdp_ok=False, platforms=("instagram",))
    assert results[0].status == STATUS_SKIP and results[0].blocking is False
    assert results[1].id == "login.instagram" and results[1].status == STATUS_UNKNOWN
    assert calls["browser"] == []


def test_browser_is_unknown_never_fail_when_playwright_is_missing():
    """A packaging problem is reported by its OWN check; darkening the box here would
    park it for a condition it cannot self-diagnose."""
    results = preflight.check_browser(
        FakeCfg(state_dir=Path("."), capabilities=(IG,)), _probes(playwright=False),
        cdp_ok=True, platforms=("instagram",))
    assert results[0].status == STATUS_UNKNOWN and results[0].blocking is False


def test_browser_is_unknown_on_readiness_version_skew():
    """A partially-upgraded install whose readiness module has no probe_browser."""
    probe = BrowserProbe(attached=False, error=preflight._PROBE_UNAVAILABLE)
    results = preflight.check_browser(
        FakeCfg(state_dir=Path("."), capabilities=(IG,)), _probes(browser=probe),
        cdp_ok=True, platforms=("instagram",))
    assert results[0].status == STATUS_UNKNOWN and results[0].blocking is False
    assert "readiness module" in results[0].detail


def test_browser_fails_fatally_on_the_degraded_chrome_case():
    """B6/D3: HTTP 200 on /json/version proves a socket. This proves the browser will
    actually talk to Playwright — the exact condition under which every job nacks on a
    box that looks perfect."""
    probe = BrowserProbe(attached=False, error="TargetClosedError")
    results = preflight.check_browser(
        FakeCfg(state_dir=Path("."), capabilities=(IG,), cdp_url=CDP_9333),
        _probes(browser=probe), cdp_ok=True, platforms=("instagram",))
    attach = results[0]
    assert attach.severity == SEVERITY_FATAL and attach.blocking is True
    assert "TargetClosedError" in attach.detail and "degraded" in attach.remedy
    assert results[1].status == STATUS_UNKNOWN          # a login is unknowable now


@pytest.mark.parametrize("error", ["ReadinessTimeout", "TimeoutError",
                                   "PlaywrightTimeout", "timeout"])
def test_browser_is_unknown_never_fatal_when_the_attach_only_ran_out_of_clock(error):
    """F-1. A timeout and a refusal are different facts. Our attach budget is far
    tighter than the harvest run's (core/cdp.py gives it nav_timeout+10s and calls those
    budgets generous "for a slow worker PC"), so a fatal here says "this box cannot
    work" about a box on which the job would have run — a cold Playwright driver spawn
    behind Windows AV clears five seconds routinely. unknown never blocks; the 30s
    re-probe settles it."""
    results = preflight.check_browser(
        FakeCfg(state_dir=Path("."), capabilities=(IG,), cdp_url=CDP_9333),
        _probes(browser=BrowserProbe(attached=False, error=error)),
        cdp_ok=True, platforms=("instagram",))
    attach = results[0]
    assert attach.status == STATUS_UNKNOWN and attach.blocking is False
    assert error in attach.detail and "did not finish" in attach.detail
    assert "re-checks every 30s" in attach.remedy
    assert results[1].status == STATUS_UNKNOWN


def test_browser_still_fails_fatally_on_an_error_that_is_not_a_timeout():
    """The demotion is narrow on purpose: an unrecognised error name stays FATAL, so a
    rename upstream loses the demotion rather than silently unblocking a dead box."""
    results = preflight.check_browser(
        FakeCfg(state_dir=Path("."), capabilities=(IG,), cdp_url=CDP_9333),
        _probes(browser=BrowserProbe(attached=False, error="TimedOutOnce")),
        cdp_ok=True, platforms=("instagram",))
    assert results[0].blocking is True


def test_the_fatal_budget_matches_readinesss_contract():
    """F-1. ``probe_browser``'s contract is explicit: a caller that treats
    ``attached=False`` as FATAL must pass ``ATTACH_FATAL_BUDGET_SEC``. Preflight is THE
    fatal caller — ``cdp_attachable`` is the check that parks the box — so anything
    smaller puts the gate back to being stricter than the job it gates.

    The literal is duplicated rather than imported (F-9: importing readiness at module
    scope drags Playwright into every sidecar), so this is the drift guard for it."""
    from aizu import readiness
    assert preflight._BROWSER_TIMEOUT_SEC == readiness.ATTACH_FATAL_BUDGET_SEC


def test_the_default_browser_probe_hands_readiness_ONE_budget_not_a_pre_split_one(
        monkeypatch):
    """Splitting attach-vs-reads belongs to readiness (``_attach_timeout_sec``) and to it
    alone. Preflight briefly pre-split the budget too, and two splits compound: 30s outer
    became a 12s "attach" that readiness then split again down to 7.2s — the same
    starvation F-1 is about, reached from the other side. Passing ``read_state`` would
    also skip ``probe_browser``'s PlaywrightUnavailable branch, whose "unknown, not
    broken" verdict is exactly what preflight needs on an unpackaged box."""
    seen: dict = {}

    def _probe_browser(url, platforms, timeout, *, read_state=None):
        seen["outer"] = timeout
        seen["has_read_state"] = read_state is not None
        return BrowserProbe(attached=True, logins={p: "logged_in" for p in platforms})

    from aizu import readiness
    monkeypatch.setattr(readiness, "probe_browser", _probe_browser)
    monkeypatch.setattr(readiness, "PLAYWRIGHT_AVAILABLE", True)

    probe = preflight._default_browser_probe(CDP_9333, ("instagram",))
    assert probe.attached is True
    assert seen["has_read_state"] is False
    assert seen["outer"] == preflight._BROWSER_TIMEOUT_SEC


def test_readiness_splits_the_budget_it_is_given_leaving_room_after_the_attach():
    """The other end of the same contract: preflight hands over one number precisely
    BECAUSE readiness reserves part of it for contexts/pages/cookies. If readiness ever
    stopped splitting, one number for both would starve the reads again."""
    from aizu import readiness
    budget = preflight._BROWSER_TIMEOUT_SEC
    attach = readiness._attach_timeout_sec(budget)
    assert 0.0 < attach < budget
    # ...and the reserve is real time, not a rounding artefact.
    assert budget - attach >= 1.0


def test_the_default_browser_probe_leaves_the_missing_playwright_verdict_to_readiness(
        monkeypatch):
    """``probe_browser``'s PlaywrightUnavailable branch reports "unknown, not broken",
    which is what preflight needs on an unpackaged box — so preflight must let it run."""
    def _probe_browser(url, platforms, timeout, *, read_state=None):
        return BrowserProbe(attached=False, error="PlaywrightUnavailable")

    from aizu import readiness
    monkeypatch.setattr(readiness, "probe_browser", _probe_browser)
    monkeypatch.setattr(readiness, "PLAYWRIGHT_AVAILABLE", False)
    assert preflight._default_browser_probe(CDP_9333, ("instagram",)).attached is False


def test_the_default_browser_probe_works_against_a_probe_browser_without_the_seam(
        monkeypatch):
    """Version skew: an older ``probe_browser`` taking no ``read_state`` keyword must
    still be callable, not reported as a broken box."""
    def _probe_browser(url, platforms, timeout):
        return BrowserProbe(attached=True, logins={"instagram": "logged_in"})

    from aizu import readiness
    monkeypatch.setattr(readiness, "probe_browser", _probe_browser)
    monkeypatch.setattr(readiness, "PLAYWRIGHT_AVAILABLE", True)
    probe = preflight._default_browser_probe(CDP_9333, ("instagram",))
    assert probe.attached is True and probe.logins == {"instagram": "logged_in"}


def test_browser_classifies_one_login_row_per_platform_from_one_attach():
    """Three platforms cost ONE CDP connection, not three."""
    calls: dict = {}
    probe = BrowserProbe(attached=True, logins={"instagram": "logged_in",
                                                "x": "logged_out"})
    results = preflight.check_browser(
        FakeCfg(state_dir=Path("."), capabilities=(IG, X, LI), cdp_url=CDP_9333),
        _probes(browser=probe, calls=calls), cdp_ok=True,
        platforms=("instagram", "linkedin", "x"))
    by_id = {r.id: r for r in results}
    assert calls["browser"] == [(CDP_9333, ("instagram", "linkedin", "x"))]
    assert by_id["cdp_attachable"].status == STATUS_PASS
    assert by_id["login.instagram"].status == STATUS_PASS
    assert by_id["login.instagram"].remedy is None
    assert by_id["login.x"].status == STATUS_FAIL
    assert by_id["login.linkedin"].status == STATUS_UNKNOWN   # absent from the probe
    for platform in ("instagram", "linkedin", "x"):
        row = by_id[f"login.{platform}"]
        # Risk 1: li_at/auth_token are UNVALIDATED cookie names. A false red must never
        # park a fleet at 4am with nobody present — the wizard blocks, the launch informs.
        assert row.severity == SEVERITY_WARN and row.blocking is False
    assert "Open login tab" in by_id["login.x"].remedy


# ---- run_preflight: composition, ordering, and the never-raises guarantee ----

def _cdp_cfg(tmp_path: Path, **over) -> FakeCfg:
    return FakeCfg(state_dir=tmp_path / "state", capabilities=(IG,), **over)


def test_run_preflight_runs_every_check_in_the_frozen_order(tmp_path: Path):
    report = preflight.run_preflight(
        _cdp_cfg(tmp_path), probes=_probes(env=_green_env(), cdp={CDP_9333: "ok"}))
    assert [c.id for c in report.checks] == [
        "state_dir_writable", "token_persistence", "dispatch_credential", "capabilities",
        "llm_backend", "playwright", "cdp_reachable", "cdp_port_drift",
        "chrome_profile", "cdp_attachable", "login.instagram"]
    assert report.ok is True and report.blocking is False
    assert report.ran_at > 0 and report.duration_ms >= 0


def test_run_preflight_probes_the_sibling_port_only_when_the_configured_one_is_dead(
        tmp_path: Path):
    """Two named candidates, never a scan — and never a second probe from inside a
    check."""
    calls: dict = {}
    preflight.run_preflight(_cdp_cfg(tmp_path), probes=_probes(
        env=_green_env(), cdp={CDP_9333: "ok"}, calls=calls))
    assert calls["cdp"] == [CDP_9333]

    calls = {}
    report = preflight.run_preflight(
        _cdp_cfg(tmp_path, cdp_url=CDP_9222),
        probes=_probes(env=_green_env(), cdp={CDP_9333: "ok"}, calls=calls))
    assert calls["cdp"] == [CDP_9222, CDP_9333]
    assert report.get("cdp_reachable").detail == (
        "Chrome is on 9333 but this worker is configured for 9222")
    assert report.get("cdp_port_drift").status == STATUS_FAIL


def test_an_api_only_box_touches_no_network_at_all(tmp_path: Path):
    """~1ms and zero I/O — which is what keeps the whole existing worker suite fast and
    offline once a youtube capability is in the shared fixture."""
    calls: dict = {}
    report = preflight.run_preflight(
        FakeCfg(state_dir=tmp_path / "s", capabilities=(YT,)),
        probes=_probes(env=_green_env(), calls=calls))
    assert calls["cdp"] == [] and calls["browser"] == []
    assert report.blocking is False
    assert {c.id for c in report.checks if c.status == STATUS_SKIP} == {
        "playwright", "cdp_reachable", "cdp_port_drift", "cdp_attachable",
        "chrome_profile"}
    assert not [c for c in report.checks if c.id.startswith("login.")]


def test_a_raising_check_is_demoted_to_a_warning(tmp_path: Path, monkeypatch):
    """THE property that makes this safe to ship: our own bug can only ever demote. A
    fatal check that raises must not park a healthy box."""
    def _boom(cfg, probes):
        raise ZeroDivisionError("/home/op/aizu-secrets.env line 3")
    monkeypatch.setattr(preflight, "check_capabilities", _boom)

    report = preflight.run_preflight(
        _cdp_cfg(tmp_path), probes=_probes(env=_green_env(), cdp={CDP_9333: "ok"}))
    result = report.get("capabilities")
    assert result.severity == SEVERITY_WARN and result.status == STATUS_FAIL
    assert result.detail == "the check itself raised ZeroDivisionError"
    assert "ZeroDivisionError" in result.remedy
    assert report.blocking is False               # <- the whole point
    # The exception's MESSAGE never rides anywhere: it can carry a path or a secret.
    assert "aizu-secrets.env" not in json.dumps(report.to_wire())


def test_a_raising_browser_check_still_yields_its_login_rows(tmp_path: Path, monkeypatch):
    """The UI's row count must be stable across a crashing probe, or a login badge
    silently vanishes rather than reading 'unknown'."""
    def _boom(cfg, probes, *, cdp_ok, platforms):
        raise RuntimeError("boom")
    monkeypatch.setattr(preflight, "check_browser", _boom)

    report = preflight.run_preflight(
        FakeCfg(state_dir=tmp_path / "s", capabilities=(IG, X)),
        probes=_probes(env=_green_env(), cdp={CDP_9333: "ok"}))
    assert report.get("cdp_attachable").severity == SEVERITY_WARN
    assert report.get("login.instagram").status == STATUS_UNKNOWN
    assert report.get("login.x").status == STATUS_UNKNOWN
    assert report.blocking is False


def test_run_preflight_never_raises_even_when_every_probe_explodes(tmp_path: Path):
    """An injected probe that raises must not sink the report — the operator still gets
    every other row."""
    def _explode(*a, **k):
        raise OSError("probe exploded")

    probes = Probes(cdp=_explode, browser=_explode, token_roundtrip=_explode,
                    env=_explode, run_active=_explode, playwright_available=_explode)
    report = preflight.run_preflight(_cdp_cfg(tmp_path), probes=probes)
    assert isinstance(report, PreflightReport)
    assert report.get("state_dir_writable").status == STATUS_PASS
    assert "probe exploded" not in json.dumps(report.to_wire())
    assert report.enforced is True     # a broken env probe must FAIL SAFE, not fail open


def test_run_preflight_never_raises_on_a_garbage_config():
    """Nothing about a malformed cfg may escape as an exception into Sidecar.run()."""
    report = preflight.run_preflight(object(), probes=_probes())
    assert isinstance(report, PreflightReport) and report.checks


@pytest.mark.parametrize("value,enforced", [
    ("0", False), ("false", False), ("NO", False), (" off ", False),
    ("", True), ("1", True), ("true", True), ("maybe", True), ("please-dont", True),
])
def test_enforcement_break_glass_only_disables_on_an_explicit_off(
        tmp_path: Path, value, enforced):
    """Unset or garbage must ENFORCE — fail safe, not fail open (rule 7)."""
    report = preflight.run_preflight(
        _cdp_cfg(tmp_path),
        probes=_probes(env={preflight.PREFLIGHT_ENFORCE_ENV: value}))
    assert report.enforced is enforced


def test_explicit_enforce_argument_wins_over_the_env(tmp_path: Path):
    probes = _probes(env={preflight.PREFLIGHT_ENFORCE_ENV: "0"})
    assert preflight.run_preflight(_cdp_cfg(tmp_path), probes=probes,
                                   enforce=True).enforced is True


# ---- the mechanically-enforced invariants (§2.2) ----

def _scenarios(tmp_path: Path) -> list:
    """Every shape a real box has been observed in, as (name, cfg, probes)."""
    green = {"OPENROUTER_API_KEY": "sk-x"}
    dead = BrowserProbe(attached=False, error="TargetClosedError")
    out = BrowserProbe(attached=True, logins={"instagram": "logged_out",
                                              "x": "unknown", "linkedin": "logged_in"})
    cdp = FakeCfg(state_dir=tmp_path / "a", capabilities=(IG, X, LI))
    return [
        ("api-only", FakeCfg(state_dir=tmp_path / "b", capabilities=(YT,)),
         _probes(env=green)),
        ("all-green", cdp, _probes(env=green, cdp={CDP_9333: "ok"})),
        ("chrome-down", cdp, _probes(env=green)),
        ("port-drift", dataclasses.replace(cdp, cdp_url=CDP_9222),
         _probes(env=green, cdp={CDP_9333: "ok"})),
        ("degraded-chrome", cdp,
         _probes(env=green, cdp={CDP_9333: "ok"}, browser=dead)),
        ("logged-out", cdp, _probes(env=green, cdp={CDP_9333: "ok"}, browser=out)),
        ("no-playwright", cdp,
         _probes(env=green, cdp={CDP_9333: "ok"}, playwright=False)),
        ("version-skew", cdp,
         _probes(env=green, cdp={CDP_9333: "ok"},
                 browser=BrowserProbe(attached=False,
                                      error=preflight._PROBE_UNAVAILABLE))),
        ("job-running", cdp,
         _probes(env=green, cdp={CDP_9333: "ok"}, run_active=True)),
        ("slow-chrome", cdp,
         _probes(env=green, cdp={CDP_9333: "ok"},
                 browser=BrowserProbe(attached=False, error="ReadinessTimeout"))),
        ("nothing-configured", FakeCfg(state_dir=tmp_path / "c", dispatch_base_url=""),
         _probes(token_error="SecretCipherError: AIZU_SECRET_KEY is not set",
                 token_present=None)),
        ("no-credential", dataclasses.replace(cdp, bootstrap_token=None),
         _probes(env=green, cdp={CDP_9333: "ok"}, token_present=False)),
        ("warming-only", cdp,
         _probes(env={preflight.WARMING_ONLY_ENV: "1"}, cdp={CDP_9333: "ok"})),
        ("warming-enabled-but-leases-harvest", cdp,
         _probes(env={"AIZU_WARMING_ENABLED": "1"}, cdp={CDP_9333: "ok"})),
        # ...and the box upgraded across the per-brand profile split, still holding the
        # profile it warmed before it. Warn-severity by design, so this entry is what
        # mechanically pins that it cannot park a box
        # (test_a_warn_severity_row_never_blocks_in_any_scenario) while still carrying a
        # remedy an SSH session can act on.
        ("legacy-chrome-profile", cdp,
         _probes(env=green, cdp={CDP_9333: "ok"},
                 chrome_profile=_base(tmp_path, legacy=True, name="pre-split"))),
    ]


def test_no_fatal_check_returns_unknown(tmp_path: Path):
    """Rule 3, stated precisely: ``unknown`` may never block.

    NOTE the check id allowlist. The spec's §2.2.1 headline ("no fatal check may return
    unknown") contradicts its own §2 row 9, which REQUIRES ``cdp_attachable`` — a fatal
    check — to report ``unknown`` on a missing Playwright or a readiness version skew.
    The invariant that actually holds, and the one worth enforcing, is that an unknown
    never blocks; the allowlist keeps a NEW fatal-unknown anywhere else from slipping in
    unnoticed."""
    allowed_fatal_unknown = {"cdp_attachable"}
    for name, cfg, probes in _scenarios(tmp_path):
        report = preflight.run_preflight(cfg, probes=probes)
        for check in report.checks:
            if check.status == STATUS_UNKNOWN:
                assert check.blocking is False, f"{name}: {check.id} blocks on unknown"
                if check.severity == SEVERITY_FATAL:
                    assert check.id in allowed_fatal_unknown, f"{name}: {check.id}"


def test_a_warn_severity_row_never_blocks_in_any_scenario(tmp_path: Path):
    """Including every ``login.*`` row — the governing split is that the wizard blocks
    and the launch preflight informs."""
    for name, cfg, probes in _scenarios(tmp_path):
        for check in preflight.run_preflight(cfg, probes=probes).checks:
            if check.severity == SEVERITY_WARN:
                assert check.blocking is False, f"{name}: {check.id}"


def test_every_failing_check_carries_an_actionable_remedy(tmp_path: Path):
    """F12: nobody can SSH into these PCs, so the remedy IS the diagnostic. A red row
    with no next step is a dead end."""
    for name, cfg, probes in _scenarios(tmp_path):
        for check in preflight.run_preflight(cfg, probes=probes).checks:
            if check.status != STATUS_FAIL:
                continue
            assert check.remedy, f"{name}: {check.id} failed with no remedy"
            assert check.detail, f"{name}: {check.id} failed with no detail"


def test_every_non_pass_check_at_least_says_why(tmp_path: Path):
    """``unknown`` rows are the one place a remedy may legitimately be absent: an
    unattachable-because-Playwright-is-missing box is told what to do by the
    ``playwright`` row instead, and the version-skew case has no operator action at
    all. They must still carry a detail — an amber row with no text is unreadable."""
    for name, cfg, probes in _scenarios(tmp_path):
        for check in preflight.run_preflight(cfg, probes=probes).checks:
            if check.status == STATUS_PASS:
                continue
            assert check.detail, f"{name}: {check.id} says nothing"


def test_every_check_id_and_severity_is_from_the_frozen_set(tmp_path: Path):
    """The check list is frozen (§2): 11 ids, exactly. A new id would silently bypass
    the server's ``_validate_preflight_summary`` whitelist and be dropped on the wire."""
    known = {preflight.CHECK_STATE_DIR, preflight.CHECK_TOKEN_PERSISTENCE,
             preflight.CHECK_DISPATCH_CREDENTIAL, preflight.CHECK_CAPABILITIES,
             preflight.CHECK_LLM_BACKEND, preflight.CHECK_PLAYWRIGHT,
             preflight.CHECK_CDP_REACHABLE, preflight.CHECK_CDP_PORT_DRIFT,
             preflight.CHECK_CDP_ATTACHABLE, preflight.CHECK_CHROME_PROFILE,
             preflight.CHECK_PREFLIGHT_ERROR}
    for name, cfg, probes in _scenarios(tmp_path):
        report = preflight.run_preflight(cfg, probes=probes)
        for check in report.checks:
            assert check.severity in (SEVERITY_FATAL, SEVERITY_WARN)
            assert check.status in (STATUS_PASS, STATUS_FAIL, STATUS_UNKNOWN, STATUS_SKIP)
            if check.id.startswith(preflight.CHECK_LOGIN_PREFIX):
                assert check.id[len("login."):] in ("instagram", "linkedin", "x")
            else:
                assert check.id in known, f"{name}: unknown check id {check.id}"
            assert check.title and check.title != check.id


def test_no_secret_value_in_any_wire_field(tmp_path: Path):
    """``detail`` is worker-authored text that rides upstream into the SUPERADMIN fleet
    console. Every field of both wire shapes AND every log line is swept for real-looking
    secret values planted in the environment."""
    secrets = {
        "OPENROUTER_API_KEY": "sk-or-v1-LEAKED-OPENROUTER-VALUE",
        "AIZU_SECRET_KEY": "LEAKED-FERNET-KEY-VALUE=",
        "AIZU_WORKER_CAPABILITIES": "[[1,'nope',null]]",
        "AIZU_TOKEN_BACKEND": "file",
    }
    cfg = FakeCfg(state_dir=tmp_path / "state", capabilities=(IG,),
                  bootstrap_token="LEAKED-BOOTSTRAP-TOKEN-VALUE")
    probes = _probes(env=secrets, cdp={CDP_9333: "ok"},
                     browser=BrowserProbe(attached=True,
                                          logins={"instagram": "logged_out"}))
    report = preflight.run_preflight(cfg, probes=probes)

    logger = RecordingLogger()
    report.log_to(logger)
    haystack = "\n".join([json.dumps(report.to_wire()),
                          json.dumps(report.to_upstream_wire()),
                          "\n".join(logger.lines)])
    for value in ("sk-or-v1-LEAKED-OPENROUTER-VALUE", "LEAKED-FERNET-KEY-VALUE=",
                  "LEAKED-BOOTSTRAP-TOKEN-VALUE"):
        assert value not in haystack, f"secret value {value!r} leaked"
    # ...while the NAMES, which are what an operator needs, are all still there.
    assert "OPENROUTER_API_KEY" in haystack


def test_every_scenario_produces_a_json_serialisable_report(tmp_path: Path):
    """Both wire shapes cross a JSON boundary (control surface, register body); a
    non-primitive sneaking into a detail would fail at the worst possible moment."""
    for name, cfg, probes in _scenarios(tmp_path):
        report = preflight.run_preflight(cfg, probes=probes)
        json.dumps(report.to_wire())
        upstream = report.to_upstream_wire()
        assert upstream is not None, f"{name}: upstream wire unexpectedly dropped"
        json.dumps(upstream)


# ---- error_report (check 11) ----

def test_error_report_is_a_non_blocking_warning_carrying_only_a_type_name():
    """Belt-and-braces over ``_guarded``: even a crash in the COMPOSITION leaves the box
    leasing at full capabilities, because this report has no fatal check in it."""
    report = preflight.error_report(ValueError("a message with /a/path and a secret"))
    assert report.blocking is False and report.ok is False
    check = report.get(preflight.CHECK_PREFLIGHT_ERROR)
    assert check.severity == SEVERITY_WARN and check.status == STATUS_FAIL
    assert check.detail == "the preflight raised ValueError"
    assert "a message with" not in json.dumps(report.to_wire())
    assert report.to_upstream_wire()["failed"][0]["id"] == "preflight_error"


def test_error_report_honours_the_enforcement_flag():
    assert preflight.error_report(ValueError("x"), enforced=False).enforced is False


# ---- cdp_platforms_advertised ----

@pytest.mark.parametrize("capabilities,expected", [
    ((), ()),
    ((YT,), ()),
    ((IG, IG), ("instagram",)),
    ((X, IG, LI, YT), ("instagram", "linkedin", "x")),
    (((1, "INSTAGRAM", None),), ("instagram",)),       # tolerant of case
    ((None, "junk", (), (1,), (1, None, None)), ()),   # tolerant of malformed entries
])
def test_cdp_platforms_advertised(capabilities, expected):
    assert preflight.cdp_platforms_advertised(
        FakeCfg(state_dir=Path("."), capabilities=capabilities)) == expected


def test_cdp_platforms_advertised_tolerates_a_config_without_capabilities():
    assert preflight.cdp_platforms_advertised(object()) == ()


# ---- resolve_cdp_url (F10 adoption) ----

def test_resolve_cdp_url_respects_explicit(tmp_path: Path):
    """An operator who explicitly pinned a port and got it wrong gets a NAMED FATAL,
    never a silent repoint — silently overriding an explicit setting is how you lose an
    afternoon."""
    calls: dict = {}
    cfg = FakeCfg(state_dir=tmp_path, cdp_url=CDP_9222, cdp_url_explicit=True)
    out = preflight.resolve_cdp_url(
        cfg, probes=_probes(cdp={CDP_9333: "ok"}, calls=calls))
    assert out is cfg and calls["cdp"] == []      # not even probed


def test_resolve_cdp_url_keeps_a_configured_port_that_answers(tmp_path: Path):
    calls: dict = {}
    cfg = FakeCfg(state_dir=tmp_path, cdp_url=CDP_9333)
    out = preflight.resolve_cdp_url(
        cfg, probes=_probes(cdp={CDP_9333: "ok", CDP_9222: "ok"}, calls=calls))
    assert out is cfg and calls["cdp"] == [CDP_9333]   # the sibling is never consulted


def test_resolve_cdp_url_adopts_sibling_when_unset(tmp_path: Path):
    """The reason unifying on 9333 is safe: an unpinned box whose Chrome is genuinely on
    the other port keeps working, unattended, WITH A RECEIPT."""
    cfg = FakeCfg(state_dir=tmp_path, cdp_url=CDP_9333)
    out = preflight.resolve_cdp_url(cfg, probes=_probes(cdp={CDP_9222: "ok"}))
    assert out.cdp_url == CDP_9222
    assert "9222" in out.cdp_url_drift_note and "pin cdp_port" in out.cdp_url_drift_note


def test_resolve_cdp_url_leaves_the_config_alone_when_no_sibling_answers(tmp_path: Path):
    cfg = FakeCfg(state_dir=tmp_path, cdp_url=CDP_9333)
    assert preflight.resolve_cdp_url(cfg, probes=_probes()) is cfg


@pytest.mark.parametrize("url", ["http://127.0.0.1:9999", "not a url", ""])
def test_resolve_cdp_url_leaves_an_unrelated_port_alone(tmp_path: Path, url):
    """Two named candidates, never a scan: a URL on neither well-known port has no
    sibling to adopt."""
    cfg = FakeCfg(state_dir=tmp_path, cdp_url=url)
    assert preflight.resolve_cdp_url(
        cfg, probes=_probes(cdp={CDP_9222: "ok", CDP_9333: "ok"})) is cfg


def test_resolve_cdp_url_adopts_on_an_older_config_without_the_note_field(tmp_path: Path):
    """Version skew: a partially-upgraded install still gets the working port, just
    without the adoption receipt (the drift check reports it from the live probe)."""
    cfg = OldCfg(state_dir=tmp_path, cdp_url=CDP_9333)
    out = preflight.resolve_cdp_url(cfg, probes=_probes(cdp={CDP_9222: "ok"}))
    assert out.cdp_url == CDP_9222 and not hasattr(out, "cdp_url_drift_note")


def test_resolve_cdp_url_never_raises(tmp_path: Path):
    """Port resolution is an optimisation, never a gate — it runs before the Sidecar
    even exists, so an exception here would be a launch crash."""
    def _explode(*a, **k):
        raise OSError("no network stack")
    cfg = FakeCfg(state_dir=tmp_path, cdp_url=CDP_9333)
    assert preflight.resolve_cdp_url(cfg, probes=Probes(cdp=_explode)) is cfg
    assert preflight.resolve_cdp_url(object(), probes=_probes()) is not None


def test_an_adopted_config_reports_a_green_reachable_and_a_red_drift(tmp_path: Path):
    """The end-to-end F10 story on an unpinned box: it works, and the operator is still
    told to pin the port before the next relaunch becomes a coin flip."""
    cfg = _cdp_cfg(tmp_path, cdp_url=CDP_9333)
    resolved = preflight.resolve_cdp_url(cfg, probes=_probes(cdp={CDP_9222: "ok"}))
    report = preflight.run_preflight(
        resolved, probes=_probes(env=_green_env(), cdp={CDP_9222: "ok"}))
    assert report.get("cdp_reachable").status == STATUS_PASS
    assert report.get("cdp_port_drift").status == STATUS_FAIL
    assert report.blocking is False


# ---- against the real WorkerConfig ----

def test_run_preflight_against_a_real_worker_config(tmp_path: Path):
    """preflight reads cfg through getattr, so it must work against the real frozen
    dataclass both before and after the F10 fields land in worker/config.py."""
    cfg = WorkerConfig(dispatch_base_url="http://dispatch.local",
                       cfg_dir=tmp_path / "config", db_path=":memory:",
                       state_dir=tmp_path / "state", capabilities=(YT,),
                       bootstrap_token="boot")
    report = preflight.run_preflight(cfg, probes=_probes(env=_green_env()))
    assert report.blocking is False and report.ok is True
    json.dumps(report.to_wire())

    blind = dataclasses.replace(cfg, capabilities=())
    blocked = preflight.run_preflight(blind, probes=_probes(env=_green_env()))
    assert blocked.blocking is True
    assert [c.id for c in blocked.blocking_checks()] == ["capabilities"]
