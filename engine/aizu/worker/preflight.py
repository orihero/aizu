"""Launch-time self-check for a worker box (ledger F9/F10/F12, B6).

Answers one question before the box takes work: CAN this machine actually run a job?
Every failure this catches was observed live producing a box that reads PERFECTLY
HEALTHY in the fleet console and cannot work. Nothing here exits the process, refuses
registration or stops the control surface — a fatal check withholds CAPABILITIES and
parks the lease loop, re-probing every 30s, so every fatal state self-heals unattended
(F8's lesson: a sidecar that exits rc=0 makes its supervisor give up).

Design rules this module enforces mechanically, not by convention:

  - **Fatal checks probe MECHANISMS, never env-var names.** Token persistence is a real
    save/load/clear through whichever backend ``AIZU_TOKEN_BACKEND`` selected; the LLM
    check uses the IDENTICAL predicate ``cli._build_run_io`` raises on. An env-name
    check gets both backwards on a keyring box and on a local-Ollama box.
  - **``unknown`` never blocks.** Only warn-severity checks may return it, so a probe
    that cannot tell can never dark a box (test: ``test_no_fatal_check_returns_unknown``).
  - **The preflight's own bug is a warning.** :func:`run_preflight` never raises: every
    check is individually guarded into a warn-level result, so a crash in a fatal check
    demotes to a warning instead of parking a healthy box.
  - **No secret VALUES anywhere** — in a ``detail``, a log line or a wire field. Variable
    NAMES only. ``_default_token_roundtrip`` never even reads a live credential when one
    is already stored: its existence is the proof the backend works.
  - **A probe never writes where the live credential lives.** The round-trip runs in a
    disjoint state dir + keychain account (:func:`_probe_token_backend`), because it
    executes on a detached thread while a ``_register()`` may land at any instant and
    losing that token costs an operator visit (B10).
  - **A timeout is not a refusal.** Only checks that got an ANSWER may be fatal; a probe
    that ran out of clock reports ``unknown`` (see :func:`_is_attach_timeout`), because
    every budget here is tighter than the real job's.

Every side effect is injected through :class:`Probes` with a real default (the idiom
``chrome_manager`` already uses), so the whole module is unit-testable with no real
browser, no network and no keychain.
"""
from __future__ import annotations

import dataclasses
import json
import os
import shutil
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Optional
from urllib.parse import urlparse, urlunparse

from ..core.config import CDP_PLATFORMS
from ..core.logsetup import get_logger
from ..secrets import SecretCipherError

log = get_logger("aizu.worker.preflight")

SEVERITY_FATAL = "fatal"
SEVERITY_WARN = "warn"
STATUS_PASS, STATUS_FAIL, STATUS_UNKNOWN, STATUS_SKIP = "pass", "fail", "unknown", "skip"

PREFLIGHT_ENFORCE_ENV = "AIZU_PREFLIGHT_ENFORCE"   # break-glass; default = enforce
# The ONE thing that establishes "this box will never run a live harvest job", declared
# per box. It is deliberately NOT `AIZU_WARMING_ENABLED`: that is the GLOBAL layer-1
# warming hard-stop (CLAUDE.md), i.e. "warming is permitted at all", which a box that
# also leases harvest jobs has every reason to set. Gating the LLM check on it demoted
# the F9.3 fatal to a warning on exactly those boxes — every live job then dead-letters
# at attempt 5 with the real cause never leaving the machine, behind an amber row nobody
# reads. Nothing else in WorkerConfig says anything about mode (engine_mode is a
# per-JOB field on JobSpec), so this has to be an explicit operator declaration.
WARMING_ONLY_ENV = "AIZU_WORKER_WARMING_ONLY"
# How long `_park_for_preflight` SLEEPS between probes — not the cycle length. The sleep
# runs BEFORE each probe, so a wedged Chrome makes the cycle 30s + the probe's own bound
# (3 + 1.5 + 30 = 34.5s worst case) ≈ 65s, and the previous probe's abandoned reader is
# hard-stopped by readiness at budget+5s = 35s. That ordering is what keeps two CDP
# attaches off the one warmed Chrome without a single-flight guard: the interval does not
# need to exceed the probe budget, it only needs to follow it.
RECHECK_INTERVAL_SEC = 30.0
_CDP_TIMEOUT_SEC = 3.0
_ALT_CDP_TIMEOUT_SEC = 1.5
# The WHOLE wall-clock budget for one browser probe: attach + contexts + pages + cookies.
# Sized from the job this check GATES, not invented here — `core/cdp.py` gives the real
# harvest attach nav_timeout+10s = 30s (with no timeout on connect_over_cdp at all) and
# calls those budgets "GENEROUS defaults tuned for a slow worker PC". A gate stricter
# than the work it admits can only manufacture false fatals, and a cold Playwright driver
# spawn behind Windows AV clears 5s routinely.
#
# Deliberately a LOCAL literal mirroring `readiness.ATTACH_FATAL_BUDGET_SEC` rather than
# an import of it, for the same reason `worker/config.py` duplicates `DEFAULT_CDP_URL`:
# importing readiness at module scope drags Playwright into every sidecar (F-9) and would
# undo every lazy import below. `test_the_fatal_budget_matches_readinesss_contract` is
# the drift guard.
#
# Splitting this budget between the attach and the reads that follow it is READINESS'
# job and is done inside `read_browser_state` (`_attach_timeout_sec`) — preflight passes
# ONE number and must not re-split it here. Two implementations of one split is how the
# attach silently ended up with 7.2s of a 20s bound.
_BROWSER_TIMEOUT_SEC = 30.0
MAX_UPSTREAM_FAILED = 16     # cap what rides the register/heartbeat body
MAX_UPSTREAM_DETAIL = 200    # chars
# The whole upstream object is dropped rather than sent oversized: a diagnostic hint must
# never grow the register body enough to be rejected (same spirit as the B9 spend hint —
# "an accounting hint must never make a runnable job unrunnable").
#
# Sized so a MAXIMALLY verbose report still fits: 16 rows x 200-char details, plus ids,
# severities and statuses, lands near 4.7 KB. The budget was 4096 before `status` joined
# each row, which put the worst case just OVER the line — and going over drops the report
# WHOLE, so the box with the most to say would have been the one that said nothing. The
# worker register/heartbeat body cap is 1 MiB (server.WORKER_MAX_BODY_BYTES), so this is
# still well under 1% of what the endpoint accepts.
MAX_UPSTREAM_BYTES = 8192

# The only two ports this system has ever used for the warmed Chrome. 9333 is canonical
# as of 2026-08 (F10); 9222 survives on boxes provisioned before that, which is exactly
# why detection is two named candidates and NEVER a port scan.
_ALTERNATE_CDP_PORTS = (9222, 9333)

# Written+read+deleted only when NOTHING is stored yet — mirrors token_store's own
# _keyring_health_ok probe value. Never a real credential.
_PROBE_TOKEN = "aizu-preflight-probe"
# ...and it is written into ITS OWN state dir, never the one holding the live credential.
# Both token backends key off exactly two things and the probe changes both (see
# _probe_token_backend): the file backend off `<state_dir>/worker-token.enc`, the keyring
# backend off `<state_dir>/machine-id` as its account name.
_PROBE_SUBDIR = ".preflight-token-probe"
_PROBE_MACHINE_ID = "preflight-token-probe"
# Sentinel used by _default_browser_probe when readiness has no probe_browser (a
# partially-upgraded install). Treated exactly like "Playwright is missing": UNKNOWN,
# never a fatal fail — a version skew must not dark a working box.
_PROBE_UNAVAILABLE = "ProbeUnavailable"

CHECK_STATE_DIR = "state_dir_writable"
CHECK_TOKEN_PERSISTENCE = "token_persistence"
CHECK_DISPATCH_CREDENTIAL = "dispatch_credential"
CHECK_CAPABILITIES = "capabilities"
CHECK_LLM_BACKEND = "llm_backend"
CHECK_PLAYWRIGHT = "playwright"
CHECK_CDP_REACHABLE = "cdp_reachable"
CHECK_CDP_PORT_DRIFT = "cdp_port_drift"
CHECK_CDP_ATTACHABLE = "cdp_attachable"
CHECK_LOGIN_PREFIX = "login."
CHECK_PREFLIGHT_ERROR = "preflight_error"

_TITLES = {
    CHECK_STATE_DIR: "Worker state directory is writable",
    CHECK_TOKEN_PERSISTENCE: "Worker identity can be stored",
    CHECK_DISPATCH_CREDENTIAL: "Enrolment credential present",
    CHECK_CAPABILITIES: "Platforms this box advertises",
    CHECK_LLM_BACKEND: "LLM backend for live runs",
    CHECK_PLAYWRIGHT: "Playwright is available",
    CHECK_CDP_REACHABLE: "Warmed Chrome is reachable",
    CHECK_CDP_PORT_DRIFT: "CDP port matches the running Chrome",
    CHECK_CDP_ATTACHABLE: "Chrome accepts a DevTools attach",
    CHECK_PREFLIGHT_ERROR: "Preflight could not complete",
}

# Remedies are OPERATOR COPY, frozen by the spec — one actionable sentence plus where in
# the desktop wizard to do it. They are the only text a human on a worker PC ever sees
# (F12: nobody can SSH into these boxes), so they are kept verbatim here rather than
# assembled at the call site.
_REMEDY_STATE_DIR = (
    "AIZU_WORKER_STATE points at {path}, which this process cannot write. Fix the path "
    "or its permissions and restart the worker (Setup → Advanced writes it for you).")
_REMEDY_TOKEN = (
    "This box cannot store its worker identity: {error}. Set AIZU_SECRET_KEY (generate "
    "one in Setup → Credentials, or python -c \"from aizu.secrets import SecretCipher; "
    "print(SecretCipher.generate_key())\") in worker-secrets.env, or set "
    "AIZU_TOKEN_BACKEND=keyring on a box whose keychain is pre-authorized.")
_REMEDY_DISPATCH_CREDENTIAL = (
    "No enrolment credential on this box — registration will be rejected. Paste a "
    "per-worker enrolment token from the panel (Fleet → Add worker) into Setup → Connect.")
_REMEDY_CAPABILITIES = (
    "This box advertises no platforms, so the fleet can never dispatch a job to it. Set "
    "AIZU_WORKER_PLATFORMS=all (or e.g. instagram,x,linkedin). If you set "
    "AIZU_WORKER_CAPABILITIES, it did not parse into any supported platform — it must be "
    "a JSON array of [orgId, platform, accountHandle].")
_REMEDY_LLM = (
    "No LLM backend on this box — every live run would fail at setup. Put "
    "OPENROUTER_API_KEY=... in worker-secrets.env (Setup → Credentials), or set "
    "AIZU_LLM_BASE_URL for a local Ollama/vLLM endpoint. If this box only ever runs "
    "warming jobs, set AIZU_WORKER_WARMING_ONLY=1 and this drops to a warning.")
_REMEDY_PLAYWRIGHT = (
    "Playwright is not importable in the sidecar, so Chrome cannot be checked (and CDP "
    "runs will fail). Reinstall the worker package with its browser extra, or reinstall "
    "the desktop app — this is broken packaging, not a config mistake.")
_REMEDY_CDP_REACHABLE = (
    "Nothing answers CDP at {cdp_url}. If the hint names another port, set "
    "AIZU_CDP_URL=http://127.0.0.1:<that port> (or cdp_port in config.toml) to match the "
    "Chrome you actually started. Otherwise start the warmed Chrome (Setup → Chrome, or "
    "engine/scripts/warm_chrome.sh). The worker re-checks every 30s and resumes on its own.")
_REMEDY_CDP_DRIFT = (
    "Pin the port so this cannot drift: set cdp_port in config.toml (Setup → Chrome → "
    "\"Use {port}\"), or AIZU_CDP_URL for a hand-run sidecar.")
_REMEDY_CDP_ATTACHABLE = (
    "Chrome answers on {cdp_url} but refuses a DevTools attach — this is the \"degraded "
    "Chrome\" case. Quit that Chrome completely and start a fresh warmed one (Setup → "
    "Chrome). Do not just reload a tab.")
_REMEDY_CDP_ATTACH_SLOW = (
    "Chrome answers on {cdp_url} but did not finish a DevTools attach within {seconds}s. "
    "The worker is NOT parked for this and re-checks every 30s — a cold browser or a "
    "virus scanner is usually enough. If it stays amber, quit that Chrome completely and "
    "start a fresh warmed one (Setup → Chrome).")
_REMEDY_LOGIN = (
    "Chrome is not signed in to {platform} (or the session expired). Open Setup → Sign "
    "in, click \"Open login tab\" for {platform}, finish the login and any 2FA in the "
    "real Chrome window — the badge turns green by itself.")
_REMEDY_CHECK_RAISED = (
    "The preflight could not complete ({error}) — the worker is running normally and "
    "unblocked. Report this with the sidecar log; it is a bug in the check, not in your box.")


# ---- results ----

@dataclass(frozen=True)
class CheckResult:
    """One check's verdict. ``detail`` is a short machine-flavoured fact for the operator
    (a path, a port, an exception TYPE) and NEVER a secret value; ``remedy`` is the
    human sentence and is populated only when there is something to do about it."""

    id: str
    title: str
    severity: str
    status: str
    detail: Optional[str] = None      # NEVER a secret VALUE — names only
    remedy: Optional[str] = None

    @property
    def blocking(self) -> bool:
        """Only a fatal check that actually FAILED blocks. 'unknown' deliberately does
        not: a probe that could not tell must never dark a box (rule 5)."""
        return self.severity == SEVERITY_FATAL and self.status == STATUS_FAIL

    def to_wire(self) -> dict:
        return {"id": self.id, "title": self.title, "severity": self.severity,
                "status": self.status, "detail": self.detail, "remedy": self.remedy}


@dataclass(frozen=True)
class BrowserProbe:
    """One bounded connect_over_cdp: did we attach, and what is each platform's login
    state. ``error`` is an exception TYPE NAME only, never a message."""

    attached: bool
    error: Optional[str] = None
    logins: dict = field(default_factory=dict)   # platform -> logged_in|logged_out|unknown


# ---- injected side effects (every one has a real default) ----

def _default_env(name: str) -> str:
    """Read one env var as a string ('' when unset) so callers can .strip() freely. The
    ONLY env reader in this module — tests substitute a dict and never touch os.environ."""
    return os.environ.get(name, "") or ""


def _never_active() -> bool:
    """Default run_active: a bare sidecar with no job supervisor wired in."""
    return False


def _default_cdp_probe(cdp_url: str, timeout: float = _CDP_TIMEOUT_SEC) -> str:
    """'ok' | 'unreachable' from readiness.probe_cdp (a cheap HTTP /json/version check,
    never Playwright). Imported lazily: readiness pulls Playwright in at import time and
    preflight must stay cheap to import on an API-only box."""
    from .. import readiness
    return readiness.probe_cdp(cdp_url, timeout)


def _default_browser_probe(cdp_url: str, platforms: tuple,
                           timeout: float = _BROWSER_TIMEOUT_SEC) -> BrowserProbe:
    """One bounded Playwright attach + login classification, delegated to
    ``readiness.probe_browser`` (readiness owns everything that touches a browser).

    Adapts whatever shape readiness returns rather than assuming its class identity, and
    degrades to ``_PROBE_UNAVAILABLE`` when the symbol is absent — a sidecar whose
    readiness module predates this work must read UNKNOWN, never a fatal fail.

    ``timeout`` is the WHOLE budget and is passed through as one number. Readiness splits
    it between the attach and the reads that follow (``_attach_timeout_sec``); preflight
    deliberately does NOT pre-split it or inject ``read_state``, because a second split
    layered on the first compounds — the attach ends up with a fraction of a fraction,
    which is the starvation F-1 was filed about, arrived at from the other direction."""
    from .. import readiness
    probe = getattr(readiness, "probe_browser", None)
    if probe is None:
        return BrowserProbe(attached=False, error=_PROBE_UNAVAILABLE, logins={})
    result = probe(cdp_url, tuple(platforms), timeout)
    if isinstance(result, BrowserProbe):
        return result
    return BrowserProbe(attached=bool(getattr(result, "attached", False)),
                        error=getattr(result, "error", None),
                        logins=dict(getattr(result, "logins", {}) or {}))


def _default_playwright_available() -> bool:
    from .. import readiness
    return bool(getattr(readiness, "PLAYWRIGHT_AVAILABLE", False))


def _default_token_roundtrip(cfg) -> Optional[str]:
    """Prove this box can persist AND read back its bearer token (F9.1). Returns None on
    success, else a SHORT backend error string (a type name and the backend's own message
    — never a token value).

    Probes the MECHANISM, not ``AIZU_SECRET_KEY``'s presence, because the box may be on
    the keyring backend where that var is irrelevant. When a token is ALREADY stored,
    loading it is itself the proof and we stop there. Only an empty (or corrupt, which
    ``_load_token_safely`` already recovers by clearing) store gets the round-trip, and
    that round-trip runs in its own disjoint storage location — see
    :func:`_probe_token_backend`. This function NEVER writes to, and never clears, the
    location holding the live credential."""
    from .token_store import TokenStore
    try:
        store = TokenStore(cfg.state_dir)
    except Exception as e:  # noqa: BLE001 — a bad AIZU_TOKEN_BACKEND lands here too
        return f"{type(e).__name__}: {e}"
    try:
        if store.load():
            return None
    except SecretCipherError:
        # A corrupt/undecryptable blob is NOT a broken backend: the sidecar clears and
        # re-registers on this today. Fall through to the round-trip, which answers the
        # real question (can we write a fresh one?).
        pass
    except Exception as e:  # noqa: BLE001
        return f"{type(e).__name__}: {e}"
    return _probe_token_backend(cfg)


def _probe_token_backend(cfg) -> Optional[str]:
    """Save/load/clear a throwaway token through the SAME backend ``AIZU_TOKEN_BACKEND``
    selected, in a storage location DISJOINT from the live credential's. Returns None on
    success, else a short backend error string.

    Disjointness is the whole design, and it is what makes this safe rather than merely
    unlikely to bite. ``request_preflight`` runs this on a detached thread while the
    wizard polls and a ``_register()`` may persist the real token at ANY instant, so
    every scheme that reads the shared store, decides, and then writes or clears it —
    including compare-before-clear — leaves a window in which it deletes a credential
    minted in between. The cost of losing that token is not a retry: it is a hand-minted
    enrolment token plus a visit to a PC nobody can SSH into (B10). So the probe simply
    never addresses the same storage key. Both backends key off exactly two things, and
    the probe changes both:

      - ``FernetFileBackend`` → ``<state_dir>/worker-token.enc``. The probe's state dir
        is a subdirectory, so it is a different file.
      - ``KeyringBackend`` → (service, ``<state_dir>/machine-id``) as the account name,
        with a SHARED ``"unregistered"`` literal when that file is missing. The probe
        writes its own machine-id first, so it is a different keychain account even on a
        box that has not registered yet — which is precisely the box that runs this path.

    This also subsumes the older B10 hazard (a missing/rotated AIZU_SECRET_KEY makes
    ``save()`` raise at the cipher with the real blob still intact on disk, and a
    cleanup in a ``finally`` would then unlink it): the cleanup here can only ever reach
    the probe's own directory and keychain entry, so it is unconditional and still safe.
    """
    from .token_store import TokenStore
    store = None
    probe_dir = None
    try:
        probe_dir = cfg.state_dir / _PROBE_SUBDIR
        probe_dir.mkdir(parents=True, exist_ok=True)
        # Load-bearing, not an artefact: this file IS the keyring backend's account name.
        (probe_dir / "machine-id").write_text(_PROBE_MACHINE_ID, encoding="utf-8")
        store = TokenStore(probe_dir)
        store.save(_PROBE_TOKEN)
        if store.load() != _PROBE_TOKEN:
            return "the token did not read back (the backend accepted a write and lost it)"
        return None
    except Exception as e:  # noqa: BLE001 — the point is to REPORT a broken backend
        return f"{type(e).__name__}: {e}"
    finally:
        if store is not None:
            try:
                store.clear()   # a keychain entry does not live under probe_dir
            except Exception:  # noqa: BLE001 — best effort; a stuck probe token is harmless
                pass
        if probe_dir is not None:
            shutil.rmtree(probe_dir, ignore_errors=True)


def _default_token_present(cfg) -> Optional[bool]:
    """Is a worker bearer token ACTUALLY stored on this box? True/False, or None when we
    could not tell (an unreadable/undecryptable store — ``token_persistence`` reports
    that far better than this check can).

    Separate from ``token_roundtrip`` on purpose: that one answers "does the backend
    work", which is what F9.1 needs and is TRUE on a brand-new box with nothing stored.
    Only presence answers "will register be rejected" (F-11). Strictly read-only — it
    never writes, never clears, and the value never leaves this function (rule 5)."""
    from .token_store import TokenStore
    try:
        return bool(TokenStore(cfg.state_dir).load())
    except Exception:  # noqa: BLE001 — a broken/corrupt store tells us nothing either way
        return None


@dataclass(frozen=True)
class Probes:
    """Every side effect injected with a real default (chrome_manager.py's idiom)."""

    cdp: Callable[[str, float], str] = _default_cdp_probe
    browser: Callable[[str, tuple, float], BrowserProbe] = _default_browser_probe
    token_roundtrip: Callable[[Any], Optional[str]] = _default_token_roundtrip
    token_present: Callable[[Any], Optional[bool]] = _default_token_present
    env: Callable[[str], str] = _default_env
    run_active: Callable[[], bool] = _never_active
    playwright_available: Callable[[], bool] = _default_playwright_available


# ---- the report ----

@dataclass(frozen=True)
class PreflightReport:
    checks: tuple
    ran_at: float
    duration_ms: int
    enforced: bool = True

    @property
    def blocking(self) -> bool:
        """True iff enforcement is on AND some fatal check failed. The break-glass
        (AIZU_PREFLIGHT_ENFORCE=0) demotes here, at the REPORT level, rather than by
        rewriting each check's severity — so the operator still sees which checks are
        fatal alongside the loud enforced:false banner (rule 7)."""
        return self.enforced and any(c.blocking for c in self.checks)

    @property
    def ok(self) -> bool:
        return not any(c.status == STATUS_FAIL for c in self.checks)

    def blocking_checks(self) -> tuple:
        return tuple(c for c in self.checks if c.blocking)

    def get(self, check_id: str) -> Optional[CheckResult]:
        for c in self.checks:
            if c.id == check_id:
                return c
        return None

    def to_wire(self) -> dict:
        """The control-surface /status shape (§4.6) — the ONLY channel the desktop shell
        has. Full detail + remedy: this payload never leaves the box."""
        return {"ok": self.ok, "blocking": self.blocking, "enforced": self.enforced,
                "ranAt": self.ran_at, "durationMs": self.duration_ms,
                "checks": [c.to_wire() for c in self.checks]}

    def to_upstream_wire(self) -> Optional[dict]:
        """The compact shape that rides the register/heartbeat body (§5.1) so an admin
        can read the real cause in the fleet console instead of visiting the PC (F12).

        Carries ids + severities + statuses + a truncated detail only: ``remedy``/``title``
        are UI text the console renders client-side from the id, and shipping them would
        double the body for nothing. Returns None when the result would exceed
        MAX_UPSTREAM_BYTES — a diagnostic hint must never be the reason a register is
        rejected.

        ``status`` rides along because ``failed`` holds every non-pass/non-skip row, which
        means UNKNOWN ("we could not check") sits next to FAIL ("we checked, it is broken")
        and the two need very different operator copy. Without it the commonest red state
        of all — Chrome down, so all three ``login.*`` rows go `unknown` — renders in the
        fleet console as "not signed in", sending an admin to fix a login that was never
        the problem. Remedy-by-id alone cannot recover that distinction."""
        failed = [c for c in self.checks if c.status not in (STATUS_PASS, STATUS_SKIP)]
        body = {
            "ok": self.ok, "blocking": self.blocking, "enforced": self.enforced,
            "ranAt": self.ran_at,
            "failed": [{"id": c.id, "severity": c.severity, "status": c.status,
                        "detail": (c.detail or "")[:MAX_UPSTREAM_DETAIL] or None}
                       for c in failed[:MAX_UPSTREAM_FAILED]],
        }
        try:
            size = len(json.dumps(body, ensure_ascii=False).encode("utf-8"))
        except (TypeError, ValueError):  # pragma: no cover - fields are all primitives
            return None
        return body if size <= MAX_UPSTREAM_BYTES else None

    def log_to(self, logger) -> None:
        """One line per non-pass check; ERROR for anything blocking, WARNING otherwise.
        Flat by design (risk 3): a genuinely misconfigured box repeats this every 30s,
        which is the point — the log IS the operator's diagnostic on a headless PC."""
        if not self.enforced:
            logger.warning(
                "PREFLIGHT ENFORCEMENT IS OFF (%s=0) — fatal checks will not park this "
                "box. Unset it once the box is fixed.", PREFLIGHT_ENFORCE_ENV)
        for c in self.checks:
            if c.status == STATUS_PASS or c.status == STATUS_SKIP:
                continue
            line = "preflight %s [%s] %s: %s"
            args = (c.status, c.severity, c.id, c.detail or c.title)
            if c.blocking and self.enforced:
                logger.error(line + " — %s", *args, c.remedy or "")
            else:
                logger.warning(line, *args)


# ---- helpers ----

def _truthy(value: Optional[str]) -> bool:
    """A truthy env value (1/true/yes/on), matching config._env_flag. Absent/'' → False."""
    return (value or "").strip().lower() in ("1", "true", "yes", "on")


def _redact_userinfo(url: str) -> str:
    """Strip any ``user:pass@`` from a URL before it reaches a detail, a log or the wire.

    Preflight details are published THREE ways — the local control surface, the sidecar
    log, and ``to_upstream_wire()`` into the cloud fleet console — so a URL interpolated
    into one is a URL published to all three. Falls back to the host-only form if the URL
    is malformed enough that urlparse/urlunparse disagree; never raises, because a
    redaction helper that throws would take the whole check down with it."""
    try:
        parts = urlparse(url)
        if not parts.username and not parts.password:
            return url
        host = parts.hostname or ""
        if parts.port:
            host = f"{host}:{parts.port}"
        return urlunparse(parts._replace(netloc=f"***@{host}"))
    except Exception:  # noqa: BLE001 — a detail string is never worth an exception
        return "<redacted>"


def _falsey(value: Optional[str]) -> bool:
    """An explicitly-off env value. Only these disable enforcement — an unset or garbage
    AIZU_PREFLIGHT_ENFORCE must ENFORCE (fail safe, not fail open)."""
    return (value or "").strip().lower() in ("0", "false", "no", "off")


def _is_attach_timeout(error: Optional[str]) -> bool:
    """True when the browser probe gave up on the CLOCK rather than on an answer.

    ``BrowserProbe.error`` is an exception TYPE NAME (readiness raises ``ReadinessTimeout``
    from its own bound; a Playwright-side deadline surfaces as ``TimeoutError``), so this
    matches by suffix. Deliberately conservative: an unrecognised name falls through to
    the FATAL branch, i.e. a rename upstream loses the demotion rather than silently
    demoting something that was never a timeout."""
    name = (error or "").strip().lower()
    return name.endswith("timeout") or name.endswith("timeouterror")


def _port_of(url: str) -> Optional[int]:
    try:
        return urlparse(url).port
    except ValueError:
        return None


def _alternate_cdp_url(cdp_url: str) -> Optional[str]:
    """The sibling of {9222, 9333} on the same host, else None (F10). Pure.

    ``readiness.alternate_cdp_url`` is the canonical implementation (it is what the
    CLI/panel half of the F10 decision calls); this delegates to it when present and
    otherwise computes the same thing, so preflight works on a partially-upgraded
    install without importing a symbol that may not exist yet."""
    try:
        from .. import readiness
        fn = getattr(readiness, "alternate_cdp_url", None)
        if fn is not None:
            return fn(cdp_url)
        parsed = urlparse(cdp_url)
        port = parsed.port
        if port not in _ALTERNATE_CDP_PORTS or parsed.hostname is None:
            return None
        other = _ALTERNATE_CDP_PORTS[0] if port == _ALTERNATE_CDP_PORTS[1] else _ALTERNATE_CDP_PORTS[1]
        return urlunparse(parsed._replace(netloc=f"{parsed.hostname}:{other}"))
    except Exception:  # noqa: BLE001 — a malformed URL just has no sibling
        return None


def cdp_platforms_advertised(cfg) -> tuple:
    """capabilities ∩ core.config.CDP_PLATFORMS, sorted, deduped. Empty means this box is
    API-only (youtube/telegram/reddit) and needs no browser at all — every Chrome check
    then SKIPS rather than failing, which is also what keeps the existing worker test
    suite network-free and sub-millisecond."""
    found = set()
    for entry in getattr(cfg, "capabilities", ()) or ():
        try:
            platform = str(entry[1] or "").strip().lower()
        except (IndexError, TypeError):
            continue
        if platform in CDP_PLATFORMS:
            found.add(platform)
    return tuple(sorted(found))


def _capability_platforms(cfg) -> tuple:
    """Every supported platform this box advertises, sorted + deduped (for the
    `capabilities` check's detail line)."""
    found = set()
    for entry in getattr(cfg, "capabilities", ()) or ():
        try:
            platform = str(entry[1] or "").strip().lower()
        except (IndexError, TypeError):
            continue
        if platform:
            found.add(platform)
    return tuple(sorted(found))


# ---- the checks (each returns CheckResult(s); each is individually unit-tested) ----

def check_state_dir(cfg) -> CheckResult:
    """Everything downstream writes here — machine-id, single-flight locks, spec/result
    files, the token blob. Today a bad path first surfaces as a crash inside
    ``cfg.machine_id`` during ``_register``, i.e. AFTER the server minted an identity.
    Runs FIRST because the token check needs the directory to exist."""
    path = getattr(cfg, "state_dir", None)
    probe = None
    try:
        path.mkdir(parents=True, exist_ok=True)
        probe = path / ".preflight-probe"
        fd = os.open(probe, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        try:
            os.write(fd, b"1")
        finally:
            os.close(fd)
    except Exception as e:  # noqa: BLE001 — permissions, a file where a dir belongs, a full disk
        return CheckResult(CHECK_STATE_DIR, _TITLES[CHECK_STATE_DIR], SEVERITY_FATAL,
                           STATUS_FAIL, f"{path}: {type(e).__name__}: {e}",
                           _REMEDY_STATE_DIR.format(path=path))
    finally:
        if probe is not None:
            try:
                probe.unlink(missing_ok=True)
            except Exception:  # noqa: BLE001
                pass
    return CheckResult(CHECK_STATE_DIR, _TITLES[CHECK_STATE_DIR], SEVERITY_FATAL,
                       STATUS_PASS, str(path))


def check_token_persistence(cfg, probes: Probes) -> CheckResult:
    """F9.1 — this box can persist AND read back its bearer token. Without it
    ``FernetFileBackend._get_cipher`` raises from inside ``_register`` after the server
    already minted the identity: the row reads 'online' while the process is dead, and a
    supervisor crash-loop rotates worker_token_hash every iteration."""
    error = probes.token_roundtrip(cfg)
    backend = "keyring backend" if (probes.env("AIZU_TOKEN_BACKEND").strip().lower()
                                    == "keyring") else "encrypted-file backend"
    if error:
        return CheckResult(CHECK_TOKEN_PERSISTENCE, _TITLES[CHECK_TOKEN_PERSISTENCE],
                           SEVERITY_FATAL, STATUS_FAIL, f"{backend}: {error}",
                           _REMEDY_TOKEN.format(error=error))
    return CheckResult(CHECK_TOKEN_PERSISTENCE, _TITLES[CHECK_TOKEN_PERSISTENCE],
                       SEVERITY_FATAL, STATUS_PASS, backend)


def check_dispatch_credential(cfg, probes: Probes, *, token_ok: bool) -> CheckResult:
    """The dispatch URL parses as http(s) AND this box holds some credential. With none,
    register is guaranteed to 401 forever and B10's 5–8 minute confirmation window is
    spent proving it. No network of its own; ``_register`` runs microseconds later and
    reports reachability far better than a duplicate probe could.

    The credential question is answered by ``probes.token_present`` — an actual read of
    the store — NOT by ``token_ok`` (F-11). ``token_ok`` only says the token BACKEND
    works, and a brand-new box with an empty store round-trips a probe token perfectly,
    so gating on it made the "no credential" branch unreachable on the one box shape it
    exists for: fresh, no bootstrap token, register about to 401 forever. ``token_ok``
    still matters, as the demoter: when the store cannot be read at all, presence is
    unknowable and this reports UNKNOWN rather than inventing a second red row for the
    fatal ``token_persistence`` already carries.

    Warn-only throughout: a box mid-re-enrolment (B10 parks it with the token cleared,
    waiting for an operator) legitimately holds neither credential, and parking it a
    second time over that would be a fatal for a state the sidecar is already handling
    loudly."""
    raw = getattr(cfg, "dispatch_base_url", "") or ""
    try:
        scheme = urlparse(raw).scheme
    except ValueError:
        scheme = ""
    # This module's docstring promises no secret VALUES in a detail, a log line or a
    # wire field — and every detail below rides `to_upstream_wire()` to the cloud and
    # into the sidecar log. A dispatch URL may legitimately carry userinfo
    # (https://user:pass@host), so redact before it is ever interpolated. readiness.py
    # carries the same warning about messages smuggling credential-bearing URLs.
    url = _redact_userinfo(raw)
    if scheme not in ("http", "https"):
        return CheckResult(CHECK_DISPATCH_CREDENTIAL, _TITLES[CHECK_DISPATCH_CREDENTIAL],
                           SEVERITY_WARN, STATUS_FAIL,
                           f"AIZU_DISPATCH_URL is not an http(s) URL: {url!r}",
                           _REMEDY_DISPATCH_CREDENTIAL)
    if getattr(cfg, "bootstrap_token", None):
        # Names the SOURCE, never the value — a first register is possible either way.
        return CheckResult(CHECK_DISPATCH_CREDENTIAL, _TITLES[CHECK_DISPATCH_CREDENTIAL],
                           SEVERITY_WARN, STATUS_PASS,
                           f"{url} — AIZU_WORKER_BOOTSTRAP_TOKEN is set")
    present = probes.token_present(cfg) if token_ok else None
    if present is None:
        return CheckResult(
            CHECK_DISPATCH_CREDENTIAL, _TITLES[CHECK_DISPATCH_CREDENTIAL], SEVERITY_WARN,
            STATUS_UNKNOWN,
            f"{url} — cannot tell whether a worker token is stored (see the "
            f"{CHECK_TOKEN_PERSISTENCE} check)")
    if not present:
        return CheckResult(CHECK_DISPATCH_CREDENTIAL, _TITLES[CHECK_DISPATCH_CREDENTIAL],
                           SEVERITY_WARN, STATUS_FAIL,
                           f"{url} — no stored worker token and no bootstrap token",
                           _REMEDY_DISPATCH_CREDENTIAL)
    return CheckResult(CHECK_DISPATCH_CREDENTIAL, _TITLES[CHECK_DISPATCH_CREDENTIAL],
                       SEVERITY_WARN, STATUS_PASS, f"{url} — a worker token is stored")


def check_capabilities(cfg, probes: Probes) -> CheckResult:
    """F9.2 — ``_parse_capabilities_env`` returns () when neither AIZU_WORKER_PLATFORMS
    nor AIZU_WORKER_CAPABILITIES is set, so the box registers with capabilities: [], can
    NEVER be leased to, and still flipped the tenant's readiness banner to a false
    ready:true. Fatal: a box that advertises nothing is not a worker."""
    platforms = _capability_platforms(cfg)
    if platforms:
        return CheckResult(CHECK_CAPABILITIES, _TITLES[CHECK_CAPABILITIES],
                           SEVERITY_FATAL, STATUS_PASS, ", ".join(platforms))
    # Two causes are indistinguishable from cfg alone — "nothing set" and "set, but the
    # tolerant parser silently dropped every entry" (a JSON typo, an unsupported platform
    # name). Read the raw env FOR DIAGNOSIS ONLY so the operator is told which one it is.
    raw_caps = probes.env("AIZU_WORKER_CAPABILITIES").strip()
    raw_plats = probes.env("AIZU_WORKER_PLATFORMS").strip()
    if raw_caps:
        detail = ("AIZU_WORKER_CAPABILITIES is set but did not parse into any supported "
                  "platform (it must be a JSON array of [orgId, platform, accountHandle])")
    elif raw_plats:
        detail = ("AIZU_WORKER_PLATFORMS is set but named no supported platform "
                  "(expected 'all' or a comma-separated list)")
    else:
        detail = "neither AIZU_WORKER_PLATFORMS nor AIZU_WORKER_CAPABILITIES is set"
    return CheckResult(CHECK_CAPABILITIES, _TITLES[CHECK_CAPABILITIES], SEVERITY_FATAL,
                       STATUS_FAIL, detail, _REMEDY_CAPABILITIES)


def check_llm_backend(cfg, probes: Probes) -> CheckResult:
    """F9.3 — ``cfg.run_args`` runs the engine in a child ON THIS BOX, so the box needs
    its own LLM backend; without one every live harvest job raises at ``_build_run_io``
    and dead-letters at attempt 5 with the real cause never leaving the machine (F12).

    The predicate is copied EXACTLY from ``cli._build_run_io`` so preflight can never
    disagree with the runtime. Demoted to warn ONLY on a box the operator has declared
    warming-only (``AIZU_WORKER_WARMING_ONLY``): ``_build_warming_io`` needs no LLM, so
    parking such a box would be a fabricated failure — but the declaration has to say
    "this box declines harvest work", which the global ``AIZU_WARMING_ENABLED``
    hard-stop never did (see WARMING_ONLY_ENV). Never echoes a value."""
    local = probes.env("AIZU_LLM_BASE_URL").strip()
    cloud = probes.env("OPENROUTER_API_KEY")
    warming_only = _truthy(probes.env(WARMING_ONLY_ENV))
    severity = SEVERITY_WARN if warming_only else SEVERITY_FATAL
    if local:
        return CheckResult(CHECK_LLM_BACKEND, _TITLES[CHECK_LLM_BACKEND], severity,
                           STATUS_PASS, "AIZU_LLM_BASE_URL is set")
    if cloud:
        return CheckResult(CHECK_LLM_BACKEND, _TITLES[CHECK_LLM_BACKEND], severity,
                           STATUS_PASS, "OPENROUTER_API_KEY is set")
    detail = "neither AIZU_LLM_BASE_URL nor OPENROUTER_API_KEY is set"
    if warming_only:
        detail += (f" (warning only — {WARMING_ONLY_ENV} is on and warming needs no LLM; "
                   "any harvest job leased here would still fail)")
    elif _truthy(probes.env("AIZU_WARMING_ENABLED")):
        # Named explicitly because this is the MIGRATION case: a box already in the
        # field with the global hard-stop on and no LLM key was amber before this
        # changeset and parks now. It is a true fatal (harvest work leased here fails
        # at attempt 5, which is F9.3), not a false one — but the operator needs to be
        # told which of the two similarly-named flags they actually want.
        detail += (f" — AIZU_WARMING_ENABLED is the GLOBAL warming hard-stop and does "
                   f"not declare this box warming-only; set {WARMING_ONLY_ENV}=1 if it "
                   "should decline harvest work")
    return CheckResult(CHECK_LLM_BACKEND, _TITLES[CHECK_LLM_BACKEND], severity,
                       STATUS_FAIL, detail, _REMEDY_LLM)


def check_playwright(cfg, probes: Probes) -> CheckResult:
    """Without Playwright ``readiness`` returns 'unknown' unconditionally and every CDP
    job fails. On a frozen PyInstaller sidecar this is a real, previously-silent
    packaging failure. Warn-only on purpose: a mis-detected import must never dark a box,
    and ``cdp_attachable`` fails loudly anyway if it is genuinely absent."""
    if not cdp_platforms_advertised(cfg):
        return CheckResult(CHECK_PLAYWRIGHT, _TITLES[CHECK_PLAYWRIGHT], SEVERITY_WARN,
                           STATUS_SKIP, "skipped — this box advertises no CDP platform")
    if probes.playwright_available():
        return CheckResult(CHECK_PLAYWRIGHT, _TITLES[CHECK_PLAYWRIGHT], SEVERITY_WARN,
                           STATUS_PASS)
    return CheckResult(CHECK_PLAYWRIGHT, _TITLES[CHECK_PLAYWRIGHT], SEVERITY_WARN,
                       STATUS_FAIL, "playwright is not importable in this interpreter",
                       _REMEDY_PLAYWRIGHT)


def check_cdp_reachable(cfg, *, cdp_state: str,
                        alternate_alive: Optional[str]) -> CheckResult:
    """F10 — something answers /json/version at cfg.cdp_url. PURE: run_preflight probes
    the two candidate ports once and hands both results in, so this (and the drift check
    below) can be reasoned about and tested with no I/O at all.

    Skipped entirely on an API-only box. On failure the detail names the SIBLING port
    when it is the one that answered — that single sentence is the whole F10 fix for an
    operator who provisioned Chrome per the warming runbook (9333) while this sidecar was
    configured for 9222."""
    platforms = cdp_platforms_advertised(cfg)
    if not platforms:
        return CheckResult(CHECK_CDP_REACHABLE, _TITLES[CHECK_CDP_REACHABLE],
                           SEVERITY_FATAL, STATUS_SKIP,
                           "skipped — this box advertises no CDP platform")
    cdp_url = getattr(cfg, "cdp_url", "")
    if cdp_state == "ok":
        note = getattr(cfg, "cdp_url_drift_note", None)
        return CheckResult(CHECK_CDP_REACHABLE, _TITLES[CHECK_CDP_REACHABLE],
                           SEVERITY_FATAL, STATUS_PASS, note or cdp_url)
    if alternate_alive:
        detail = (f"Chrome is on {_port_of(alternate_alive)} but this worker is "
                  f"configured for {_port_of(cdp_url)}")
    else:
        detail = f"nothing answers CDP at {cdp_url}"
    return CheckResult(CHECK_CDP_REACHABLE, _TITLES[CHECK_CDP_REACHABLE], SEVERITY_FATAL,
                       STATUS_FAIL, detail, _REMEDY_CDP_REACHABLE.format(cdp_url=cdp_url))


def check_cdp_port_drift(cfg, *, cdp_state: str,
                         alternate_alive: Optional[str]) -> CheckResult:
    """The named-error half of the F10 decision, and the visible receipt when the worker
    auto-adopted the sibling port. Warn-only — it never blocks; ``cdp_reachable`` already
    does that when the configured port is genuinely dead.

    Red in BOTH drift shapes: the sibling is live while the configured port is dead (the
    operator must repoint), and the sidecar already adopted the sibling at main() (the
    operator must pin it, or the next relaunch depends on probe order again)."""
    if not cdp_platforms_advertised(cfg):
        return CheckResult(CHECK_CDP_PORT_DRIFT, _TITLES[CHECK_CDP_PORT_DRIFT],
                           SEVERITY_WARN, STATUS_SKIP,
                           "skipped — this box advertises no CDP platform")
    cdp_url = getattr(cfg, "cdp_url", "")
    adoption_note = getattr(cfg, "cdp_url_drift_note", None)
    if cdp_state != "ok" and alternate_alive:
        return CheckResult(
            CHECK_CDP_PORT_DRIFT, _TITLES[CHECK_CDP_PORT_DRIFT], SEVERITY_WARN,
            STATUS_FAIL,
            f"configured {cdp_url}; a CDP endpoint IS answering at {alternate_alive}",
            _REMEDY_CDP_DRIFT.format(port=_port_of(alternate_alive)))
    if adoption_note:
        return CheckResult(CHECK_CDP_PORT_DRIFT, _TITLES[CHECK_CDP_PORT_DRIFT],
                           SEVERITY_WARN, STATUS_FAIL, adoption_note,
                           _REMEDY_CDP_DRIFT.format(port=_port_of(cdp_url)))
    return CheckResult(CHECK_CDP_PORT_DRIFT, _TITLES[CHECK_CDP_PORT_DRIFT], SEVERITY_WARN,
                       STATUS_PASS, cdp_url)


def check_browser(cfg, probes: Probes, *, cdp_ok: bool, platforms: tuple) -> tuple:
    """``cdp_attachable`` + one ``login.<platform>`` per advertised CDP platform, all from
    ONE bounded connect_over_cdp.

    B6/D3, the failure this codebase has been burned by: an HTTP 200 on /json/version is
    NOT sufficient — a stale/degraded Chrome answers HTTP while REJECTING
    connect_over_cdp. ``ChromeManager::ensure_running`` gets this right; the sidecar never
    checked it, and that is the exact condition under which every job nacks on a box that
    looks perfect. Hence FATAL.

    The login checks are WARN because their cookie signatures for linkedin (li_at) and x
    (auth_token) could not be validated against a live session (risk 1): a wrong name
    yields a permanent false logged_out, and a red badge on a working box must never park
    it. The wizard blocks on them; an unattended 4am relaunch must not (rule 1)."""
    login_ids = tuple(f"{CHECK_LOGIN_PREFIX}{p}" for p in platforms)

    def _logins(status: str, detail: Optional[str]) -> tuple:
        return tuple(
            CheckResult(f"{CHECK_LOGIN_PREFIX}{p}", f"Chrome is signed in to {p}",
                        SEVERITY_WARN, status, detail,
                        None if status == STATUS_PASS or status == STATUS_SKIP
                        else _REMEDY_LOGIN.format(platform=p))
            for p in platforms)

    def _attach(status: str, detail: Optional[str], remedy: Optional[str] = None):
        return CheckResult(CHECK_CDP_ATTACHABLE, _TITLES[CHECK_CDP_ATTACHABLE],
                           SEVERITY_FATAL, status, detail, remedy)

    if not platforms:
        return (_attach(STATUS_SKIP, "skipped — this box advertises no CDP platform"),)
    if probes.run_active():
        # check_readiness' single-browser invariant: a live run already owns the one CDP
        # connection this architecture allows, and attaching a second client mid-run
        # risks exactly the hiccup TASK A hardened against. A manual re-check from the
        # desktop UI mid-job must not do that.
        reason = "a job is running — not opening a second CDP connection"
        return (_attach(STATUS_SKIP, reason),) + _logins(STATUS_SKIP, reason)
    if not cdp_ok:
        reason = "skipped — CDP endpoint is unreachable"
        return (_attach(STATUS_SKIP, reason),) + _logins(STATUS_UNKNOWN, reason)
    if not probes.playwright_available():
        reason = "Playwright is unavailable — cannot attach to Chrome"
        return (_attach(STATUS_UNKNOWN, reason),) + _logins(STATUS_UNKNOWN, reason)

    cdp_url = getattr(cfg, "cdp_url", "")
    probe = probes.browser(cdp_url, tuple(platforms), _BROWSER_TIMEOUT_SEC)
    if probe.error == _PROBE_UNAVAILABLE:
        # Version skew, not a broken box (see _default_browser_probe).
        reason = "this sidecar's readiness module has no browser probe"
        return (_attach(STATUS_UNKNOWN, reason),) + _logins(STATUS_UNKNOWN, reason)
    if not probe.attached and _is_attach_timeout(probe.error):
        # A TIMEOUT and a REFUSAL are different facts and must not share a severity. A
        # refused attach is B6/D3: Chrome answered and said no, the box genuinely cannot
        # run a job — fatal. A timeout says only that OUR clock ran out, and our clock is
        # tighter than the real harvest attach's (core/cdp.py: nav_timeout+10s, budgets
        # its own comment calls generous "for a slow worker PC"), so the job this gates
        # would very likely have succeeded. unknown never blocks (rule 3): the row still
        # goes amber in the wizard and the fleet console, and the 30s re-probe settles
        # it. A false fatal on a healthy box is the worst thing this feature can do.
        detail = (f"connect_over_cdp to {cdp_url} did not finish within "
                  f"{_BROWSER_TIMEOUT_SEC:.0f}s ({probe.error})")
        return ((_attach(STATUS_UNKNOWN, detail,
                         _REMEDY_CDP_ATTACH_SLOW.format(
                             cdp_url=cdp_url, seconds=int(_BROWSER_TIMEOUT_SEC))),)
                + _logins(STATUS_UNKNOWN, "could not read the browser session"))
    if not probe.attached:
        detail = f"connect_over_cdp to {cdp_url} failed"
        if probe.error:
            detail += f" ({probe.error})"
        return ((_attach(STATUS_FAIL, detail,
                         _REMEDY_CDP_ATTACHABLE.format(cdp_url=cdp_url)),)
                + _logins(STATUS_UNKNOWN, "could not read the browser session"))

    results = [_attach(STATUS_PASS, cdp_url)]
    logins = probe.logins or {}
    for check_id, platform in zip(login_ids, platforms):
        state = str(logins.get(platform) or "unknown")
        if state == "logged_in":
            status, detail = STATUS_PASS, "logged_in"
        elif state == "logged_out":
            status, detail = STATUS_FAIL, "logged_out"
        else:
            status, detail = STATUS_UNKNOWN, "could not read the browser session"
        results.append(CheckResult(
            check_id, f"Chrome is signed in to {platform}", SEVERITY_WARN, status, detail,
            None if status == STATUS_PASS else _REMEDY_LOGIN.format(platform=platform)))
    return tuple(results)


# ---- composition ----

def _guarded(check_id: str, fn: Callable[[], Any]) -> tuple:
    """Run one check, converting ANY exception into a warn-level failure (rule 6).

    This is what makes "the preflight's own bug is a warning" true mechanically rather
    than by review: a fatal check that raises is demoted to warn, so a bug here can never
    park a healthy box. Returns a tuple because check_browser yields several results."""
    try:
        out = fn()
    except Exception as e:  # noqa: BLE001 — deliberately total; see docstring
        log.warning("preflight check %s raised %s", check_id, type(e).__name__)
        return (CheckResult(check_id, _TITLES.get(check_id, check_id), SEVERITY_WARN,
                            STATUS_FAIL, f"the check itself raised {type(e).__name__}",
                            _REMEDY_CHECK_RAISED.format(error=type(e).__name__)),)
    return out if isinstance(out, tuple) else (out,)


def _safe(fn: Callable[[], Any], default: Any) -> Any:
    """An injected probe that raises must not sink the whole report."""
    try:
        return fn()
    except Exception as e:  # noqa: BLE001
        log.warning("preflight probe raised %s — treating as %r", type(e).__name__, default)
        return default


def run_preflight(cfg, *, probes: Probes = Probes(),
                  enforce: Optional[bool] = None) -> PreflightReport:
    """Runs EVERY check — never short-circuits; the operator wants the whole list.
    NEVER raises: each check is individually try/except'd into a warn-level result.

    Worst case ~34.5s (cdp 3s + alternate 1.5s + one shared Playwright attach 30s), which
    deliberately EXCEEDS the 30s re-probe interval — the browser budget is pinned to the
    real harvest attach's (see _BROWSER_TIMEOUT_SEC) rather than trimmed to fit a tick,
    because a gate stricter than the job it admits only manufactures false fatals. The
    park sleeps before it probes, so a longer probe stretches the cycle instead of
    overlapping the previous one. ~1ms on an API-only box, which is what the whole
    existing test suite is."""
    started = time.monotonic()
    ran_at = time.time()
    if enforce is None:
        enforce = not _falsey(_safe(lambda: probes.env(PREFLIGHT_ENFORCE_ENV), ""))
    platforms = cdp_platforms_advertised(cfg)
    run_active = bool(_safe(probes.run_active, False))

    checks: list = []
    checks.extend(_guarded(CHECK_STATE_DIR, lambda: check_state_dir(cfg)))
    checks.extend(_guarded(CHECK_TOKEN_PERSISTENCE,
                           lambda: check_token_persistence(cfg, probes)))
    token_ok = any(c.id == CHECK_TOKEN_PERSISTENCE and c.status == STATUS_PASS
                   for c in checks)
    checks.extend(_guarded(CHECK_DISPATCH_CREDENTIAL,
                           lambda: check_dispatch_credential(cfg, probes,
                                                             token_ok=token_ok)))
    checks.extend(_guarded(CHECK_CAPABILITIES, lambda: check_capabilities(cfg, probes)))
    checks.extend(_guarded(CHECK_LLM_BACKEND, lambda: check_llm_backend(cfg, probes)))
    checks.extend(_guarded(CHECK_PLAYWRIGHT, lambda: check_playwright(cfg, probes)))

    # The two candidate ports are probed AT MOST once each, here, and the results are
    # handed to the two pure checks below — never a scan, and never a second probe from
    # inside a check. The alternate is only probed when the configured one is dead.
    cdp_state = "unreachable"
    alternate_alive: Optional[str] = None
    if platforms:
        cdp_url = getattr(cfg, "cdp_url", "")
        cdp_state = str(_safe(lambda: probes.cdp(cdp_url, _CDP_TIMEOUT_SEC), "unreachable"))
        if cdp_state != "ok":
            alt = _alternate_cdp_url(cdp_url)
            if alt and _safe(lambda: probes.cdp(alt, _ALT_CDP_TIMEOUT_SEC),
                             "unreachable") == "ok":
                alternate_alive = alt
    checks.extend(_guarded(CHECK_CDP_REACHABLE,
                           lambda: check_cdp_reachable(cfg, cdp_state=cdp_state,
                                                       alternate_alive=alternate_alive)))
    checks.extend(_guarded(CHECK_CDP_PORT_DRIFT,
                           lambda: check_cdp_port_drift(cfg, cdp_state=cdp_state,
                                                        alternate_alive=alternate_alive)))
    browser_checks = _guarded(
        CHECK_CDP_ATTACHABLE,
        lambda: check_browser(cfg, probes, cdp_ok=(cdp_state == "ok"),
                              platforms=platforms))
    if len(browser_checks) == 1 and browser_checks[0].id == CHECK_CDP_ATTACHABLE \
            and browser_checks[0].severity == SEVERITY_WARN and platforms and not run_active:
        # check_browser itself raised (the _guarded fallback is a single warn result) and
        # the login rows would silently vanish from the operator's list. Re-add them as
        # unknown so the UI's row count is stable across a crashing probe.
        browser_checks = browser_checks + tuple(
            CheckResult(f"{CHECK_LOGIN_PREFIX}{p}", f"Chrome is signed in to {p}",
                        SEVERITY_WARN, STATUS_UNKNOWN,
                        "could not read the browser session",
                        _REMEDY_LOGIN.format(platform=p))
            for p in platforms)
    checks.extend(browser_checks)

    duration_ms = int((time.monotonic() - started) * 1000)
    return PreflightReport(checks=tuple(checks), ran_at=ran_at, duration_ms=duration_ms,
                           enforced=bool(enforce))


def error_report(exc: BaseException, *, enforced: bool = True) -> PreflightReport:
    """The synthetic warn-level report ``Sidecar._refresh_preflight`` substitutes when the
    injected preflight callable raised (check 11). Belt-and-braces on top of
    run_preflight's own guards: even a crash in the composition itself leaves the box
    leasing with FULL capabilities, because this report has no fatal check in it."""
    return PreflightReport(
        checks=(CheckResult(CHECK_PREFLIGHT_ERROR, _TITLES[CHECK_PREFLIGHT_ERROR],
                            SEVERITY_WARN, STATUS_FAIL,
                            f"the preflight raised {type(exc).__name__}",
                            _REMEDY_CHECK_RAISED.format(error=type(exc).__name__)),),
        ran_at=time.time(), duration_ms=0, enforced=enforced)


def resolve_cdp_url(cfg, *, probes: Probes = Probes()):
    """F10 port adoption. Returns cfg UNCHANGED when ``cfg.cdp_url_explicit`` is True,
    when the configured URL answers, or when the sibling port does not. Otherwise returns
    ``dataclasses.replace(cfg, cdp_url=<sibling>, cdp_url_drift_note=<one sentence>)`` and
    logs it at WARNING.

    Called exactly twice in the codebase: once in ``Sidecar.run()`` BEHIND the
    control-surface bind (it does up to 4.5s of blocking port probing, and a box with a red
    preflight and a dead cloud still has to be able to tell an operator why), and once per
    ``_park_for_preflight`` tick (safe: parking means nothing is leasing and no job is
    live).

    Only an UNSET cdp_url auto-adopts. An operator who explicitly pinned a port and got it
    wrong gets a named FATAL error instead of a silent repoint — silently overriding an
    explicit setting is how you lose an afternoon. The worker adopts at all (where the CLI
    and panel only hint) because nobody can SSH into these PCs: parking a box over a port
    literal is precisely the brick this work exists to prevent."""
    try:
        if getattr(cfg, "cdp_url_explicit", False):
            return cfg
        cdp_url = getattr(cfg, "cdp_url", "")
        if _safe(lambda: probes.cdp(cdp_url, _CDP_TIMEOUT_SEC), "unreachable") == "ok":
            return cfg
        alt = _alternate_cdp_url(cdp_url)
        if not alt:
            return cfg
        if _safe(lambda: probes.cdp(alt, _ALT_CDP_TIMEOUT_SEC), "unreachable") != "ok":
            return cfg
        note = (f"adopted {alt}: nothing answered CDP at the default {cdp_url} and a CDP "
                f"endpoint is live on {_port_of(alt)} — pin cdp_port so this cannot drift")
        log.warning("CDP port drift: %s", note)
        try:
            return dataclasses.replace(cfg, cdp_url=alt, cdp_url_drift_note=note)
        except TypeError:
            # An older WorkerConfig without cdp_url_drift_note (the field lands with the
            # §1 port unification). Adopt the port anyway — the drift check still reports
            # it from the live probe results, just without the adoption receipt.
            return dataclasses.replace(cfg, cdp_url=alt)
    except Exception as e:  # noqa: BLE001 — resolution is an optimisation, never a gate
        log.warning("CDP port resolution raised %s — keeping the configured URL",
                    type(e).__name__)
        return cfg
