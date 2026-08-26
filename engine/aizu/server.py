"""Local bridge server.

Serves the built React panel (admin-panel/dist) as a single-page app and
exposes the live engine state as JSON at `/api/state` (built from the DB via
`build_raw`). The panel fetches that endpoint directly — no data injection.
Unknown non-API GET paths fall back to `index.html` so client-side routes
(e.g. /matches, /health) resolve on a hard refresh.

Write surfaces persist to SQLite (status-mark, campaign upsert, team, settings,
integrations). PRD §5 revision (v5): the server may also act as a *control plane*
— `POST /api/run` spawns the engine for one campaign or all live ones (dry-run by
default, live as an explicit opt-in, single-run lock; see runner.RunManager). The
in-flight + recent run status is added to `/api/state` as the additive `RUN` block;
SQLite remains the durable record of what a run did. Stdlib only — no web framework.

  python -m aizu.cli panel --db aizu.db --panel-dir ../admin-panel/dist
"""
from __future__ import annotations

import errno
import hmac
import json
import logging
import math
import os
import re
import sqlite3
import ssl
import threading
import time
import uuid
from datetime import datetime, timezone
from http.cookies import CookieError, SimpleCookie
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable, Iterable, Optional
from urllib.parse import parse_qs, unquote, urlparse

from . import campaign_gen, connections, rbac
from .core.router import build_router, _parse_csv_env
from .auth import (LoginThrottle, MAX_PASSWORD_LENGTH, MIN_PASSWORD_LENGTH,
                   SESSION_TTL_SECONDS, hash_password, new_session_token,
                   session_expiry, verify_password)
from . import admin_auth
from .core.config import (CDP_PLATFORMS, MAX_CAMPAIGN_BRIEF_BYTES, PER_ORG_CREDENTIAL_PLATFORMS,
                     SUPPORTED_PLATFORMS, Campaign, campaign_from_brief, campaign_to_brief,
                     load_campaign, load_soul, resolve_campaign)
from . import billing
from .panel import (build_empty_raw, build_raw, delivery_state, lead_uid,
                    org_flag_summary)
from .panel_org import (LEADS_PAGE_SIZE_DEFAULT, build_admin_org_campaigns,
                        build_admin_org_leads, build_campaigns_org,
                        build_dashboard_org, build_leads_org, build_reports_org,
                        build_settings_org)
from .runner import RunManager, RunSpec, VALID_RUN_MODES
from . import readiness
from .secrets import SecretCipherError
from .engines.telegram.login import TelegramLoginError, TelegramLoginManager
from .core.logsetup import configure_logging, get_logger
from .core.store import (CONTROL_FLAG_SCOPES, DEFAULT_PLATFORM,
                    EXECUTION_BACKENDS, EXECUTION_DISTRIBUTED, EXECUTION_IN_PROCESS,
                    FORCED_REASON_STATUS,
                    MAX_NOTE_LENGTH, MAX_SYNC_LEADS, MAX_SYNC_SPEND_ROWS,
                    REVEAL_ACTION,
                    VALID_CAMPAIGN_STATUS, VALID_STATUS,
                    WORKER_HEARTBEAT_INTERVAL_SEC, WORKER_TOKEN_TTL_SEC,
                    default_lease_ttl_sec, Store)
from .core.schedule import SCHEDULE_KINDS, next_fire

log = get_logger(__name__)

STATE_PATH = "/api/state"
# Per-page read endpoints (org-wide aggregate) that supersede the monolithic STATE_PATH.
DASHBOARD_PATH = "/api/dashboard"
CAMPAIGNS_PATH = "/api/campaigns"
LEADS_PATH = "/api/leads"
REPORTS_PATH = "/api/reports"
# /api/settings (SETTINGS_PATH, defined below) is the GET source for the Settings page
# too — do_GET and do_POST dispatch separately, so the read and write share one path.
STATUS_PATH = "/api/status"
STATUS_BULK_PATH = "/api/status/bulk"
LEAD_NOTE_PATH = "/api/lead/note"
LEAD_REVEAL_PATH = "/api/lead/reveal"   # v27: audited, per-lead un-redaction
CAMPAIGN_PATH = "/api/campaign"
CAMPAIGN_GENERATE_PATH = "/api/campaign/generate"
CAMPAIGN_INTERVIEW_PATH = "/api/campaign/interview"
CAMPAIGN_ARCHIVE_PATH = "/api/campaign/archive"
CAMPAIGN_SCHEDULE_PATH = "/api/campaign/schedule"
TEAM_PATH = "/api/team"
INVITE_PATH = "/api/invite"
ORG_PATH = "/api/org"
SETTINGS_PATH = "/api/settings"
INTEGRATION_PATH = "/api/integration"
TELEGRAM_START_PATH = "/api/integration/telegram/start"
TELEGRAM_VERIFY_PATH = "/api/integration/telegram/verify"
BILLING_CHECKOUT_PATH = "/api/billing/checkout"
BILLING_PORTAL_PATH = "/api/billing/portal"
BILLING_WEBHOOK_PATH = "/api/billing/webhook"   # PUBLIC; provider-signed, no session
RUN_PATH = "/api/run"
RUN_ACTIVITY_PATH = "/api/run/activity"
RUN_STOP_PATH = "/api/run/stop"
RUN_PAUSE_PATH = "/api/run/pause"
RUN_RESUME_PATH = "/api/run/resume"
# "Can a live run start right now?" — the readiness contract the panel's global
# AgentReadinessBanner polls, and the same check POST /api/run gates on (409
# agent_not_ready). What it measures depends on the execution backend: the local
# warmed Chrome in_process, or worker-fleet presence when distributed.
AGENT_READINESS_PATH = "/api/agent/readiness"
AGENT_LAUNCH_LOGIN_PATH = "/api/agent/launch-login"
AUTH_SIGNUP_PATH = "/api/auth/signup"
AUTH_LOGIN_PATH = "/api/auth/login"
AUTH_LOGOUT_PATH = "/api/auth/logout"
AUTH_ME_PATH = "/api/auth/me"
# v14 distributed-workers plane. Bearer-token gated (NOT the cookie/RBAC gate).
WORKER_REGISTER_PATH = "/api/worker/register"
WORKER_HEARTBEAT_PATH = "/api/worker/heartbeat"   # WORKER-level presence (NOT job-scoped)
WORKER_LEASE_PATH = "/api/worker/lease"           # v14 Phase 3: pull one job
# Job-scoped worker routes: /api/worker/jobs/{id}/{heartbeat|ack|nack|credential}
# (v14 Phase 3; `credential` added by the SECURITY REVIEW CRITICAL/HIGH fix — see
# Handler._handle_job_credential — replacing the server-side credential bake).
WORKER_JOBS_PREFIX = "/api/worker/jobs/"
_WORKER_JOB_ACTIONS = ("heartbeat", "ack", "nack", "credential")
ADMIN_FLEET_PATH = "/api/admin/fleet"
ADMIN_ENQUEUE_PATH = "/api/admin/jobs/enqueue"    # v14 Phase 3: operator enqueue
ADMIN_CONTROL_FLAGS_PATH = "/api/admin/control-flags"  # v14 Phase 4: set/clear/list flags
ADMIN_WORKER_REVOKE_PATH = "/api/admin/workers/revoke"  # v14 Phase 4: revoke a worker token
# v22 (BUILD-PLAN B8 fix): per-worker, single-use, admin-minted enrolment tokens —
# POST mints, GET lists (never the plaintext/hash); revoke is a separate POST.
ADMIN_WORKER_ENROLMENT_TOKENS_PATH = "/api/admin/worker-enrolment-tokens"
ADMIN_WORKER_ENROLMENT_TOKEN_REVOKE_PATH = "/api/admin/worker-enrolment-tokens/revoke"
ADMIN_EXECUTION_BACKEND_PATH = "/api/admin/execution-backend"  # v16: run routing switch
ADMIN_MODEL_COMPARISON_PATH = "/api/admin/model-comparison"     # v17: fan-out on/off switch
ADMIN_MODEL_COMPARISON_STATS_PATH = "/api/admin/model-comparison/stats"  # v17: Model Performance page
# v15 superadmin plane: a SEPARATE auth surface (MFA + IP-allowlist), cookie
# rr_admin_session, resolved by _current_admin — never the org cookie/RBAC gate.
ADMIN_LOGIN_PATH = "/api/admin/login"
ADMIN_LOGOUT_PATH = "/api/admin/logout"
ADMIN_WHOAMI_PATH = "/api/admin/whoami"
ADMIN_IMPERSONATE_PATH = "/api/admin/impersonate"          # v15 Phase 5c: start
ADMIN_IMPERSONATE_END_PATH = "/api/admin/impersonate/end"  # v15 Phase 5c: end
ADMIN_AUDIT_PATH = "/api/admin/audit"                      # v15 Phase 5c: list
ADMIN_AUDIT_VERIFY_PATH = "/api/admin/audit/verify"        # v15 Phase 5c: chain check
ADMIN_ORGS_PATH = "/api/admin/orgs"                        # v15 Phase 5d: cross-org index
ADMIN_ORGS_PREFIX = "/api/admin/orgs/"                     # /{id}/{campaigns|leads|runs}
_ADMIN_ORG_SUBRESOURCES = ("campaigns", "leads", "runs")
# v27 run-log redaction: the narrative run feed is a SUPERADMIN surface now. The org
# plane's /api/run/activity returns scalars only (see _serve_run_activity), so this is
# the ONLY route that still hands out event messages/details — hence the admin gate.
ADMIN_RUN_ACTIVITY_PATH = "/api/admin/run/activity"        # ?runId=&after=
# Lease long-poll: a worker may ask us to hold the request open this long before
# returning an empty lease. Clamped so a hostile/buggy worker can't pin a thread.
WORKER_LEASE_POLL_MAX_SEC = 30
_WORKER_LEASE_POLL_STEP_SEC = 0.5
SESSION_COOKIE = "rr_session"
MAX_BODY_BYTES = 64 * 1024     # bulk status writes can carry many comment ids
# Worker register/presence carry a capability matrix + metric blobs, and a job ack
# now carries the captured-lead bodies (Phase 3 sync-back, capped at MAX_SYNC_LEADS).
# Give the worker plane its own ceiling well above the 64 KB default but far below the
# AI-generate cap — 1 MB comfortably holds a full MAX_SYNC_LEADS lead batch.
WORKER_MAX_BODY_BYTES = 1024 * 1024
# RETIRED in v15 (Phase 5d): the interim env allowlist gate was replaced by the real
# platform-admin plane (admin_auth + platform_admins + _require_admin). Kept only as a
# named constant so an operator can still `unset` a stale value; no code reads it now.
PLATFORM_ADMINS_ENV = "AIZU_PLATFORM_ADMINS"
# Phase-2 first-register bootstrap secret. A brand-new box with no token yet must
# present this in `Authorization: Bearer <secret>`; unset ⇒ first-register is closed.
WORKER_BOOTSTRAP_ENV = "AIZU_WORKER_BOOTSTRAP_TOKEN"
# v22 (BUILD-PLAN B8 fix): the shared bootstrap secret is now a FALLBACK, tried only
# after a per-worker enrolment token fails to redeem. Defaults ON (unset/'1'/'true' =
# enabled; only '0'/'false'/'no' disables it) so every existing/in-flight box keeps
# registering with zero operator action on upgrade day — the operator flips this off
# only once every box has been re-enrolled with its own token.
WORKER_LEGACY_BOOTSTRAP_ENV = "AIZU_WORKER_LEGACY_BOOTSTRAP_ENABLED"
DEFAULT_ENROLMENT_TTL_HOURS = 168   # 7 days
MAX_ENROLMENT_TTL_HOURS = 720       # 30 days — a bounded standing-risk window
# Phase 4: minimum acceptable agent version. A worker below it keeps running its current
# job but every heartbeat carries updateRequired:true so the desktop app can update. Unset
# ⇒ no gate. Compared as a dotted numeric tuple (see _agent_version_below).
MIN_AGENT_VERSION_ENV = "AIZU_MIN_AGENT_VERSION"
# Defensive caps for worker register fields (on top of the body cap).
_WORKER_MAX_STR = 200
_WORKER_MAX_SESSIONS_CEIL = 50
_WORKER_MAX_CAPABILITIES = 100
# v23 worker launch preflight (ledger F9/F10/F12): the compact self-check summary a box
# carries on register/heartbeat. Mirrors worker/preflight.py's own MAX_UPSTREAM_* caps —
# the worker already trims to these before sending, so a body over budget here is a box
# we do not recognise, not a box we should reject.
_WORKER_MAX_PREFLIGHT_FAILED = 16
_WORKER_MAX_PREFLIGHT_DETAIL = 200
_WORKER_MAX_PREFLIGHT_BYTES = 8192
_WORKER_PREFLIGHT_SEVERITIES = frozenset({"fatal", "warn"})
# Only non-passing rows ride the wire, so `pass`/`skip` never legitimately appear here —
# but accept them rather than drop the row, since a dropped row loses a diagnostic and
# gains nothing. An unrecognised/absent status reads as "fail" (see the docstring).
_WORKER_PREFLIGHT_STATUSES = frozenset({"fail", "unknown", "pass", "skip"})
# The closed set of non-login check ids. Kept as literals rather than imported from
# worker/preflight.py so the BRIDGE never takes a static dependency on the sidecar
# package it validates input from — but drift is caught mechanically by
# tests/worker/test_worker_server.py::test_preflight_check_id_whitelist_covers_the_preflight_module,
# which asserts every CHECK_* constant in that module appears here. Per-platform login
# rows (`login.<platform>`) are matched separately against CDP_PLATFORMS below.
_WORKER_PREFLIGHT_CHECK_IDS = frozenset({
    "state_dir_writable", "token_persistence", "dispatch_credential", "capabilities",
    "llm_backend", "playwright", "cdp_reachable", "cdp_port_drift", "cdp_attachable",
    "chrome_profile", "preflight_error",
})
_WORKER_PREFLIGHT_LOGIN_PREFIX = "login."
# AI campaign generation can carry a base64 product screenshot, far over the
# default body cap — so /api/campaign/generate gets its own larger ceiling.
GENERATE_MAX_BODY_BYTES = 8 * 1024 * 1024
_GEN_MAX_URL = 2048
_GEN_MAX_TEXT = 8000
_GEN_MAX_IMAGE_B64 = 6_000_000          # ~6 MB base64 ≈ ~4.5 MB raw image
_GEN_MAX_ID_HINT = 200
# Conversational-interview caps (the panel echoes the serialized context back, and
# carries the running Q&A transcript, on each round).
_GEN_MAX_CONTEXT = 16_000
_GEN_MAX_QA = 30                        # max Q&A pairs in a transcript
_GEN_MAX_QA_LEN = 4000                  # max chars per question or answer
_GEN_MAX_PLATFORMS = 6
_GEN_MAX_PLATFORM_LEN = 40
GENERATE_SPEND_CAP_USD = 2.0            # bound the cost of one generate call
# The ceiling a worker box falls back to when it has no AIZU_SPEND_CAP of its own —
# kept in lockstep with worker.config.WorkerConfig.spend_cap's default. Deliberately NOT
# used as a cloud-side fallback: see _fleet_spend_cap_usd for why guessing the fleet's
# cap from the bridge's env would permanently 409 a hosted deployment.
DEFAULT_FLEET_SPEND_CAP_USD = 20.0
_DATA_URL_PREFIX = re.compile(r"^data:image/[A-Za-z0-9.+-]+;base64,", re.IGNORECASE)
# Max chars of a request/response body to echo in the DEBUG firehose (secrets are
# scrubbed by the logging RedactingFilter before any line is emitted).
_BODY_LOG_MAX = 4000
# Max chars of the request PATH to echo in any log line. The path is 100%
# attacker-controlled and can be ~64 KB (http.server's request-line ceiling);
# rendering that through the console handler is per-character work done while
# holding the GIL, so an anonymous client could stall every thread in the process
# just by asking for a very long URL. The tail of such a path has no diagnostic
# value — log a bounded prefix plus the true length. (core.logsetup's
# LineCapFormatter is the second, sink-side belt on the same braces.)
_LOG_PATH_MAX = 300
MAX_RUN_DURATION_MINUTES = 720  # 12h ceiling on a timed run (the panel offers up to 4h)
MAX_RUN_LEAD_TARGET = 1000  # ceiling on a lead-target run (the panel offers up to 100)
INVITE_TTL_SECONDS = 14 * 24 * 3600     # an invite link is valid for 14 days


def _log_path(path: Any) -> str:
    """A request path trimmed to `_LOG_PATH_MAX` for safe logging.

    Used EVERYWHERE `self.path` reaches a log line: the access log, the API-error
    line, and the DEBUG request/response firehose."""
    text = path if isinstance(path, str) else str(path)
    if len(text) <= _LOG_PATH_MAX:
        return text
    return f"{text[:_LOG_PATH_MAX]}…(truncated, {len(text)} chars total)"


def _json_sanitize(value: Any) -> Any:
    """Deep-copy `value` with every non-finite float replaced by None."""
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, dict):
        return {k: _json_sanitize(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_sanitize(v) for v in value]
    return value


def _json_bytes(payload: Any) -> bytes:
    """Serialize a response body, guaranteeing the result is *valid* JSON.

    `allow_nan=False` makes the encoder REFUSE a non-finite float rather than emit
    a bare `NaN`/`Infinity` token, which is invalid JSON per RFC 8259 and which
    every strict parser — the panel's included — rejects outright. One such value
    in one row used to brick an entire org's panel (GET /api/state answering 200
    with an unparseable body) with no in-app way to fix it.

    The numeric boundaries (`_finite_number`) are the real gate; this is the
    backstop that keeps the guarantee true for values arriving from any other
    door (an older row already in the DB, a worker report, a computed ratio). On
    that path we scrub and re-encode instead of failing the response: a null is a
    value the panel can render, an unparseable body is not."""
    try:
        return json.dumps(payload, ensure_ascii=False, allow_nan=False).encode("utf-8")
    except ValueError:
        log.error("response body carried a non-finite number — scrubbed to null "
                  "(invalid JSON would have broken the panel's parser)")
        return json.dumps(_json_sanitize(payload), ensure_ascii=False,
                          allow_nan=False).encode("utf-8")


# Invite-creation rate limit (mirrors LoginThrottle, keyed by actor user id):
# at most MAX_INVITE_CREATES creates per actor inside a rolling INVITE_RATE_WINDOW.
MAX_INVITE_CREATES = 10
INVITE_RATE_WINDOW = 3600.0     # seconds (rolling window for the create count)

# Per-route minimum authorization (the route gate; finer per-op checks live in the
# handlers — e.g. owner-only edits of an admin). action names mirror rbac.PERMISSIONS.
_ROUTE_ACTIONS = {
    STATUS_PATH: "edit_leads",
    STATUS_BULK_PATH: "bulk_edit_leads",
    LEAD_NOTE_PATH: "edit_leads",
    # LEAD_REVEAL_PATH is DELIBERATELY absent. Its `reveal_lead` check runs inside
    # `_handle_lead_reveal` instead, because a refused reveal must still write an
    # audit row — and this table's gate 403s before any handler runs, so a denial
    # here would be invisible. A denied reveal attempt is the row an operator most
    # wants to find later; it must not be the one row we don't keep.
    CAMPAIGN_PATH: "edit_campaigns",
    CAMPAIGN_GENERATE_PATH: "edit_campaigns",   # AI-draft a campaign = creating one
    CAMPAIGN_INTERVIEW_PATH: "edit_campaigns",   # interview step of campaign creation
    CAMPAIGN_ARCHIVE_PATH: "edit_campaigns",    # archive/un-archive is a campaign edit
    CAMPAIGN_SCHEDULE_PATH: "edit_campaigns",    # arming a recurring schedule is an edit
    TEAM_PATH: "invite_member",        # floor; per-op checks gate admin edits
    INVITE_PATH: "invite_member",
    ORG_PATH: "edit_settings",
    SETTINGS_PATH: "edit_settings",
    INTEGRATION_PATH: "toggle_integration",
    TELEGRAM_START_PATH: "toggle_integration",
    TELEGRAM_VERIFY_PATH: "toggle_integration",
    BILLING_CHECKOUT_PATH: "manage_billing",
    BILLING_PORTAL_PATH: "manage_billing",
    # BILLING_WEBHOOK_PATH is intentionally absent — it is PUBLIC (provider-signed)
    # and handled before the protected gate; it must NOT require a session/role.
    RUN_PATH: "run_campaigns",
    RUN_STOP_PATH: "run_campaigns",
    RUN_PAUSE_PATH: "run_campaigns",
    RUN_RESUME_PATH: "run_campaigns",
    AGENT_LAUNCH_LOGIN_PATH: "fix_agent",   # owner/admin: open a Chrome login tab
}
# Company profile field caps (defensive, on top of the 64 KB body cap).
_MAX_COMPANY_NAME = 200
_MAX_COMPANY_DESC = 2000
_MAX_COMPANY_LOGO = 8192

# A real PBKDF2 hash of a throwaway secret. A login for an unknown email still
# runs one verify against this so response timing doesn't reveal which emails
# have accounts (defeats timing-based user enumeration). Computed once at import.
_DUMMY_PASSWORD_HASH = hash_password(new_session_token())

# Settings keys the panel may write (whitelist). productName/timezone are strings,
# pacing is an object merged into CONFIG.pacing; the rest are numeric thresholds.
_SETTINGS_STR = {"productName", "timezone"}
_SETTINGS_NUM = {"matchThreshold", "skipRatioThreshold", "budgetCapUsd",
                 "canaryLimitReels", "watchlistTtlDays"}
_SETTINGS_KEYS = _SETTINGS_STR | _SETTINGS_NUM | {"pacing"}
_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
_BULK_MAX = 500

# The React dev server (vite) runs on another local port and talks to this
# bridge cross-origin; anything not loopback-local gets no CORS grant. The regex
# anchors the whole origin so a lookalike host (e.g. http://127.0.0.1.evil.com)
# can't satisfy a naive startswith and slip past the cross-origin guard.
_LOCAL_ORIGIN_RE = re.compile(r"^https?://(127\.0\.0\.1|localhost|\[::1\])(:\d+)?$")

# Origins a hosted deployment serves the panel from, beyond loopback. The bridge
# was built local-first, so a served-over-the-network panel would otherwise have
# every POST rejected by the cross-origin guard below. Opt-in only: unset (the
# local-first default) leaves the loopback-only posture exactly as it was.
# CSV, each entry an exact scheme://host[:port], e.g. "https://aizu.uz".
ALLOWED_ORIGINS_ENV = "AIZU_ALLOWED_ORIGINS"


def _is_local_origin(origin: str) -> bool:
    return bool(_LOCAL_ORIGIN_RE.match(origin))


def _is_allowed_origin(origin: str) -> bool:
    """Loopback, or an origin the operator named in AIZU_ALLOWED_ORIGINS. Matched
    whole and case-insensitively per RFC 6454 (scheme/host are case-insensitive;
    a trailing slash is not part of an origin, so it is trimmed on both sides).
    Never a prefix/suffix test — http://aizu.uz.evil.com must not match."""
    if _is_local_origin(origin):
        return True
    candidate = origin.rstrip("/").lower()
    return any(allowed.rstrip("/").lower() == candidate
               for allowed in _parse_csv_env(ALLOWED_ORIGINS_ENV))


def _query_int(values: Optional[list[str]], default: int) -> int:
    """First value of a parsed query param as a positive int, else `default`. Lenient:
    a missing/blank/non-numeric param never 400s — it falls back (mirrors run-activity)."""
    raw = (values or [None])[0]
    try:
        return max(1, int(raw))  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return default


def _aggregate_run_counters(sessions: list[dict], actions: dict[str, int]) -> dict:
    """Sum a run's session counters (a batch run spans several sessions) into the
    camelCase metrics the activity drawer shows. likes/follows come from the actions
    table (they aren't columns on the sessions row)."""
    agg = {"reelsSeen": 0, "relevancePasses": 0, "commentsScored": 0,
           "matches": 0, "spendUsd": 0.0}
    for s in sessions:
        agg["reelsSeen"] += int(s.get("reels_seen") or 0)
        agg["relevancePasses"] += int(s.get("relevance_passes") or 0)
        agg["commentsScored"] += int(s.get("comments_scored") or 0)
        agg["matches"] += int(s.get("matches") or 0)
        agg["spendUsd"] += float(s.get("spend_usd") or 0.0)
    agg["spendUsd"] = round(agg["spendUsd"], 6)
    agg["likes"] = int(actions.get("like", 0))
    agg["follows"] = int(actions.get("follow", 0))
    return agg


# --- v27 run-log redaction: org-facing run progress ---------------------------------
#
# The narrative run feed is a SUPERADMIN surface now (ADMIN_RUN_ACTIVITY_PATH), but a
# customer must still be able to watch a run work. So the events are folded into
# NUMBERS here, server-side, and only scalars go org-facing. Shipping a "filtered feed"
# instead was the tempting shortcut and is the wrong shape: a comments/success detail is
# literally `{"username", "score", "tier", "reelId"}` and a relevance one is
# `{"reelId", "author"}` — i.e. we would ship exactly the rows we are hiding and trust a
# client-side filter, and a future engine's new detail key would ride along for free.
#
# The events are ALSO the only live signal a fleet-routed run has. Both the session
# counters and the captured `matches` rows travel in the job ACK body (the sidecar
# collects leads from the WORKER's local store), so the cloud reads zero for both for
# the whole run, while run_events land on the ~45s job heartbeat (_sync_job_run_events).
# Measured mid-flight on a live prod run: every counter 0 and /api/leads total 0 against
# 29 landed events, 13 of them matches.

# How many of a run's events the aggregate folds. Always read from the START of the run
# (never from the poll cursor): the numbers are a function of the whole run, not of what
# arrived since the last poll. A growing PREFIX is what makes every number monotonic —
# a run that out-runs this cap freezes its estimate until the authoritative ack-time
# rows overtake it below.
RUN_ACTIVITY_AGGREGATE_EVENTS = 2000
# The per-item id key inside an event detail is platform-specific: reelId (instagram),
# postId (linkedin/x), submissionId (reddit), messageId (telegram), videoId (youtube).
# Deduping on "reelId" alone would collapse every non-instagram run into one bucket, so
# resolve the id through this allow-list. These two keys and `found`/`reelsSeen`/
# `relevancePasses` are the ONLY things ever read out of a detail blob on the org path.
_RUN_EVENT_ITEM_KEYS = ("reelId", "postId", "submissionId", "messageId", "videoId")
# run_events.phase -> the one customer-safe word the panel renders. An explicit ALLOW-
# list, never a passthrough: an unknown (or newly invented) engine phase degrades to
# "working" rather than leaking an internal stage name into a customer's UI.
_ORG_RUN_PHASES = {
    "lifecycle": "starting",
    "feed_walk": "searching",
    "relevance": "searching",
    "comments": "qualifying",
    "engage": "qualifying",
    "halt": "stopped",
}


def _event_detail(event: dict) -> dict:
    """One event's `detail` decoded, or `{}`. It is a TEXT column holding JSON written
    by an engine (or by a worker's sync), so it can be NULL, malformed, or a non-object.
    Never raises — a progress number must not be able to 500 the activity poll."""
    raw = event.get("detail")
    if isinstance(raw, dict):        # already decoded (some callers/tests pass objects)
        return raw
    if not raw:
        return {}
    try:
        decoded = json.loads(raw)
    except (TypeError, ValueError):
        return {}
    return decoded if isinstance(decoded, dict) else {}


def _event_item_id(detail: dict) -> str:
    """The post/reel/video id this event is about, "" when the blob names none. Used
    only as a dedupe key — never emitted."""
    for key in _RUN_EVENT_ITEM_KEYS:
        value = detail.get(key)
        if value:
            return str(value)
    return ""


def _leads_from_match_events(details: Iterable[dict]) -> int:
    """The deduped lead estimate for ONE run, from its match-event `detail` blobs.

    THE single definition of "how many leads did this run find", shared by the run
    activity feed and by the superadmin run picker. It lives on its own precisely
    because the two used to answer differently: the picker summed `sessions.matches`,
    and a dead-lettered run has no session rows in the cloud at all, so a run that
    really found fifteen leads was listed as "0 leads" next to a log that said 15.

    Two shapes arrive on the same (phase=comments, level=success) channel and are
    reconciled with max(), never added:

    * the per-COMMENT match event, deduped on `(item id, username)`. `run_events` is
      append-only per ATTEMPT while the store dedupes via `upsert_match` on comment
      id, so a retry that re-scans a post it already scored emits a SECOND success
      for the same comment — and retries are the normal case (a live run was observed
      on attempt 4 of 5). It under-counts only when one person leaves two qualifying
      comments on one post: the safe direction, and it self-corrects against the real
      rows at ack.
    * the per-POST roll-up ("3 match(es) on reel X"), MAXed per item. It is an
      aggregate OF those same comment events, so adding the two would double-count
      every lead.

    Blobs are accepted raw (JSON text) or decoded — `_event_detail` normalises both
    and never raises, because a progress number must not be able to 500 a poll.
    """
    keys: set[tuple[str, str]] = set()
    rollup: dict[str, int] = {}
    for raw in details:
        # `{"detail": raw}` re-uses the one decoder for both callers: the feed passes
        # already-decoded dicts, the picker passes the TEXT column straight from SQL.
        detail = _event_detail({"detail": raw})
        item = _event_item_id(detail)
        username = detail.get("username")
        if username:
            keys.add((item, str(username)))
        elif isinstance(detail.get("found"), int):
            rollup[item] = max(rollup.get(item, 0), int(detail["found"]))
    return max(len(keys), sum(rollup.values()))


def _aggregate_run_progress(events: list[dict], *, counters: dict[str, Any],
                            lead_rows: int, finished: bool,
                            failed: bool) -> dict[str, Any]:
    """Fold a run's narrative events into the scalars an ORG caller may see.

    THE single place these aggregates are computed. Every one of them is a max/sum over
    a prefix of the run's own events, so all of them are monotonic — a customer must
    never watch a progress number fall back down mid-run.

    `counters` is the ack-time `sessions` aggregate: it reads all-zero for the whole of
    a fleet-routed run and becomes correct at ack, so it is folded in with `max`, never
    taken blindly. `lead_rows` is the authoritative count of the org's real `matches`
    rows for the run — also empty until ack, also folded with `max`, which is what makes
    `leadsFound` end up exact.

    E.5: `lead_rows` ALSO ships on its own as `leadsDelivered`, because the "authoritative
    rows win at ack" reconciliation never fires for a run that never acks. A dead-lettered
    run's harvest stays in the worker's local sqlite, so its rows stay 0 permanently and
    `max(estimate, rows)` collapses to the estimate — forever. That estimate is therefore
    never discarded, reset, or recomputed to zero when the job flips to `failed`: for that
    run it is the only record the customer will ever have. Shipping both numbers (and the
    `delivery` word `delivery_state` derives from them) is what stops the panel having to
    choose which of the two available lies to tell.
    """
    match_details: list[dict] = []            # (comments, success) detail blobs
    walk: dict[str, tuple[int, int, int]] = {}  # session -> (seq, itemsSeen, relevant)
    last_phase: Optional[str] = None
    last_event_at: Optional[float] = None
    for event in events:
        created = event.get("createdAt")
        if isinstance(created, (int, float)) and (
                last_event_at is None or created > last_event_at):
            last_event_at = float(created)
        phase = event.get("phase") or None
        if phase:
            last_phase = phase        # rows arrive oldest-first, so the last one wins
        detail = _event_detail(event)
        if phase == "comments" and event.get("level") == "success":
            # Deduping/reconciling these is `_leads_from_match_events` — one
            # definition, shared with the superadmin run picker so the list and the
            # log can never quote different numbers for the same run.
            match_details.append(detail)
        elif phase == "feed_walk":
            # PER-SESSION MAX, THEN SUM. The emitting engine reads its OWN Session
            # counters, and one run_id spans MANY sessions (a batch run loops them, a
            # multi-channel campaign fans them out). Taking "the newest feed_walk detail"
            # both under-reports (a session that matched twice may emit no feed_walk at
            # all) and GOES BACKWARDS on a retry — the customer watches the number fall
            # from 8 to 2. Grouping by session mirrors _aggregate_run_counters.
            session_id = str(event.get("sessionId") or "")
            seq = int(event.get("seq") or 0)
            previous = walk.get(session_id)
            if previous is None or seq >= previous[0]:
                walk[session_id] = (seq,
                                    int(detail.get("reelsSeen") or 0),
                                    int(detail.get("relevancePasses") or 0))
            # NOTE: `detail["matches"]` is deliberately NOT read. `counters.matches` is
            # incremented once per POST after a whole comment batch while the success
            # events fire per COMMENT, so it lags — observed reading 0 against 15 success
            # rows in the same run. `leadsFound` is the only lead number that goes on
            # screen; two disagreeing counts must never render together.
    if failed:
        phase_word = "failed"
    elif finished:
        phase_word = "done"
    elif last_phase is None:
        phase_word = "starting"       # zero events + a live run is "starting", not "0 found"
    else:
        phase_word = _ORG_RUN_PHASES.get(last_phase, "working")
    return {
        # E.5: `leadsFound` / `leadsDelivered` / `delivery` come out of the ONE helper
        # that reconciles the pair, shared with the panel builders so a run drawer and a
        # campaign card can never disagree about what "not delivered" means. A failed run
        # is over, so its gap is a verdict rather than ack lag.
        **delivery_state(max(_leads_from_match_events(match_details), int(lead_rows)),
                         lead_rows, finished=finished or failed),
        "itemsScanned": max(sum(w[1] for w in walk.values()),
                            int(counters.get("reelsSeen") or 0)),
        "relevantFound": max(sum(w[2] for w in walk.values()),
                             int(counters.get("relevancePasses") or 0)),
        # A timestamp is not a log — it is the liveness beat that stops the panel's
        # stall banner from lying about a quiet-but-alive run.
        "lastEventAt": last_event_at,
        "phase": phase_word,
    }


# How many of an org's runs the superadmin picker lists. Newest-first, so the cap only
# ever hides history an operator would have to scroll for anyway.
ADMIN_ORG_RUNS_LIMIT = 50


def _build_admin_org_runs(store: Any, org_id: int, modes: dict[str, str],
                          limit: int = ADMIN_ORG_RUNS_LIMIT) -> list[dict[str, Any]]:
    """One org's recent runs, newest first — the superadmin's picker for the narrative
    feed at ADMIN_RUN_ACTIVITY_PATH.

    There is no `runs` table: a run IS the set of `sessions` sharing a `run_id` (a batch
    run loops several, a multi-channel campaign fans several out), so the rows are folded
    here. That is the durable record — but a FLEET-routed run has NO session rows in the
    cloud until its job acks (they are mirrored from the worker at ack), so the org's
    active fleet jobs are merged in as started-but-empty runs. Without that merge the one
    run an operator most wants to inspect — the one running right now — would be the one
    missing from the list.

    `modes` maps run_id -> 'live'/'dry' for the runs THIS process still remembers; the
    sessions table records no mode, so an older run's `mode` is honestly null rather than
    a guess. The N+1 over campaigns mirrors _handle_admin_orgs and is fine at PRD scale.

    LEAD COUNTS. Summing `sessions.matches` — the only source this used to have — reads
    ZERO for exactly the run an operator is most likely to be looking at: a fleet run
    that dead-lettered never acked, so it mirrored no session rows into the cloud at all.
    A run that really harvested fifteen leads was listed as "0 leads" while its own
    narrative log said fifteen. So each row now carries the SAME pair the log does, out
    of the same `_leads_from_match_events` definition:
      * `leadsFound`     — the deduped event estimate: what the run discovered.
      * `leadsDelivered` — real `matches` rows: what actually reached the account.
      * `delivery`       — the word that reconciles them (E.5/E.7), so a not-delivered
        run is legible as one instead of as a number that looks either inflated or lost.
    `leads` is kept as an alias of `leadsFound` so an older picker keeps rendering — and
    now renders the honest number rather than the session sum's zero.
    """
    names: dict[str, str] = {}
    runs: dict[str, dict[str, Any]] = {}
    platforms: dict[str, set] = {}
    for meta in store.list_campaign_meta(org_id):
        cid = meta["campaign_id"]
        names[cid] = meta.get("display_name") or cid
        for s in store.all_sessions(cid):
            run_id = s.get("run_id")
            if not run_id:
                continue  # pre-v10 sessions and CLI runs carry no run correlation
            run = runs.get(run_id)
            if run is None:
                run = runs[run_id] = {
                    "runId": run_id, "campaignId": cid, "campaignName": names[cid],
                    "mode": modes.get(run_id), "status": "done", "platforms": [],
                    "startedAt": None, "finishedAt": None, "sessions": 0, "leads": 0,
                }
                platforms[run_id] = set()
            if s.get("platform"):
                platforms[run_id].add(s["platform"])
            started, ended = s.get("started_at"), s.get("ended_at")
            if started is not None and (run["startedAt"] is None
                                        or started < run["startedAt"]):
                run["startedAt"] = started
            run["sessions"] += 1
            run["leads"] += int(s.get("matches") or 0)
            if s.get("status") == "running" or ended is None:
                # Still open (or crashed without an end_session) — the run is not over,
                # and `finishedAt` must stay null rather than report the last session's
                # end as the run's end.
                run["status"] = "running"
                run["finishedAt"] = None
            elif run["status"] != "running":
                if s.get("status") == "halted":
                    run["status"] = "halted"
                if run["finishedAt"] is None or ended > run["finishedAt"]:
                    run["finishedAt"] = ended
    for cid, run_id in store.active_fleet_runs_for_org(org_id).items():
        if run_id in runs:
            continue  # already acked and mirrored into sessions
        job = store.get_job_for_run(run_id, org_id)
        runs[run_id] = {
            "runId": run_id, "campaignId": cid, "campaignName": names.get(cid, cid),
            # Only LIVE runs are ever dispatched to the fleet (dry runs stay in-process),
            # so this one is not a guess.
            "mode": "live", "status": "running",
            "platforms": [job["platform"]] if job and job.get("platform") else [],
            "startedAt": job.get("createdAt") if job else None,
            "finishedAt": None, "sessions": 0, "leads": 0,
        }
    _merge_event_only_runs(store, org_id, runs, names, limit)
    for run_id, marks in platforms.items():
        runs[run_id]["platforms"] = sorted(marks)
    ordered = sorted(runs.values(), key=lambda r: r["startedAt"] or 0.0,
                     reverse=True)[:limit]
    _attach_admin_run_leads(store, org_id, ordered)
    return ordered


def _merge_event_only_runs(store: Any, org_id: int, runs: dict[str, dict[str, Any]],
                           names: dict[str, str], limit: int) -> None:
    """Add the org's runs that exist ONLY as narrative events, in place.

    THIRD source, after sessions and active fleet jobs, and the one that catches the
    run an operator is most likely hunting for. Sessions reach the cloud in the ACK
    body, so a job that dead-lettered mirrored none — and the moment it leaves the
    active statuses it also drops out of the fleet merge above. The run then vanished
    from the picker while ADMIN_RUN_ACTIVITY_PATH could still render its complete log,
    which is the same list/log disagreement as a wrong lead count, only total.

    Everything about such a run is derived from what did arrive:
      * `startedAt`/`finishedAt` from the first/last event timestamp — the run really
        was alive between them;
      * `status` from its JOB row, which nack_job DID update (spend rides on the nack
        body even though leads do not), so a dead-lettered run reads `failed` rather
        than being dressed up as a normal `done`;
      * `mode` is `live` only when a job proves the run was dispatched to the fleet
        (dry runs never are) and null otherwise — honestly unknown, not guessed.
    Best-effort — the picker's job is to reach a log, so a read failure here leaves the
    session-derived list intact rather than failing the org page.
    """
    try:
        candidates = store.run_event_runs(org_id, limit=limit)
    except Exception:  # noqa: BLE001 — additive rows; never break the picker.
        log.exception("admin run picker event scan failed · org=%s", org_id)
        return
    for row in candidates:
        run_id = row["runId"]
        if run_id in runs:
            continue          # sessions or a live job already described it, in full
        job = None
        try:
            job = store.get_job_for_run(run_id, org_id)
        except Exception:  # noqa: BLE001 — status degrades, the row still lists.
            log.exception("admin run picker job lookup failed · run=%s", run_id)
        status = (job or {}).get("status")
        cid = row["campaignId"] or (job or {}).get("campaignId") or ""
        runs[run_id] = {
            "runId": run_id, "campaignId": cid, "campaignName": names.get(cid, cid),
            # A fleet job is the only way a run gets here with no sessions, and only
            # LIVE runs are dispatched to the fleet — but say nothing when there is no
            # job to prove it rather than guessing.
            "mode": "live" if job else None,
            "status": "failed" if status in ("failed", "interrupted") else (
                "running" if status in ("queued", "leased", "running") else "done"),
            "platforms": [job["platform"]] if job and job.get("platform") else [],
            "startedAt": row["firstAt"],
            # A run with no session row never reported an end; its last event is the
            # last thing anyone knows about it, and calling that the finish is more
            # honest than a null that reads as "still going".
            "finishedAt": None if status in ("queued", "leased", "running")
            else row["lastAt"],
            "sessions": row["sessions"], "leads": 0,
        }


def _attach_admin_run_leads(store: Any, org_id: int,
                            rows: list[dict[str, Any]]) -> None:
    """Fold the log's own lead pair into each picker row, in place.

    Two bulk reads for the whole page (never a per-run N+1) and both are best-effort:
    the picker's job is to let an operator REACH a run's log, so a counting failure
    must leave the list usable rather than 500 the org page. On that path the rows
    keep the session-derived `leads` they already carry.

    `finished` for the pair is "the run is not still running": a live run's gap is ack
    lag (`pending`), while a run that is over and still short really did lose leads.
    """
    if not rows:
        return
    try:
        delivered_by_run = store.lead_counts_by_run(org_id)
        details_by_run = store.match_event_details_by_run(
            org_id, [r["runId"] for r in rows])
    except Exception:  # noqa: BLE001 — additive numbers; never break the picker.
        log.exception("admin run picker lead counts failed · org=%s", org_id)
        return
    for row in rows:
        delivered = delivered_by_run.get(row["runId"], 0)
        estimate = _leads_from_match_events(details_by_run.get(row["runId"], []))
        # The session counter joins the max as a third floor: it is the only source
        # for a pre-v10 run whose events have since been pruned by retention.
        row.update(delivery_state(max(estimate, delivered, int(row["leads"] or 0)),
                                  delivered,
                                  finished=row["status"] != "running"))
        row["leads"] = row["leadsFound"]


def _attach_fleet_run_ids(store: Any, raw: dict, org_id: int) -> None:
    """FIX 2: fold a DB-derived `fleetRunId` into each campaign card on `/api/campaigns`
    (the run_id of that campaign's most-recent ACTIVE fleet job, else None). One map
    fetch, one lookup per card. Rewrites each card immutably (new dict) rather than
    mutating the builder's objects in place. Best-effort — a read error leaves the
    cards untouched (no fleetRunId key) rather than failing the page."""
    campaigns = raw.get("CAMPAIGNS")
    if not isinstance(campaigns, list):
        return
    try:
        active = store.active_fleet_runs_for_org(org_id)
    except Exception:  # noqa: BLE001 — additive field; never break the campaigns page.
        return
    raw["CAMPAIGNS"] = [
        {**c, "fleetRunId": active.get(c.get("id"))} for c in campaigns]


def _fleet_run_finished(fleet_job: dict, sessions: list, session_finished: bool) -> bool:
    """Resolve `finished` for a fleet-routed run's activity feed (FIX 2 + review).

    The panel poller stops on `finished`, so this must never leave the drawer polling
    a dead run forever, and never cut off before the final counters land:
      - failed/interrupted → finished (nothing more will arrive).
      - done → finished ONLY once the observational session mirror has landed;
        ack commits `done` microseconds before the best-effort `sessions` write, so
        while it is missing keep polling one more cycle (counters would read zero).
      - leased/running with a LIVE lease → alive (keep polling the silent worker).
      - leased/running with an EXPIRED lease (worker died mid-run) → fall back to the
        session-derived value so a dead run's drawer can terminate rather than poll on.
      - queued → alive (waiting for a worker; polling is drawer-gated client-side)."""
    status = fleet_job["status"]
    if status in ("failed", "interrupted"):
        return True
    if status == "done":
        return bool(sessions)
    if status in ("leased", "running"):
        lease = fleet_job.get("leaseExpiresAt")
        if lease is not None and lease < time.time():
            return session_finished
        return False
    return False  # queued


def _fleet_job_reason(result: Any) -> Optional[str]:
    """The operator-facing failure code carried by a fleet job's `result` blob, or None.

    Two shapes land in that column: `nack_job` writes ``{"reason", "poison", …}`` on a
    requeue/dead-letter, and `ack_job` overwrites it with the engine run summary (whose
    equivalent key is `halt_reason`) on success — so read both, reason first. The value
    is worker-authored (a fixed code by design: sidecar never sends a raw exception
    string), but re-cap it before echoing it to a browser. Callers must key the
    failed/succeeded wording off the job's STATUS, never off the presence of a reason:
    a `done` job can legitimately carry one (e.g. a daytime halt that still acked)."""
    if not isinstance(result, dict):
        return None
    value = result.get("reason") or result.get("halt_reason")
    if not isinstance(value, str) or not value.strip():
        return None
    return value.strip()[:_WORKER_MAX_STR]


class InviteThrottle:
    """Thread-safe, in-memory invite-creation limiter, keyed by actor user id.

    Mirrors auth.LoginThrottle's rolling-window design, but counts SUCCESSFUL
    creates (not failures): after `max_creates` creates inside `window` seconds the
    actor is throttled until older creates age out of the window. Memory is bounded
    by pruning expired timestamps on every touch.
    """

    def __init__(self, *, max_creates: int = MAX_INVITE_CREATES,
                 window: float = INVITE_RATE_WINDOW):
        self._max = max_creates
        self._window = window
        self._lock = threading.Lock()
        self._creates: dict[str, list[float]] = {}  # key -> create timestamps

    def _now(self) -> float:
        return time.time()

    def is_throttled(self, key: str) -> bool:
        """True iff `key` already has `max_creates` creates inside the window."""
        now = self._now()
        with self._lock:
            recent = [t for t in self._creates.get(key, []) if now - t < self._window]
            self._creates[key] = recent
            return len(recent) >= self._max

    def record_create(self, key: str) -> None:
        now = self._now()
        with self._lock:
            recent = [t for t in self._creates.get(key, []) if now - t < self._window]
            recent.append(now)
            self._creates[key] = recent


def _validate_status_request(payload: Any) -> tuple[Optional[dict], Optional[str]]:
    """Returns (fields, None) on success or (None, error message)."""
    if not isinstance(payload, dict):
        return None, "body must be a JSON object"
    fields = {}
    for key in ("campaignId", "commentId", "status"):
        value = payload.get(key)
        if not isinstance(value, str) or not value.strip():
            return None, f"missing or empty field: {key}"
        fields[key] = value.strip()
    if fields["status"] not in VALID_STATUS:
        return None, f"invalid status (expected one of {sorted(VALID_STATUS)})"
    # platform is optional — a multi-platform panel sends the match's platform so
    # the status mark hits the right row; older panels omit it (defaults below).
    platform = payload.get("platform")
    if platform is not None and (not isinstance(platform, str) or not platform.strip()):
        return None, "platform, if present, must be a non-empty string"
    fields["platform"] = platform.strip() if isinstance(platform, str) else DEFAULT_PLATFORM
    # Optional reason note. Required (non-empty) when moving into a forced-reason
    # status — the store enforces this too, but failing fast here is friendlier.
    note, err = _opt_note(payload.get("note"))
    if err is not None:
        return None, err
    fields["note"] = note
    if fields["status"] in FORCED_REASON_STATUS and not note:
        return None, f"a reason note is required to move a lead to {fields['status']!r}"
    return fields, None


def _opt_note(value: Any) -> tuple[Optional[str], Optional[str]]:
    """Validate an optional note/reason body; (None, None) when absent/blank."""
    if value is None:
        return None, None
    if not isinstance(value, str):
        return None, "note, if present, must be a string"
    note = value.strip()
    if len(note) > MAX_NOTE_LENGTH:
        return None, f"note exceeds {MAX_NOTE_LENGTH} characters"
    return (note or None), None


# Every number arriving over the wire must be REPRESENTABLE, not merely well-typed.
# Two holes this closes, both of which used to be reachable from a plain POST:
#
#   * `json.loads` accepts JSON's non-standard `NaN` / `Infinity` / `-Infinity`
#     literals (and `1e400` overflows to `inf`) by default. A non-finite float
#     sails through an `x < 0` range check — every comparison against NaN is
#     False — is stored, and is then re-emitted by `json.dumps` as a BARE
#     `Infinity`/`NaN` token. That is invalid JSON per RFC 8259, so /api/state and
#     /api/campaigns come back 200 with a body the panel's parser rejects: the
#     whole org's panel is dead with no in-app way to undo it.
#   * A 400-digit integer literal blows up `float()` with OverflowError, and a
#     merely huge one blows up SQLite's INTEGER binding — exceptions that used to
#     escape the handler entirely (see `_dispatch_guarded`).
#
# _MAX_WIRE_NUMBER is comfortably above any legitimate budget/goal/epoch a panel
# field carries, and below both SQLite's signed-64-bit INTEGER ceiling (2**63-1)
# and float's exact-integer limit, so a value that passes here can always be
# stored and re-serialized.
_MAX_WIRE_NUMBER = 1e15


def _finite_number(value: Any, name: str) -> tuple[Optional[float], Optional[str]]:
    """Coerce a wire number to a finite, storable float; (None, error) otherwise."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None, f"{name} must be a number"
    try:
        number = float(value)
    except (OverflowError, ValueError):     # e.g. a 400-digit integer literal
        return None, f"{name} is out of range"
    if not math.isfinite(number):
        return None, f"{name} must be a finite number"
    if abs(number) > _MAX_WIRE_NUMBER:
        return None, f"{name} is out of range"
    return number, None


def _opt_number(value: Any, name: str) -> tuple[Optional[float], Optional[str]]:
    """Validate an optional finite, non-negative number; (None,None) when absent."""
    if value is None:
        return None, None
    number, err = _finite_number(value, name)
    if err is not None:
        return None, err
    if number < 0:
        return None, f"{name} must be >= 0"
    return number, None


def _validate_bulk_status(payload: Any) -> tuple[Optional[dict], Optional[str]]:
    if not isinstance(payload, dict):
        return None, "body must be a JSON object"
    campaign_id = payload.get("campaignId")
    status = payload.get("status")
    items = payload.get("items")
    if not isinstance(campaign_id, str) or not campaign_id.strip():
        return None, "missing or empty field: campaignId"
    if status not in VALID_STATUS:
        return None, f"invalid status (expected one of {sorted(VALID_STATUS)})"
    if not isinstance(items, list) or not items:
        return None, "items must be a non-empty list"
    if len(items) > _BULK_MAX:
        return None, f"items exceeds max of {_BULK_MAX}"
    # One shared reason for the whole bulk action (an operator makes one decision
    # about many leads). Fail fast if a forced-reason target has no note.
    note, err = _opt_note(payload.get("note"))
    if err is not None:
        return None, err
    if status in FORCED_REASON_STATUS and not note:
        return None, f"a reason note is required to move leads to {status!r}"
    parsed = []
    for it in items:
        if not isinstance(it, dict):
            return None, "each item must be an object"
        comment_id = it.get("commentId")
        if not isinstance(comment_id, str) or not comment_id.strip():
            return None, "each item needs a non-empty commentId"
        platform = it.get("platform")
        platform = platform.strip() if isinstance(platform, str) and platform.strip() \
            else DEFAULT_PLATFORM
        parsed.append({"commentId": comment_id.strip(), "platform": platform})
    return {"campaignId": campaign_id.strip(), "status": status, "items": parsed,
            "note": note}, None


def _validate_lead_note(payload: Any) -> tuple[Optional[dict], Optional[str]]:
    """Op-based lead note request: create needs campaignId/commentId/body (+optional
    platform); delete needs a numeric noteId. Author is resolved server-side."""
    if not isinstance(payload, dict):
        return None, "body must be a JSON object"
    op = payload.get("op")
    if op not in ("create", "delete"):
        return None, "op must be one of create/delete"
    if op == "create":
        out: dict[str, Any] = {"op": op}
        for key in ("campaignId", "commentId", "body"):
            value = payload.get(key)
            if not isinstance(value, str) or not value.strip():
                return None, f"missing or empty field: {key}"
            out[key] = value.strip()
        if len(out["body"]) > MAX_NOTE_LENGTH:
            return None, f"body exceeds {MAX_NOTE_LENGTH} characters"
        platform = payload.get("platform")
        out["platform"] = platform.strip() if isinstance(platform, str) and platform.strip() \
            else DEFAULT_PLATFORM
        return out, None
    note_id = payload.get("noteId")
    if isinstance(note_id, bool) or not isinstance(note_id, int):
        return None, "missing or invalid field: noteId"
    return {"op": op, "noteId": note_id}, None


def _validate_lead_reveal(payload: Any) -> tuple[Optional[dict], Optional[str]]:
    """One lead's composite key: campaignId + commentId (+ optional platform).

    Exactly ONE lead, by the `matches` primary key — there is no list form, no
    `commentIds`, no `status`/`limit`/`all` filter. That is the point of the
    endpoint: a bulk reveal would silently rebuild the export leak that the v27
    redaction exists to close, so the shape refuses to widen rather than relying
    on a handler to cap it. Mirrors `_validate_status_request`'s field checks.
    """
    if not isinstance(payload, dict):
        return None, "body must be a JSON object"
    fields: dict[str, Any] = {}
    for key in ("campaignId", "commentId"):
        value = payload.get(key)
        if not isinstance(value, str) or not value.strip():
            return None, f"missing or empty field: {key}"
        fields[key] = value.strip()
    platform = payload.get("platform")
    if platform is not None and (not isinstance(platform, str) or not platform.strip()):
        return None, "platform, if present, must be a non-empty string"
    fields["platform"] = platform.strip() if isinstance(platform, str) else DEFAULT_PLATFORM
    return fields, None


class PortInUseError(RuntimeError):
    """`serve()` could not bind its listen port. Carries an operator-facing message
    (no errno, no traceback) so the CLI can print it in the same `error: …` style it
    already uses for a missing panel build."""


def _is_soft_landing_path(path: str) -> bool:
    """Whether an unknown, non-API, non-/app path should soft-land on the marketing
    page instead of 404ing. True only for a single bare segment with no file
    extension ("/pricing"), where the landing's RELATIVE asset URLs still resolve
    against "/". Nested ("/pricing/enterprise") or extensioned ("/robots.txt") paths
    are real 404s — see the call site."""
    segments = [s for s in path.split("/") if s]
    return len(segments) == 1 and "." not in segments[0]


# POST /api/campaign is one endpoint for two operations. `op` names which.
CAMPAIGN_OPS = frozenset({"create", "edit"})
CAMPAIGN_EXISTS_MESSAGE = ("a campaign with this id already exists — rename it or "
                           "edit the existing campaign")
# Separator for an org-scoped campaign key. '.' is deliberate: the panel slugifies a
# campaign name down to [a-z0-9-] (useCampaignForm.slugify), so a scoped key can
# never be mistaken for — or collide with — a client-chosen slug.
_ORG_SCOPE_SEP = "."
# The reserved shape `_org_scoped_campaign_id` mints. A client may only ever name an
# id in this namespace when it already OWNS that row (its own campaign, sent back on
# an edit); a write that would REGISTER a fresh one is refused. Without that guard the
# namespace is squattable: since the key still lives in one global campaign_meta PK,
# a tenant could pre-register `o<victimOrg>.<slug>` and lock the victim out of that
# name for good (they would 409 on create and 404 on edit, with no remedy).
_ORG_SCOPE_RE = re.compile(r"^o\d+\.")


def _is_org_scoped_campaign_id(campaign_id: str) -> bool:
    """Whether `campaign_id` sits in the reserved per-org key namespace."""
    return bool(_ORG_SCOPE_RE.match(campaign_id))


def _org_scoped_campaign_id(org_id: Optional[int], requested: str) -> str:
    """The storage key for a campaign CREATED by `org_id`.

    campaign_meta.campaign_id is a single global primary key, so ids were a shared
    namespace across tenants: org B creating an ordinarily-named 'Q4 Outbound' that
    org A already had was answered `404 unknown campaign` — nonsensical on a create,
    with no remedy — and the same guard doubled as a cross-tenant existence oracle
    (probe an id, learn whether somebody else owns it).

    Namespacing the key by org makes identity per-org: two tenants can both have
    'Q4 Outbound', and — crucially for the oracle — the allocated key depends ONLY on
    the caller's own org, never on whether another tenant holds the bare id, so the
    outcome carries no information about anyone else's data. Legacy bare ids (rows
    created before this, file-backed campaigns, the CLI) keep resolving unchanged;
    only a create allocates a scoped key.
    """
    return f"o{org_id}{_ORG_SCOPE_SEP}{requested}"


def _validate_campaign(payload: Any) -> tuple[Optional[dict], Optional[str]]:
    if not isinstance(payload, dict):
        return None, "body must be a JSON object"
    cid = payload.get("campaignId")
    if not isinstance(cid, str) or not cid.strip():
        return None, "missing or empty field: campaignId"
    # CREATE and EDIT used to be the SAME request: same path, same payload, no
    # discriminator — so a second campaign whose name slugs onto an existing id
    # silently overwrote that campaign's brief, and (because `matches` is keyed on
    # campaign_id) re-pointed its whole lead history at what the operator believed
    # was a new campaign. `op` makes the intent explicit; absent, the legacy
    # infer-from-existence behaviour is kept so older clients keep working.
    op = payload.get("op")
    if op is not None and op not in CAMPAIGN_OPS:
        return None, f"invalid op (expected one of {sorted(CAMPAIGN_OPS)})"
    status = payload.get("status")
    if status is not None and status not in VALID_CAMPAIGN_STATUS:
        return None, f"invalid status (expected one of {sorted(VALID_CAMPAIGN_STATUS)})"
    budget, err = _opt_number(payload.get("budgetCap"), "budgetCap")
    if err:
        return None, err
    goal, err = _opt_number(payload.get("goalTarget"), "goalTarget")
    if err:
        return None, err
    name = payload.get("displayName")
    if name is not None and not isinstance(name, str):
        return None, "displayName must be a string"
    brief = payload.get("brief")
    if brief is not None and not isinstance(brief, dict):
        return None, "brief must be an object"
    if isinstance(brief, dict):
        # The one numeric field the brief form carries. `_brief_to_snake` drops an
        # unusable threshold silently (a blank means "keep the stored one"), but a
        # NUMERIC one that is non-finite deserves a loud 400: it is exactly the
        # value that would otherwise be persisted and then re-emitted as a bare
        # NaN/Infinity token. A numeric *string* keeps its historic tolerance.
        threshold = brief.get("threshold")
        if isinstance(threshold, (int, float)) and not isinstance(threshold, bool):
            value, err = _finite_number(threshold, "brief.threshold")
            if err:
                return None, err
            # RANGE, not just finiteness (Campaign Lab, Remedy Sheet #3 / Remedy E).
            # The gate is `score >= campaign.threshold`, so 0 accepts every comment
            # ever scored and 1 accepts none — both are silent, both look like a
            # working campaign, and neither raises anything anywhere. Only the
            # explicit numeric form is checked; a numeric *string* keeps its
            # historic tolerance, and a blank still means "keep the stored one".
            if value is not None and not (0.0 < float(value) < 1.0):
                return None, ("brief.threshold must be between 0 and 1 "
                              "(exclusive) — 0 matches every comment, 1 matches none")
    return {"campaignId": cid.strip(), "op": op, "status": status, "budgetCap": budget,
            "goalTarget": int(goal) if goal is not None else None,
            "displayName": name.strip() if isinstance(name, str) else None,
            "brief": _brief_to_snake(brief) if isinstance(brief, dict) else None}, None


def _validate_campaign_archive(payload: Any) -> tuple[Optional[dict], Optional[str]]:
    """v12: {campaignId, archived: bool}. Archive (true) or un-archive (false)."""
    if not isinstance(payload, dict):
        return None, "body must be a JSON object"
    cid = payload.get("campaignId")
    if not isinstance(cid, str) or not cid.strip():
        return None, "missing or empty field: campaignId"
    archived = payload.get("archived")
    if not isinstance(archived, bool):
        return None, "field 'archived' must be a boolean"
    return {"campaignId": cid.strip(), "archived": archived}, None


# Schedule timezones the panel offers (Asia/Tashkent is the engine's fixed-offset
# convention; widen this allowlist when the UI offers more).
_SCHEDULE_TIMEZONES = {"Asia/Tashkent"}


def _opt_int_in_range(value: Any, name: str, lo: int, hi: int
                      ) -> tuple[Optional[int], Optional[str]]:
    """Validate an optional integer within [lo, hi]; (None, None) when absent."""
    if value is None:
        return None, None
    if isinstance(value, bool) or not isinstance(value, int):
        return None, f"{name} must be an integer"
    if not (lo <= value <= hi):
        return None, f"{name} must be between {lo} and {hi}"
    return value, None


def _validate_campaign_schedule(payload: Any) -> tuple[Optional[dict], Optional[str]]:
    """v12: {campaignId, enabled, kind?, dow?, hour?, minute?, tz?, targetLeads?,
    durationMinutes?}. When enabled, the cadence fields are required and range-checked;
    when disabled, only campaignId matters (the schedule is cleared)."""
    if not isinstance(payload, dict):
        return None, "body must be a JSON object"
    cid = payload.get("campaignId")
    if not isinstance(cid, str) or not cid.strip():
        return None, "missing or empty field: campaignId"
    enabled = payload.get("enabled")
    if not isinstance(enabled, bool):
        return None, "field 'enabled' must be a boolean"
    fields: dict[str, Any] = {"campaignId": cid.strip(), "enabled": enabled}
    if not enabled:
        return fields, None  # clearing — cadence fields ignored

    kind = payload.get("kind")
    if kind not in SCHEDULE_KINDS:
        return None, f"invalid schedule kind (expected one of {sorted(SCHEDULE_KINDS)})"
    hour, err = _opt_int_in_range(payload.get("hour"), "hour", 0, 23)
    if err or hour is None:
        return None, err or "hour is required when enabled"
    minute, err = _opt_int_in_range(payload.get("minute"), "minute", 0, 59)
    if err or minute is None:
        return None, err or "minute is required when enabled"
    dow = None
    if kind == "weekly":
        dow, err = _opt_int_in_range(payload.get("dow"), "dow", 0, 6)
        if err or dow is None:
            return None, err or "dow (0=Mon..6=Sun) is required for a weekly schedule"
    tz = payload.get("tz", "Asia/Tashkent")
    if tz not in _SCHEDULE_TIMEZONES:
        return None, f"unsupported timezone (expected one of {sorted(_SCHEDULE_TIMEZONES)})"
    target, err = _opt_int_in_range(payload.get("targetLeads"), "targetLeads",
                                    1, MAX_RUN_LEAD_TARGET)
    if err:
        return None, err
    duration, err = _opt_int_in_range(payload.get("durationMinutes"), "durationMinutes",
                                      1, MAX_RUN_DURATION_MINUTES)
    if err:
        return None, err
    fields.update({"kind": kind, "hour": hour, "minute": minute, "dow": dow,
                   "tz": tz, "targetLeads": target, "durationMinutes": duration})
    return fields, None


# The editable brief travels camelCase over the wire (UI-side) and is persisted
# snake_case (engine-side, matching `campaign_from_brief`). Unknown keys are dropped.
_BRIEF_KEYS = {
    "platform": "platform", "goal": "goal", "threshold": "threshold",
    "languageMix": "language_mix", "relevanceDef": "relevance_def",
    "matchDef": "match_def", "extractDef": "extract_def",
    # Tuned classifier system prompts (optional, advanced). Persisted so a UI
    # campaign isn't stuck on the router's generic fallback prompt.
    "relevancePrompt": "relevance_prompt", "matchPrompt": "match_prompt",
    "visionPrompt": "vision_prompt",
    "seedHashtags": "seed_hashtags", "seedAccounts": "seed_accounts",
    "seedChannels": "seed_channels", "includeHomeFeed": "include_home_feed",
    # Multi-platform fan-out: a list of per-platform channel dicts. Handled by a
    # dedicated branch in _brief_to_snake (not a scalar/list-of-strings). Absent ⇒
    # no-change (the merge sentinel), [] ⇒ clear to single-platform (C3).
    "channels": "channels",
}
_BRIEF_LIST_KEYS = {"language_mix", "seed_hashtags", "seed_accounts", "seed_channels"}
_BRIEF_BOOL_KEYS = {"include_home_feed"}
# A blank value for these means "leave unchanged", not "clear": _handle_campaign
# MERGES the incoming brief over the stored one, so persisting an empty prompt
# would wipe a campaign's tuned system prompt. Dropping blanks lets the form omit
# them safely (blank ⇒ keep the existing prompt, or fall back to generic).
_BRIEF_BLANK_DROP_KEYS = {"relevance_prompt", "match_prompt", "vision_prompt"}


def _to_bool(value: Any) -> bool:
    return value if isinstance(value, bool) else str(value).strip().lower() in (
        "true", "1", "yes", "on")


_CHANNEL_SEED_KEYS = (("seedHashtags", "seed_hashtags"),
                      ("seedAccounts", "seed_accounts"),
                      ("seedChannels", "seed_channels"))


def _channel_to_snake(entry: Any) -> Optional[dict[str, Any]]:
    """One UI `channels[]` entry (camelCase) → a snake_case channel dict, or None to
    drop it. Returns None for a non-dict entry or an unsupported/blank platform, so
    a malformed channel is silently dropped (never a 400). Absent optional seed +
    `includeHomeFeed` keys are OMITTED so config resolves the seed-aware default."""
    if not isinstance(entry, dict):
        return None
    platform = str(entry.get("platform", "")).strip().lower()
    if platform not in SUPPORTED_PLATFORMS:
        return None
    out: dict[str, Any] = {"platform": platform}
    for camel, snake in _CHANNEL_SEED_KEYS:
        value = entry.get(camel)
        if isinstance(value, list):
            out[snake] = [str(x).strip() for x in value if str(x).strip()]
    if entry.get("includeHomeFeed") is not None:
        out["include_home_feed"] = _to_bool(entry["includeHomeFeed"])
    return out


def _campaign_merge_base(store: Store, config_dir: str,
                         campaign_id: str) -> dict[str, Any]:
    """The brief an incoming edit is overlaid on (so non-form fields aren't lost).

    Precedence: the campaign's stored DB brief if it has one; else the FILE-backed
    campaign's FULL brief (campaign.md) so editing the primary campaign keeps its
    YAML-only knobs — escalate_band, enable_actions, caps, seed_direction — that
    the panel form can't carry and that would otherwise default-away when the new
    DB brief shadows campaign.md; else {} for a brand-new UI campaign."""
    stored = store.get_campaign_brief(campaign_id)
    if stored is not None:
        return stored
    file_campaign = resolve_campaign(store, config_dir, campaign_id)
    return campaign_to_brief(file_campaign) if file_campaign is not None else {}


def _brief_to_snake(brief: dict) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for camel, snake in _BRIEF_KEYS.items():
        if brief.get(camel) is None:
            continue
        value = brief[camel]
        if snake == "channels":
            # C3 merge sentinel: absent ⇒ not emitted above (no-change); a list
            # (incl. []) ⇒ emitted, overwriting the stored channels. Invalid entries
            # are dropped, so an all-invalid list collapses to [] (clear). A non-list
            # value is ignored (treated as no-change).
            if isinstance(value, list):
                out["channels"] = [c for c in (_channel_to_snake(e) for e in value)
                                   if c is not None]
            continue
        if snake in _BRIEF_LIST_KEYS:
            if isinstance(value, list):
                out[snake] = [str(x).strip() for x in value if str(x).strip()]
        elif snake in _BRIEF_BOOL_KEYS:
            out[snake] = _to_bool(value)
        elif snake == "threshold":
            try:
                number = float(value)
            except (TypeError, ValueError, OverflowError):
                continue
            # Defence in depth behind _validate_campaign's 400: a NaN/Infinity
            # persisted here re-serializes as a bare NaN/Infinity token, which is
            # invalid JSON and kills the panel's parser for the whole org.
            if not math.isfinite(number):
                continue
            out[snake] = number
        else:
            sval = str(value)
            if snake in _BRIEF_BLANK_DROP_KEYS and not sval.strip():
                continue        # blank prompt ⇒ keep stored/generic, never clobber
            out[snake] = sval
    return out


def _opt_capped_str(payload: dict, key: str, cap: int
                    ) -> tuple[Optional[str], Optional[str]]:
    """An optional string field with a length cap. Returns (value|None, error|None)."""
    value = payload.get(key)
    if value is None:
        return None, None
    if not isinstance(value, str):
        return None, f"{key} must be a string"
    if len(value) > cap:
        return None, f"{key} is too long (max {cap})"
    return value, None


def _validate_interview_history(payload: dict
                                ) -> tuple[Optional[list], Optional[str]]:
    """The running Q&A transcript: a capped list of {question, answer} string pairs.
    Missing/None ⇒ []. Malformed entries are an error (fail fast at the boundary)."""
    raw = payload.get("interview")
    if raw is None:
        return [], None
    if not isinstance(raw, list):
        return None, "interview must be a list"
    if len(raw) > _GEN_MAX_QA:
        return None, f"interview is too long (max {_GEN_MAX_QA} entries)"
    out: list[dict] = []
    for item in raw:
        if not isinstance(item, dict):
            return None, "each interview entry must be an object"
        q = item.get("question")
        a = item.get("answer")
        if not isinstance(q, str) or not isinstance(a, str):
            return None, "interview entries need string question and answer"
        if len(q) > _GEN_MAX_QA_LEN or len(a) > _GEN_MAX_QA_LEN:
            return None, f"interview entry is too long (max {_GEN_MAX_QA_LEN} chars)"
        out.append({"question": q.strip(), "answer": a.strip()})
    return out, None


def _validate_platforms(payload: dict) -> tuple[Optional[list], Optional[str]]:
    """The platforms the client chose in the interview. Filtered to the supported
    set in campaign_gen; here we only bound the shape/size."""
    raw = payload.get("platforms")
    if raw is None:
        return None, None
    if not isinstance(raw, list):
        return None, "platforms must be a list"
    if len(raw) > _GEN_MAX_PLATFORMS:
        return None, f"too many platforms (max {_GEN_MAX_PLATFORMS})"
    out: list[str] = []
    for item in raw:
        if not isinstance(item, str) or len(item) > _GEN_MAX_PLATFORM_LEN:
            return None, "each platform must be a short string"
        if item.strip():
            out.append(item.strip())
    return out, None


def _validate_generate(payload: Any) -> tuple[Optional[dict], Optional[str]]:
    """AI campaign generation input: any combination of a product url / screenshot /
    description (at least one required, OR a pre-built productContext from a prior
    interview round). Optionally refined by an interview transcript + chosen
    platforms. Each field is length-capped at the boundary."""
    if not isinstance(payload, dict):
        return None, "body must be a JSON object"
    url, err = _opt_capped_str(payload, "url", _GEN_MAX_URL)
    if err:
        return None, err
    image, err = _opt_capped_str(payload, "imageB64", _GEN_MAX_IMAGE_B64)
    if err:
        return None, err
    text, err = _opt_capped_str(payload, "text", _GEN_MAX_TEXT)
    if err:
        return None, err
    hint, err = _opt_capped_str(payload, "campaignIdHint", _GEN_MAX_ID_HINT)
    if err:
        return None, err
    context, err = _opt_capped_str(payload, "productContext", _GEN_MAX_CONTEXT)
    if err:
        return None, err
    interview, err = _validate_interview_history(payload)
    if err:
        return None, err
    platforms, err = _validate_platforms(payload)
    if err:
        return None, err
    url = url.strip() if url else None
    text = text.strip() if text else None
    image = _DATA_URL_PREFIX.sub("", image).strip() if image else None
    context = context.strip() if context else None
    if url and not (url.startswith("http://") or url.startswith("https://")):
        return None, "url must start with http:// or https://"
    if not (url or image or text or context):
        return None, "provide at least one of: url, imageB64, text, productContext"
    return {"url": url or None, "imageB64": image or None, "text": text or None,
            "campaignIdHint": hint.strip() if hint else None,
            "productContext": context or None, "interview": interview,
            "platforms": platforms}, None


def _validate_interview(payload: Any) -> tuple[Optional[dict], Optional[str]]:
    """Conversational interview input: a product url / screenshot / description on
    the first round, OR a serialized productContext echoed back on later rounds,
    plus the running transcript and the round number. Capped at the boundary."""
    if not isinstance(payload, dict):
        return None, "body must be a JSON object"
    url, err = _opt_capped_str(payload, "url", _GEN_MAX_URL)
    if err:
        return None, err
    image, err = _opt_capped_str(payload, "imageB64", _GEN_MAX_IMAGE_B64)
    if err:
        return None, err
    text, err = _opt_capped_str(payload, "text", _GEN_MAX_TEXT)
    if err:
        return None, err
    context, err = _opt_capped_str(payload, "productContext", _GEN_MAX_CONTEXT)
    if err:
        return None, err
    interview, err = _validate_interview_history(payload)
    if err:
        return None, err
    round_raw = payload.get("round", 1)
    if not isinstance(round_raw, int) or isinstance(round_raw, bool) or round_raw < 1:
        return None, "round must be a positive integer"
    round_num = min(round_raw, campaign_gen.MAX_INTERVIEW_ROUNDS + 1)
    url = url.strip() if url else None
    text = text.strip() if text else None
    image = _DATA_URL_PREFIX.sub("", image).strip() if image else None
    context = context.strip() if context else None
    if url and not (url.startswith("http://") or url.startswith("https://")):
        return None, "url must start with http:// or https://"
    if not (url or image or text or context):
        return None, "provide at least one of: url, imageB64, text, productContext"
    return {"url": url or None, "imageB64": image or None, "text": text or None,
            "productContext": context or None, "interview": interview,
            "round": round_num}, None


def _validate_team(payload: Any) -> tuple[Optional[dict], Optional[str]]:
    """Team management over REAL user accounts (v7). Ops:
      - create:     direct-add a teammate (email + password + role)
      - updateRole: change a teammate's role (admin/member/viewer)
      - remove:     delete a teammate
    Authorization (who may target whom, last-owner guard) is enforced in the handler.
    """
    if not isinstance(payload, dict):
        return None, "body must be a JSON object"
    op = payload.get("op")
    if op not in ("create", "updateRole", "remove"):
        return None, "op must be one of create/updateRole/remove"
    if op == "create":
        email = payload.get("email")
        if not isinstance(email, str) or not _EMAIL_RE.match(email.strip().lower()):
            return None, "invalid email"
        password = payload.get("password")
        if not isinstance(password, str) or len(password) < MIN_PASSWORD_LENGTH:
            return None, f"password must be at least {MIN_PASSWORD_LENGTH} characters"
        if len(password) > MAX_PASSWORD_LENGTH:
            return None, f"password must be at most {MAX_PASSWORD_LENGTH} characters"
        role = payload.get("role")
        if role not in rbac.ASSIGNABLE_ROLES:
            return None, f"role must be one of {list(rbac.ASSIGNABLE_ROLES)}"
        return {"op": op, "email": email.strip().lower(), "password": password,
                "role": role}, None
    user_id = payload.get("userId")
    if isinstance(user_id, bool) or not isinstance(user_id, int):
        return None, "missing or invalid field: userId"
    if op == "remove":
        return {"op": op, "userId": user_id}, None
    role = payload.get("role")     # updateRole — owner is set only via signup/transfer
    if role not in rbac.ASSIGNABLE_ROLES:
        return None, f"role must be one of {list(rbac.ASSIGNABLE_ROLES)}"
    return {"op": op, "userId": user_id, "role": role}, None


def _validate_invite(payload: Any) -> tuple[Optional[dict], Optional[str]]:
    """Invite-link ops: create {role, email?} → returns a shareable link; revoke {id}."""
    if not isinstance(payload, dict):
        return None, "body must be a JSON object"
    op = payload.get("op", "create")
    if op not in ("create", "revoke"):
        return None, "op must be one of create/revoke"
    if op == "create":
        role = payload.get("role")
        if role not in rbac.ASSIGNABLE_ROLES:
            return None, f"role must be one of {list(rbac.ASSIGNABLE_ROLES)}"
        email = payload.get("email")
        if email is not None:
            if not isinstance(email, str) or not _EMAIL_RE.match(email.strip().lower()):
                return None, "invalid email"
            email = email.strip().lower()
        return {"op": op, "role": role, "email": email}, None
    invite_id = payload.get("id")
    if not isinstance(invite_id, str) or not invite_id.strip():
        return None, "missing or invalid field: id"
    return {"op": op, "id": invite_id.strip()}, None


def _validate_org(payload: Any) -> tuple[Optional[dict], Optional[str]]:
    """Company-profile edit: optional name (non-blank) / logo / description."""
    if not isinstance(payload, dict):
        return None, "body must be a JSON object"
    name = payload.get("name")
    if name is not None:
        if not isinstance(name, str) or not name.strip():
            return None, "name must be a non-empty string"
        if len(name.strip()) > _MAX_COMPANY_NAME:
            return None, f"name exceeds {_MAX_COMPANY_NAME} characters"
        name = name.strip()
    logo, err = _opt_company_str(payload.get("logo"), "logo", _MAX_COMPANY_LOGO)
    if err:
        return None, err
    desc, err = _opt_company_str(payload.get("description"), "description", _MAX_COMPANY_DESC)
    if err:
        return None, err
    if name is None and logo is None and desc is None:
        return None, "no fields to update"
    return {"name": name, "logo": logo, "description": desc}, None


def _validate_settings(payload: Any) -> tuple[Optional[dict], Optional[str]]:
    if not isinstance(payload, dict):
        return None, "body must be a JSON object"
    settings = payload.get("settings")
    if not isinstance(settings, dict) or not settings:
        return None, "settings must be a non-empty object"
    for key, value in settings.items():
        if key not in _SETTINGS_KEYS:
            return None, f"unknown setting: {key}"
        if key in _SETTINGS_STR and not isinstance(value, str):
            return None, f"{key} must be a string"
        if key in _SETTINGS_NUM:
            # _finite_number, not a bare isinstance: Python's json parser accepts the
            # non-standard NaN/Infinity literals, and a type-only check let one through
            # to `json.dumps` (allow_nan defaults True), which wrote the bare token into
            # settings.value — invalid JSON for every other reader of that column, and
            # a setting the operator then sees blank forever (the response scrubber
            # rewrites it to null on every single read).
            _, err = _finite_number(value, key)
            if err:
                return None, err
        if key == "pacing" and not isinstance(value, dict):
            return None, "pacing must be an object"
    return {"settings": settings}, None


def _validate_integration(payload: Any) -> tuple[Optional[dict], Optional[str]]:
    if not isinstance(payload, dict):
        return None, "body must be a JSON object"
    platform = payload.get("platform")
    if platform not in SUPPORTED_PLATFORMS:
        return None, f"platform must be one of {list(SUPPORTED_PLATFORMS)}"
    connected = payload.get("connected")
    if connected is not None and not isinstance(connected, bool):
        return None, "connected must be a boolean"
    detail = payload.get("detail")
    if detail is not None and not isinstance(detail, str):
        return None, "detail must be a string"
    # apiKey (optional) is the YouTube connect credential — captured here, validated
    # live and stored encrypted in the handler. Only youtube self-serves a key.
    api_key = payload.get("apiKey")
    if api_key is not None:
        if not isinstance(api_key, str) or not api_key.strip():
            return None, "apiKey, if present, must be a non-empty string"
        if platform != "youtube":
            return None, "apiKey is only supported for the youtube connection"
    # Reddit self-serves an app-only OAuth credential: client_id + client_secret +
    # user_agent (a single-step connect, no redirect dance). Validated live and
    # stored encrypted in the handler. All-or-nothing: either the trio is present
    # (a connect) or none of it is (a plain toggle / disconnect).
    reddit_fields = ("clientId", "clientSecret", "userAgent")
    reddit_present = [f for f in reddit_fields if payload.get(f) is not None]
    reddit_secret: Optional[dict] = None
    if reddit_present:
        if platform != "reddit":
            return None, "clientId/clientSecret/userAgent are only supported for the reddit connection"
        for f in reddit_fields:
            v = payload.get(f)
            if not isinstance(v, str) or not v.strip():
                return None, f"{f} must be a non-empty string"
        reddit_secret = {
            "client_id": str(payload["clientId"]).strip(),
            "client_secret": str(payload["clientSecret"]).strip(),
            "user_agent": str(payload["userAgent"]).strip(),
        }
    return {"platform": platform, "connected": connected,
            "detail": detail.strip() if isinstance(detail, str) else None,
            "apiKey": api_key.strip() if isinstance(api_key, str) else None,
            "redditSecret": reddit_secret}, None


def _opt_str_field(payload: dict, key: str) -> tuple[Optional[str], Optional[str]]:
    """An optional, length-capped string worker-register field. Returns (value, err);
    value is None when absent. Empty-after-strip collapses to None (treat blank as
    omitted, like the connection fields above)."""
    v = payload.get(key)
    if v is None:
        return None, None
    if not isinstance(v, str):
        return None, f"{key} must be a string"
    v = v.strip()
    if len(v) > _WORKER_MAX_STR:
        return None, f"{key} must be at most {_WORKER_MAX_STR} characters"
    return (v or None), None


def _validate_preflight_summary(value: Any) -> Optional[dict]:
    """Coerce the v23 worker launch-preflight summary (spec §5.3) — TOLERANT, TOTAL, and
    NEVER an error return.

    Anything unrecognised is DROPPED, never a 400. This is the B9 rule applied to a
    diagnostic: a self-check hint must never become the reason a workable box cannot
    register or heartbeat. Nothing on the auth or lease path may branch on this field —
    it exists so an admin can read the real cause in the fleet console instead of
    visiting a PC nobody can SSH into (F12).

    Kept keys: ok/blocking/enforced (coerced bool), ranAt (finite float, else dropped),
    and failed[] rows of {id, severity, status, detail}. `title`/`remedy` deliberately do
    NOT ride the wire — they are UI copy the console resolves client-side from the id, so
    operator-facing text stays under our control rather than a worker's. `status` DOES,
    because failed[] mixes "we checked and it is broken" with "we could not check at all"
    and those need different operator copy; a row from an older sidecar that omits it
    degrades to "fail", the reading that never under-reports a problem."""
    if not isinstance(value, dict):
        return None
    out: dict[str, Any] = {
        "ok": bool(value.get("ok")),
        "blocking": bool(value.get("blocking")),
        # A body that omits `enforced` is an older/hand-rolled sidecar; assume the
        # safe reading (enforcement ON) rather than silently showing a fleet as
        # unenforced.
        "enforced": bool(value.get("enforced", True)),
    }
    ran_at = value.get("ranAt")
    if isinstance(ran_at, (int, float)) and not isinstance(ran_at, bool):
        ran_at = float(ran_at)
        if math.isfinite(ran_at):
            out["ranAt"] = ran_at
    rows: list[dict] = []
    raw_failed = value.get("failed")
    if isinstance(raw_failed, list):
        for entry in raw_failed[:_WORKER_MAX_PREFLIGHT_FAILED]:
            if not isinstance(entry, dict):
                continue
            check_id = entry.get("id")
            if not isinstance(check_id, str):
                continue
            check_id = check_id.strip()
            if check_id not in _WORKER_PREFLIGHT_CHECK_IDS:
                # The only open-ended shape: one row per advertised CDP platform.
                platform = check_id[len(_WORKER_PREFLIGHT_LOGIN_PREFIX):] \
                    if check_id.startswith(_WORKER_PREFLIGHT_LOGIN_PREFIX) else ""
                if platform not in CDP_PLATFORMS:
                    continue
            severity = entry.get("severity")
            if severity not in _WORKER_PREFLIGHT_SEVERITIES:
                continue
            status = entry.get("status")
            if status not in _WORKER_PREFLIGHT_STATUSES:
                status = "fail"
            detail = entry.get("detail")
            detail = (detail.strip()[:_WORKER_MAX_PREFLIGHT_DETAIL] or None
                      if isinstance(detail, str) else None)
            rows.append({"id": check_id, "severity": severity, "status": status,
                         "detail": detail})
    out["failed"] = rows
    try:
        size = len(json.dumps(out, ensure_ascii=False).encode("utf-8"))
    except (TypeError, ValueError):  # pragma: no cover — every field is a primitive
        return None
    return out if size <= _WORKER_MAX_PREFLIGHT_BYTES else None


def _validate_worker_register(payload: Any) -> tuple[Optional[dict], Optional[str]]:
    """Validate a worker register body. machineId is required only on a first
    register (the handler enforces that against the bearer mode); here it is an
    optional capped string. capabilities is an optional list of 3-tuples
    [org_id|None, platform, account_handle]."""
    if not isinstance(payload, dict):
        return None, "body must be a JSON object"
    out: dict[str, Any] = {}
    for key in ("machineId", "displayName", "host", "os", "agentVersion"):
        value, err = _opt_str_field(payload, key)
        if err is not None:
            return None, err
        out[key] = value

    org_id = payload.get("orgId")
    if org_id is not None and not isinstance(org_id, int):
        return None, "orgId must be an integer or null"
    out["orgId"] = org_id

    max_sessions = payload.get("maxSessions")
    if max_sessions is None:
        max_sessions = 1
    elif not isinstance(max_sessions, int) or isinstance(max_sessions, bool):
        return None, "maxSessions must be an integer"
    out["maxSessions"] = max(1, min(_WORKER_MAX_SESSIONS_CEIL, max_sessions))

    capabilities = payload.get("capabilities")
    if capabilities is None:
        capabilities = []
    if not isinstance(capabilities, list):
        return None, "capabilities must be a list"
    if len(capabilities) > _WORKER_MAX_CAPABILITIES:
        return None, f"capabilities must have at most {_WORKER_MAX_CAPABILITIES} entries"
    clean_caps = []
    for entry in capabilities:
        if not isinstance(entry, (list, tuple)) or len(entry) != 3:
            return None, "each capability must be [orgId, platform, accountHandle]"
        cap_org, cap_platform, cap_handle = entry
        if cap_org is not None and (not isinstance(cap_org, int) or isinstance(cap_org, bool)):
            return None, "capability orgId must be an integer or null"
        if cap_platform not in SUPPORTED_PLATFORMS:
            return None, f"capability platform must be one of {list(SUPPORTED_PLATFORMS)}"
        # accountHandle is OPTIONAL: null (or blank) = an UNPINNED / pool-wide capability
        # (this box serves any account for that platform). This mirrors the lease matcher
        # `_job_capability_covers`, which treats a None handle as unpinned and only
        # requires an exact handle for an account-PINNED job. A non-null handle must be a
        # non-empty string (the one-account-one-box pin).
        if cap_handle is None:
            clean_handle: Optional[str] = None
        elif isinstance(cap_handle, str) and cap_handle.strip():
            clean_handle = cap_handle.strip()
        else:
            return None, "capability accountHandle must be a non-empty string or null"
        clean_caps.append([cap_org, cap_platform, clean_handle])
    out["capabilities"] = clean_caps
    # v23: optional launch-preflight summary. Coerced, never rejected — see
    # _validate_preflight_summary. Always present in `out` so the handler can pass it
    # through unconditionally (None = "this box reported none", which
    # store.register_worker writes as NULL).
    out["preflight"] = _validate_preflight_summary(payload.get("preflight"))
    return out, None


def _validate_worker_heartbeat(payload: Any) -> tuple[Optional[dict], Optional[str]]:
    """Validate a worker presence heartbeat body. currentSessions/load is an optional
    non-negative int (the worker's live load); omitted leaves the stored value
    unchanged. The body's workerId/timestamp/chromeHealth are accepted and ignored
    (the bearer token is the authoritative identity). preflight (v23) is optional and
    omitted-means-unchanged, exactly like currentSessions — the sidecar only re-sends it
    when the summary CHANGED or every 10th beat, so a heartbeat without it must leave the
    stored one alone (store.record_worker_heartbeat COALESCEs)."""
    if payload is None:
        # Bodyless beat: BOTH keys must exist or the handler KeyErrors below.
        return {"currentSessions": None, "preflight": None}, None
    if not isinstance(payload, dict):
        return None, "body must be a JSON object"
    current = payload.get("currentSessions")
    if current is None:
        current = payload.get("load")
    if current is not None:
        if not isinstance(current, int) or isinstance(current, bool):
            return None, "currentSessions must be an integer"
        if current < 0:
            return None, "currentSessions must be non-negative"
        current = min(current, _WORKER_MAX_SESSIONS_CEIL)
    return {"currentSessions": current,
            "preflight": _validate_preflight_summary(payload.get("preflight"))}, None


def _parse_agent_version(v: Any) -> tuple:
    """Parse a dotted version like '1.4.2' into a comparable int tuple. Non-numeric
    junk in a segment truncates at the first non-digit ('2rc1' → 2); an empty/garbage
    version → (0,). Never raises — a malformed version compares as oldest."""
    out: list[int] = []
    for seg in str(v or "").split("."):
        digits = ""
        for ch in seg.strip():
            if ch.isdigit():
                digits += ch
            else:
                break
        out.append(int(digits) if digits else 0)
    return tuple(out) or (0,)


def _agent_version_below(worker_version: Any, minimum: Any) -> bool:
    """True iff the worker's agent version is older than the configured minimum. An
    unset/blank minimum ⇒ no gate (always False)."""
    if not minimum or not str(minimum).strip():
        return False
    return _parse_agent_version(worker_version) < _parse_agent_version(minimum)


def _min_agent_version() -> str:
    return os.environ.get(MIN_AGENT_VERSION_ENV, "").strip()


def _validate_control_flags(payload: Any) -> tuple[Optional[dict], Optional[str]]:
    """Validate an admin set/clear control-flags body. `scope` is required and one of
    CONTROL_FLAG_SCOPES; `scopeKey` is required for non-global scopes. `clear` (bool)
    deletes the row; otherwise at least one of drain/halt/updateRequired (each an
    optional bool) must be present. reason is an optional string."""
    if not isinstance(payload, dict):
        return None, "body must be a JSON object"
    scope = payload.get("scope")
    if scope not in CONTROL_FLAG_SCOPES:
        return None, f"scope must be one of {list(CONTROL_FLAG_SCOPES)}"
    scope_key = payload.get("scopeKey", "")
    if scope != "global":
        if scope_key is None or not str(scope_key).strip():
            return None, "scopeKey is required for a non-global scope"
        scope_key = str(scope_key).strip()
    else:
        scope_key = ""
    clear = bool(payload.get("clear"))
    flags: dict[str, Optional[bool]] = {}
    for body_key, store_key in (("drain", "drain"), ("halt", "halt"),
                                ("updateRequired", "update_required")):
        val = payload.get(body_key)
        if val is None:
            continue
        if not isinstance(val, bool):
            return None, f"{body_key} must be a boolean"
        flags[store_key] = val
    if not clear and not flags:
        return None, "provide clear:true or at least one of drain/halt/updateRequired"
    reason = payload.get("reason")
    if reason is not None and not isinstance(reason, str):
        return None, "reason must be a string or null"
    return {"scope": scope, "scopeKey": scope_key, "clear": clear,
            "flags": flags, "reason": reason}, None


def _validate_worker_revoke(payload: Any) -> tuple[Optional[dict], Optional[str]]:
    """Validate an admin worker-revoke body. `workerId` (non-empty string) required."""
    if not isinstance(payload, dict):
        return None, "body must be a JSON object"
    worker_id = payload.get("workerId")
    if not isinstance(worker_id, str) or not worker_id.strip():
        return None, "workerId is required"
    return {"workerId": worker_id.strip()}, None


def _validate_worker_enrolment_mint(payload: Any) -> tuple[Optional[dict], Optional[str]]:
    """Validate an admin mint-enrolment-token body (v22, BUILD-PLAN B8 fix). `scope`
    is required, one of 'org'/'pool'; 'org' REQUIRES a non-bool integer `orgId` (400
    otherwise) and 'pool' must NOT carry one (400 if supplied — a pool grant is a
    deliberate admin decision, so a stray orgId must not be silently dropped). `label`
    is optional, trimmed, capped. `ttlHours` is optional (default
    DEFAULT_ENROLMENT_TTL_HOURS), and OUT OF RANGE is rejected, never clamped — this
    is a security-relevant control (the token's standing-risk window), not a UX
    nicety."""
    if not isinstance(payload, dict):
        return None, "body must be a JSON object"
    scope = payload.get("scope")
    if scope not in ("org", "pool"):
        return None, "scope must be 'org' or 'pool'"
    org_id = payload.get("orgId")
    if scope == "org":
        if org_id is None or isinstance(org_id, bool) or not isinstance(org_id, int):
            return None, "orgId is required (integer) when scope='org'"
    else:
        if org_id is not None:
            return None, "orgId must not be supplied when scope='pool'"
    label = payload.get("label")
    if label is not None:
        if not isinstance(label, str):
            return None, "label must be a string"
        label = label.strip()[:200] or None
    ttl_hours = payload.get("ttlHours")
    if ttl_hours is None:
        ttl_hours = DEFAULT_ENROLMENT_TTL_HOURS
    elif not isinstance(ttl_hours, int) or isinstance(ttl_hours, bool):
        return None, "ttlHours must be an integer"
    elif not (1 <= ttl_hours <= MAX_ENROLMENT_TTL_HOURS):
        return None, f"ttlHours must be between 1 and {MAX_ENROLMENT_TTL_HOURS}"
    return {"scope": scope, "orgId": org_id, "label": label, "ttlHours": ttl_hours}, None


def _validate_worker_enrolment_revoke(payload: Any) -> tuple[Optional[dict], Optional[str]]:
    """Validate an admin revoke-enrolment-token body. `tokenId` (non-empty string)
    required — same shape as `_validate_worker_revoke`."""
    if not isinstance(payload, dict):
        return None, "body must be a JSON object"
    token_id = payload.get("tokenId")
    if not isinstance(token_id, str) or not token_id.strip():
        return None, "tokenId is required"
    return {"tokenId": token_id.strip()}, None


def _match_worker_job_route(path: str) -> Optional[tuple[str, str]]:
    """Parse ``/api/worker/jobs/{id}/{action}`` → ``(job_id, action)`` for a known
    action, else None. Exactly two path segments after the prefix; the id is URL-
    decoded. Keeps the variable-id job routes out of the static route dicts."""
    if not path.startswith(WORKER_JOBS_PREFIX):
        return None
    rest = path[len(WORKER_JOBS_PREFIX):]
    parts = rest.split("/")
    if len(parts) != 2:
        return None
    job_id, action = unquote(parts[0]), parts[1]
    if not job_id or action not in _WORKER_JOB_ACTIONS:
        return None
    return job_id, action


def _match_admin_org_route(path: str) -> Optional[tuple[int, str]]:
    """Parse ``/api/admin/orgs/{id}/{campaigns|leads|runs}`` → ``(org_id, subresource)``,
    else None. The cross-org read views (Phase 5d); the variable org id stays out of the
    static route dicts. Returns None on a non-integer id or an unknown subresource.

    The allow-list is the whole gate on the subresource half: an unknown segment must
    fall through to None (→ SPA/404) rather than reach the handler, so adding a name
    here is exactly as load-bearing as writing the handler itself."""
    if not path.startswith(ADMIN_ORGS_PREFIX):
        return None
    rest = path[len(ADMIN_ORGS_PREFIX):]
    parts = rest.split("/")
    if len(parts) != 2 or parts[1] not in _ADMIN_ORG_SUBRESOURCES:
        return None
    try:
        org_id = int(parts[0])
    except (ValueError, TypeError):
        return None
    if org_id <= 0:
        return None
    return org_id, parts[1]


def _validate_worker_lease(payload: Any) -> tuple[Optional[dict], Optional[str]]:
    """Validate a lease body. The worker's CAPABILITIES are NOT trusted from the body —
    they come from the authenticated worker row (set at register), so a worker can only
    lease for what it registered. The body carries only an optional long-poll timeout."""
    if payload is None:
        return {"leasePollTimeoutSec": 0}, None
    if not isinstance(payload, dict):
        return None, "body must be a JSON object"
    poll = payload.get("leasePollTimeoutSec")
    if poll is None:
        poll = 0.0
    else:
        # NaN would survive a bare `poll < 0` (every NaN comparison is False) and
        # then poison the long-poll deadline arithmetic; a 400-digit int would
        # OverflowError out of float(). _finite_number rejects both.
        poll, err = _finite_number(poll, "leasePollTimeoutSec")
        if err is not None:
            return None, err
        if poll < 0:
            return None, "leasePollTimeoutSec must be a non-negative number"
    return {"leasePollTimeoutSec": min(poll, WORKER_LEASE_POLL_MAX_SEC)}, None


def _validate_worker_spend(payload: dict) -> tuple[Optional[list], Optional[str]]:
    """Shared ack/nack validation of the B9 spend rollup: `spend` is an optional array
    of ``{stage, model, usd, at}`` rows. Tolerant like `leads` — non-object rows are
    dropped and the batch is capped here (bounded memory before the store) — so one
    ragged row never fails the whole report. Per-field coercion and the BOLA campaign
    forcing happen in `store._sync_acked_spend`."""
    spend = payload.get("spend")
    if spend is None:
        return [], None
    if not isinstance(spend, list):
        return None, "spend must be an array"
    return [row for row in spend if isinstance(row, dict)][:MAX_SYNC_SPEND_ROWS], None


def _validate_worker_db_id(payload: dict) -> tuple[Optional[str], Optional[str]]:
    """Shared ack/nack validation of `dbId` — the reporting box's database identity
    (store.database_id). Purely a same-database sentinel: when it equals the cloud
    Store's own id the spend rollup is skipped, because the worker's db_path IS this
    database and the rows are already here (see store._sync_acked_spend)."""
    db_id = payload.get("dbId")
    if db_id is None:
        return None, None
    if not isinstance(db_id, str):
        return None, "dbId must be a string or null"
    return db_id.strip()[:_WORKER_MAX_STR] or None, None


def _validate_worker_nack(payload: Any) -> tuple[Optional[dict], Optional[str]]:
    """Validate a nack body: reason (required, capped string), poison (optional bool),
    retryAfterAt (optional epoch number — e.g. the engine's daytime 'try again at'),
    plus the optional B9 `spend` rollup and `dbId` sentinel (a crashed attempt spent
    real money before it died — see store.nack_job), plus the optional `leads` batch.

    `leads` is the sibling of `spend` and arrived for the same reason: a run that never
    acks (dead-lettered at max attempts — the NORMAL end of a run that hits its
    wall-clock cap before its lead target) previously shipped its spend and kept its
    leads, so the customer was billed for a harvest they never received. Tolerant like
    the ack validator: non-object rows are dropped and the batch is capped here, so one
    ragged row never turns a nack into a 400 — and a REJECTED nack is worse than
    unsynced leads, because the attempts counter never increments and the job stays
    leased until ReclaimManager sweeps it."""
    if not isinstance(payload, dict):
        return None, "body must be a JSON object"
    reason = payload.get("reason")
    if not isinstance(reason, str) or not reason.strip():
        return None, "reason is required"
    poison = payload.get("poison", False)
    if not isinstance(poison, bool):
        return None, "poison must be a boolean"
    retry_after = payload.get("retryAfterAt")
    if retry_after is not None:
        # An epoch, so finiteness matters twice over: a stored Infinity would come
        # straight back out of the fleet-jobs JSON as an unparseable bare token.
        retry_after, err = _finite_number(retry_after, "retryAfterAt")
        if err is not None:
            return None, err
    leads = payload.get("leads")
    if leads is None:
        leads = []
    if not isinstance(leads, list):
        return None, "leads must be an array"
    leads = [row for row in leads if isinstance(row, dict)][:MAX_SYNC_LEADS]
    spend, err = _validate_worker_spend(payload)
    if err is not None:
        return None, err
    db_id, err = _validate_worker_db_id(payload)
    if err is not None:
        return None, err
    return {"reason": reason.strip()[:_WORKER_MAX_STR], "poison": poison,
            "retryAfterAt": float(retry_after) if retry_after is not None else None,
            "leads": leads, "spend": spend, "dbId": db_id}, None


def _validate_worker_ack(payload: Any) -> tuple[Optional[dict], Optional[str]]:
    """Validate an ack body: `summary` is an optional object (the engine run summary);
    `leads` is an optional array of captured-lead objects (Phase 3 sync-back); `spend`
    is the optional B9 spend rollup and `dbId` its same-database sentinel. Tolerant on
    `leads`/`spend` — non-object rows are dropped and the batch is capped here (bounded
    memory before the store) — so one ragged row never fails the whole ack. Per-field
    coercion + the BOLA campaign forcing happen in `store.ack_job`."""
    if payload is None:
        return {"summary": {}, "leads": [], "spend": [], "dbId": None}, None
    if not isinstance(payload, dict):
        return None, "body must be a JSON object"
    summary = payload.get("summary", {})
    if summary is None:
        summary = {}
    if not isinstance(summary, dict):
        return None, "summary must be an object"
    leads = payload.get("leads")
    if leads is None:
        leads = []
    if not isinstance(leads, list):
        return None, "leads must be an array"
    leads = [row for row in leads if isinstance(row, dict)][:MAX_SYNC_LEADS]
    spend, err = _validate_worker_spend(payload)
    if err is not None:
        return None, err
    db_id, err = _validate_worker_db_id(payload)
    if err is not None:
        return None, err
    return {"summary": summary, "leads": leads, "spend": spend, "dbId": db_id}, None


def _validate_enqueue(payload: Any) -> tuple[Optional[dict], Optional[str]]:
    """Validate an operator enqueue body. campaignId + platform are required; the run
    knobs (targetLeads/durationMinutes/engineMode/soulText) fold into the job `spec`.
    jobId is optional (minted if absent). orgId/requiredAccountHandle are optional."""
    if not isinstance(payload, dict):
        return None, "body must be a JSON object"
    campaign_id = payload.get("campaignId")
    if not isinstance(campaign_id, str) or not campaign_id.strip():
        return None, "campaignId is required"
    platform = payload.get("platform")
    if platform not in SUPPORTED_PLATFORMS:
        return None, f"platform must be one of {list(SUPPORTED_PLATFORMS)}"

    org_id = payload.get("orgId")
    if org_id is not None and (not isinstance(org_id, int) or isinstance(org_id, bool)):
        return None, "orgId must be an integer or null"

    handle = payload.get("requiredAccountHandle")
    if handle is not None and not isinstance(handle, str):
        return None, "requiredAccountHandle must be a string or null"
    handle = handle.strip() if isinstance(handle, str) and handle.strip() else None

    engine_mode = payload.get("engineMode", "harvest")
    if engine_mode not in ("harvest", "warming"):
        return None, "engineMode must be 'harvest' or 'warming'"

    target_leads = payload.get("targetLeads")
    if target_leads is not None and (not isinstance(target_leads, int)
                                     or isinstance(target_leads, bool) or target_leads < 1):
        return None, "targetLeads must be a positive integer or null"
    duration = payload.get("durationMinutes")
    if duration is not None and (not isinstance(duration, int)
                                 or isinstance(duration, bool) or duration < 1):
        return None, "durationMinutes must be a positive integer or null"
    soul_text = payload.get("soulText")
    if soul_text is not None and not isinstance(soul_text, str):
        return None, "soulText must be a string or null"

    job_id = payload.get("jobId")
    if job_id is not None and (not isinstance(job_id, str) or not job_id.strip()):
        return None, "jobId, if present, must be a non-empty string"

    return {
        "jobId": job_id.strip() if isinstance(job_id, str) else None,
        "campaignId": campaign_id.strip(),
        "platform": platform,
        "orgId": org_id,
        "requiredAccountHandle": handle,
        "spec": {"target_leads": target_leads, "duration_minutes": duration,
                 "engine_mode": engine_mode, "soul_text": soul_text},
    }, None


def _validate_telegram_verify(payload: Any) -> tuple[Optional[dict], Optional[str]]:
    if not isinstance(payload, dict):
        return None, "body must be a JSON object"
    token = payload.get("token")
    if not isinstance(token, str) or not token.strip():
        return None, "token is required"
    code = payload.get("code")
    if not isinstance(code, str) or not code.strip():
        return None, "code is required"
    password = payload.get("password")
    if password is not None and not isinstance(password, str):
        return None, "password, if present, must be a string"
    return {"token": token.strip(), "code": code.strip(),
            "password": password.strip() if isinstance(password, str) and password.strip()
            else None}, None


def _fleet_spend_cap_usd() -> Optional[float]:
    """The per-campaign $ ceiling this bridge KNOWS the fleet enforces, or None when it
    knows of none. Used ONLY as an early, informational dispatch skip for a campaign
    that is already over budget — the box's own `AIZU_SPEND_CAP` is, and stays, the
    authoritative ceiling (`job_runner._effective_spend_cap` re-bases it against the
    cloud total that now rides the lease, and `run_one_job` refuses to spawn a run with
    zero headroom left). `campaign_meta.budget_cap` stays display-only.

    RETURNS None UNLESS `AIZU_SPEND_CAP` IS SET ON THIS PROCESS, and that asymmetry is
    the whole point. `AIZU_SPEND_CAP` is a WORKER-plane variable (see CLAUDE.md): in the
    hosted split topology — the only one the distributed backend exists for — it is set
    on the boxes and NOT on the bridge. A hard-coded fallback here would therefore have
    the cloud enforce a ceiling no worker actually uses: because `total_spend` is a
    LIFETIME sum that never resets and B9 now finally feeds fleet dollars into it, any
    long-lived campaign would eventually cross that guessed number and 409 FOREVER, with
    no operator control able to lift it. So "the cloud does not know the fleet's cap" now
    means "do not skip" rather than "skip at $20" — the box, which is the only process
    that can read its own cap, remains the sole enforcer. When the var IS set here
    (same-box dev/desktop, or a deployment that deliberately mirrors it) it is an
    explicit operator statement of the fleet ceiling, so we honour it.

    Why the skip exists at all (B9): before the cloud spend total rode the lease, a fresh
    box always started at $0, so an over-budget campaign never tripped
    `router._spend_guard` on call one. Now it can — and `_degrade` does NOT stop a run,
    it returns an abstain-with-low-confidence stand-in. Skipping at dispatch just fails
    faster and more legibly than the box-side refusal that backstops it."""
    raw = os.environ.get("AIZU_SPEND_CAP", "").strip()
    if not raw:
        log.debug("no AIZU_SPEND_CAP on the bridge — leaving the spend ceiling to the "
                  "worker boxes (no dispatch-time spend skip)")
        return None
    try:
        cap = float(raw)
    except ValueError:
        log.warning("AIZU_SPEND_CAP=%r is not a number — skipping the dispatch-time "
                    "spend check and leaving the ceiling to the worker boxes", raw)
        return None
    if cap <= 0:
        log.warning("AIZU_SPEND_CAP=%r is not positive — skipping the dispatch-time "
                    "spend check and leaving the ceiling to the worker boxes", raw)
        return None
    return cap


def _split_lead_budget(total: int, n: int) -> list[int]:
    """Split a lead-cap budget across `n` distributed jobs so the slices sum to EXACTLY
    `total` (never N× the cap): the first `total % n` jobs get one extra. A slice of 0
    (more campaigns than remaining budget) is the caller's signal to SKIP that job, not
    to run it unbounded. Used by the distributed scope='all' fleet dispatch."""
    if n <= 0:
        return []
    base, extra = divmod(max(0, total), n)
    return [base + (1 if i < extra else 0) for i in range(n)]


def _campaign_platforms(campaign: Campaign) -> set[str]:
    """Every platform one campaign actually discovers on. A multi-platform brief lists
    them as `channels`; the legacy single-platform brief collapses to the flat
    `platform` scalar (core.config.campaign_to_brief's collapse rule), so an empty
    `channels` is not "no platforms" — it means "just this one"."""
    if campaign.channels:
        return {channel.platform for channel in campaign.channels}
    return {campaign.platform}


def _validate_run(payload: Any) -> tuple[Optional[dict], Optional[str]]:
    """Shape-only validation for POST /api/run (runnability is checked in the
    handler, which has the DB). Exactly one of campaignId / all:true; mode defaults
    to the safe 'dry'."""
    if not isinstance(payload, dict):
        return None, "body must be a JSON object"
    campaign_id = payload.get("campaignId")
    run_all = payload.get("all")
    if run_all is not None and not isinstance(run_all, bool):
        return None, "all, if present, must be a boolean"
    has_campaign = campaign_id is not None
    if has_campaign and (not isinstance(campaign_id, str) or not campaign_id.strip()):
        return None, "campaignId, if present, must be a non-empty string"
    is_all = run_all is True
    if has_campaign == is_all:  # both provided, or neither
        return None, "provide exactly one of campaignId or all"
    mode = payload.get("mode", "dry")
    if mode not in VALID_RUN_MODES:
        return None, f"invalid mode (expected one of {sorted(VALID_RUN_MODES)})"
    target = payload.get("targetLeadCount")
    target_lead_count: Optional[int] = None
    if target is not None:
        # bool is an int subclass — reject True/False explicitly.
        if isinstance(target, bool) or not isinstance(target, int) or target <= 0:
            return None, "targetLeadCount, if present, must be a positive integer"
        if target > MAX_RUN_LEAD_TARGET:
            return None, f"targetLeadCount must be {MAX_RUN_LEAD_TARGET} or less"
        target_lead_count = target
    duration = payload.get("durationMinutes")
    duration_minutes: Optional[int] = None
    if duration is not None:
        # bool is an int subclass — reject True/False explicitly.
        if isinstance(duration, bool) or not isinstance(duration, int) or duration <= 0:
            return None, "durationMinutes, if present, must be a positive integer"
        if duration > MAX_RUN_DURATION_MINUTES:
            return None, f"durationMinutes must be {MAX_RUN_DURATION_MINUTES} (12h) or less"
        duration_minutes = duration
    return {"scope": "all" if is_all else "campaign",
            "campaignId": campaign_id.strip() if has_campaign else None,
            "mode": mode, "targetLeadCount": target_lead_count,
            "durationMinutes": duration_minutes}, None


def _validate_credentials(payload: Any, *, enforce_policy: bool) -> tuple[Optional[dict], Optional[str]]:
    """Validate an {email, password} body. Email is normalised (lowercased/stripped).

    `enforce_policy` applies the signup password rules (min length); login passes
    False so it never leaks the policy or rejects an otherwise-valid attempt — it
    only requires a non-empty, length-bounded string.
    """
    if not isinstance(payload, dict):
        return None, "body must be a JSON object"
    email = payload.get("email")
    password = payload.get("password")
    if not isinstance(email, str) or not _EMAIL_RE.match(email.strip().lower()):
        return None, "invalid email"
    if not isinstance(password, str) or not password:
        return None, "missing or empty field: password"
    if len(password) > MAX_PASSWORD_LENGTH:
        return None, f"password must be at most {MAX_PASSWORD_LENGTH} characters"
    if enforce_policy and len(password) < MIN_PASSWORD_LENGTH:
        return None, f"password must be at least {MIN_PASSWORD_LENGTH} characters"
    return {"email": email.strip().lower(), "password": password}, None


def _validate_admin_login(payload: Any) -> tuple[Optional[dict], Optional[str]]:
    """Admin login body: {email, password, totpCode}. The TOTP code is required — MFA is
    mandatory on the admin plane (PRD §10). Never leaks the password policy (like login)."""
    creds, err = _validate_credentials(payload, enforce_policy=False)
    if err is not None:
        return None, err
    totp = payload.get("totpCode")
    if not isinstance(totp, str) or not totp.strip():
        return None, "missing or empty field: totpCode"
    if len(totp.strip()) > 16:  # a TOTP code is 6 digits; bound the input hard
        return None, "invalid totpCode"
    return {**creds, "totpCode": totp.strip()}, None


def _validate_impersonate(payload: Any) -> tuple[Optional[dict], Optional[str]]:
    """Impersonation body: exactly one of {orgId, userId}, plus a required `reason` (it is
    audited). Rejects bool (a JSON true/1 must not slip through as an id)."""
    if not isinstance(payload, dict):
        return None, "body must be a JSON object"
    org_id, user_id, reason = payload.get("orgId"), payload.get("userId"), payload.get("reason")
    has_org, has_user = org_id is not None, user_id is not None
    if has_org == has_user:
        return None, "provide exactly one of orgId or userId"
    if has_org and (not isinstance(org_id, int) or isinstance(org_id, bool) or org_id <= 0):
        return None, "orgId must be a positive integer"
    if has_user and (not isinstance(user_id, int) or isinstance(user_id, bool) or user_id <= 0):
        return None, "userId must be a positive integer"
    if not isinstance(reason, str) or not reason.strip():
        return None, "reason is required"
    if len(reason.strip()) > 500:
        return None, "reason exceeds 500 characters"
    return {"orgId": org_id if has_org else None,
            "userId": user_id if has_user else None, "reason": reason.strip()}, None


def _opt_company_str(value: Any, name: str, maxlen: int) -> tuple[Optional[str], Optional[str]]:
    if value is None:
        return None, None
    if not isinstance(value, str):
        return None, f"{name} must be a string"
    v = value.strip()
    if len(v) > maxlen:
        return None, f"{name} exceeds {maxlen} characters"
    return (v or None), None


def _validate_signup(payload: Any) -> tuple[Optional[dict], Optional[str]]:
    """Signup is one of two flows: with an `inviteToken` you JOIN an existing org
    (company fields ignored); without one you CREATE a company (name required)."""
    creds, err = _validate_credentials(payload, enforce_policy=True)
    if err is not None:
        return None, err
    invite_token = payload.get("inviteToken")
    if invite_token is not None and (not isinstance(invite_token, str) or not invite_token.strip()):
        return None, "inviteToken, if present, must be a non-empty string"
    out: dict[str, Any] = {**creds,
                           "inviteToken": invite_token.strip() if isinstance(invite_token, str) else None}
    if out["inviteToken"]:
        return out, None
    name = payload.get("companyName")
    if not isinstance(name, str) or not name.strip():
        return None, "companyName is required"
    if len(name.strip()) > _MAX_COMPANY_NAME:
        return None, f"companyName exceeds {_MAX_COMPANY_NAME} characters"
    logo, err = _opt_company_str(payload.get("companyLogo"), "companyLogo", _MAX_COMPANY_LOGO)
    if err:
        return None, err
    desc, err = _opt_company_str(payload.get("companyDescription"),
                                 "companyDescription", _MAX_COMPANY_DESC)
    if err:
        return None, err
    out.update({"companyName": name.strip(), "companyLogo": logo, "companyDescription": desc})
    return out, None


def _validate_billing_checkout(payload: Any) -> tuple[Optional[dict], Optional[str]]:
    """A checkout is {tier, interval}. Free has no checkout; Scale is sales-led —
    both are rejected here so create_checkout only ever sees lite/starter/pro."""
    if not isinstance(payload, dict):
        return None, "body must be a JSON object"
    tier = payload.get("tier")
    interval = payload.get("interval")
    if tier not in billing.SELF_SERVE_TIERS:
        return None, f"tier must be one of {sorted(billing.SELF_SERVE_TIERS)}"
    if interval not in billing.VALID_INTERVALS:
        return None, "interval must be 'month' or 'year'"
    return {"tier": tier, "interval": interval}, None


def _billing_reset_label(sub: dict[str, Any]) -> str:
    """Human reset point for a 402 message: the period end for a paid plan, or the
    start of next month for Free (which has no provider period)."""
    end = sub.get("current_period_end")
    if end:
        return "on " + datetime.fromtimestamp(float(end), timezone.utc).strftime("%Y-%m-%d")
    return "at the start of next month"


def _billing_inactive_message(sub: dict[str, Any]) -> str:
    return (f"Your subscription is {sub.get('status', 'inactive')}. "
            "Update billing in Settings to start new runs.")


def _billing_cap_message(sub: dict[str, Any]) -> str:
    return (f"Plan limit reached ({sub.get('lead_cap')} leads). "
            f"Resets {_billing_reset_label(sub)}. Upgrade to keep running.")


def _billing_reveal_cap_message(sub: dict[str, Any]) -> str:
    """The 402 an org hits when its plan's per-period REVEAL allowance is full (v27).

    Same idiom as `_billing_cap_message` — the number, the reset, the fix — because it
    is the same allowance: an org may not un-anonymize more DISTINCT leads in a period
    than the plan let it capture. Without a cap, `POST /api/lead/reveal` is a per-lead
    endpoint that a twenty-line script walks into the bulk export the redaction exists
    to prevent.
    """
    return (f"Plan limit reached ({sub.get('lead_cap')} lead reveals). "
            f"Resets {_billing_reset_label(sub)}. Upgrade to reveal more leads.")


def _billing_campaign_cap_message(cap: int, tier: str) -> str:
    """The 402 an org hits when its plan's CAMPAIGN allowance is full (v27). Names the
    number and the plan for the same reason _billing_cap_message does: the fix is an
    upgrade (or archiving a campaign), and a bare "limit reached" reads as a bug."""
    display = billing.TIERS.get(tier, billing.TIERS["free"])["display_name"]
    return (f"Plan limit reached ({cap} campaigns on {display}). "
            "Upgrade to add more campaigns.")


class _HeadBodySuppressor:
    """A `wfile` shim that passes headers through and swallows the response BODY.

    `do_HEAD` re-runs the ordinary GET router so a HEAD gets byte-identical status
    and headers (RFC 9110: a HEAD response SHOULD carry the header fields the GET
    would have). Only the body must not be written — and every body in this class
    goes out through `self.wfile.write` AFTER `end_headers()`, so flipping one flag
    there is the whole mechanism.
    """

    def __init__(self, real: Any):
        self.real = real
        self.suppress = False

    def write(self, data):
        if self.suppress:
            return len(data)
        return self.real.write(data)

    def __getattr__(self, name):   # flush/close/fileno/… → the real socket writer
        return getattr(self.real, name)


class PanelHandler(SimpleHTTPRequestHandler):
    # Response identity. The stdlib default advertises "SimpleHTTP/0.6
    # Python/3.12.13" on EVERY response — a free version-fingerprint for anyone
    # scanning, and a lie about what this service is. sys_version="" drops the
    # Python half entirely (BaseHTTPRequestHandler joins the two with a space).
    server_version = "aizu-bridge"
    sys_version = ""

    def version_string(self) -> str:
        # The base joins server_version + ' ' + sys_version; with sys_version empty
        # that leaves a trailing space. Emit the token on its own.
        return self.server_version
    # set by the factory
    panel_dir: str = "."
    db_path: str = "aizu.db"
    config_dir: str = "config"
    run_manager: Optional[RunManager] = None
    login_throttle: Optional[LoginThrottle] = None
    invite_throttle: Optional[InviteThrottle] = None
    telegram_login: Optional[TelegramLoginManager] = None
    # name → BillingProvider. None/empty = billing not configured (checkout/portal
    # 503, webhook 503). Populated by serve() from env, or injected by tests.
    billing_providers: Optional[dict[str, "billing.BillingProvider"]] = None
    # Agent-readiness seams. None = the real probes (readiness.check_readiness /
    # readiness.open_login_tab). Injected by tests so a suite with no warmed Chrome
    # can exercise both sides of the gate without a 5s Playwright timeout per call.
    readiness_probe: Optional[Callable[..., dict]] = None
    login_opener: Optional[Callable[[], bool]] = None

    # NOTE (FIX 3): protocol_version stays HTTP/1.0 (no keep-alive). Every response
    # path DOES set Content-Length (JSON via _send_json_body, the SPA shell via
    # _serve_index, static files via SimpleHTTPRequestHandler, 204 preflight has no
    # body), so keep-alive would be framing-safe — BUT enabling HTTP/1.1 makes the
    # worker's single reused httpx.Client multiplex register + lease over ONE
    # connection, which breaks the register-rotates-the-token contract and yields a
    # real 401 (test_real_dispatch_integration). Correctness over the minor
    # keep-alive win: left at HTTP/1.0. See followUps.

    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=self.panel_dir, **kwargs)

    def handle_one_request(self):  # stamp a start time for per-request latency
        self._t0 = time.time()
        self._response_started = False   # per-request; read by _dispatch_guarded
        self._nosniff_sent = False       # per-request; read by end_headers
        # A client that dies mid-request (common: dev_panel restarting the server
        # mid-long-poll) surfaces as BrokenPipe/ConnectionReset. Swallow ONLY those
        # so the default handle_error stops dumping a stack trace to stderr; every
        # other exception propagates unchanged.
        try:
            super().handle_one_request()
        except (BrokenPipeError, ConnectionResetError):
            log.debug("client disconnected mid-request")

    def end_headers(self):  # noqa: N802
        # nosniff on EVERY response, including the static files SimpleHTTPRequest-
        # Handler sends itself: without it a browser is free to sniff a 404/HTML
        # body into whatever content type the URL's extension suggests and execute
        # it. One override is the only place that covers every send path.
        if not getattr(self, "_nosniff_sent", False):
            self._nosniff_sent = True
            self.send_header("X-Content-Type-Options", "nosniff")
        super().end_headers()
        # HEAD: headers are on the wire, so everything after this is body.
        wfile = self.wfile
        if isinstance(wfile, _HeadBodySuppressor):
            wfile.suppress = True

    def send_response_only(self, code, message=None):  # noqa: N802
        # Every response path in this class and its bases funnels through here, so
        # it is the one honest place to record "the client has been answered".
        # `_dispatch_guarded` reads the flag: appending a second status line to an
        # already-started response would corrupt the stream, so it stays silent.
        self._response_started = True
        super().send_response_only(code, message)

    def log_request(self, code="-", size="-"):  # noqa: N802
        """Structured access log: method, path, status, latency. Replaces the
        default stderr line; query strings are kept (no secrets ride in them —
        tokens live in the cookie/body) and the redaction filter is the backstop.

        The path is TRUNCATED (`_log_path`): it is attacker-controlled and can be
        ~64 KB, and the console sink renders per character while holding the GIL —
        so logging it whole let any anonymous client stall the whole server just
        by requesting a very long URL."""
        code_val = getattr(code, "value", code)
        dt_ms = (time.time() - getattr(self, "_t0", time.time())) * 1000
        method = getattr(self, "command", "?")
        path = _log_path(getattr(self, "path", "?"))
        emit = log.warning if isinstance(code_val, int) and code_val >= 400 else log.info
        emit("%s %s → %s · %.0fms", method, path, code_val, dt_ms)

    def log_message(self, *args):  # default warnings/errors → our logger
        # Same bound as log_request: http.server's own messages interpolate the
        # raw request line / path, so cap each argument before it reaches a sink.
        fmt = _log_path(args[0]) if args else ""
        log.debug("http: " + fmt, *(_log_path(a) if isinstance(a, str) else a
                                    for a in args[1:]))

    def _serve_html_file(self, relative_path: str) -> None:
        # Shared read/send for the two top-level HTML shells (landing + SPA). Both
        # get Cache-Control: no-store rather than letting the browser/any front proxy
        # cache them: neither is fingerprinted (unlike /assets/*, which is hashed by
        # Vite and safe to cache forever), so a stale cached copy after a deploy would
        # otherwise stick around for the browser's default heuristic lifetime. The
        # hashed sub-resources they reference (/assets/*, /landing/*) are still cached
        # normally by the static-file path below.
        payload = (Path(self.panel_dir) / relative_path).read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(payload)

    def _serve_index(self) -> None:
        # Public marketing home page ("/", "/index.html"). No auth required — it must
        # be reachable by anonymous visitors, which is the entire point of the split.
        self._serve_html_file("index.html")

    def _serve_app_index(self) -> None:
        # The React SPA shell, now hosted under /app/ instead of "/". The app fetches
        # /api/state (and everything else) itself once loaded; this just returns the
        # shell HTML for any /app path the hash router will resolve client-side.
        self._serve_html_file("app/index.html")

    def _send_cors_headers(self) -> None:
        origin = self.headers.get("Origin", "")
        if origin and _is_allowed_origin(origin):
            self.send_header("Access-Control-Allow-Origin", origin)
            self.send_header("Vary", "Origin")

    def _send_json_body(self, code: int, body: bytes,
                        extra_headers: Optional[list[tuple[str, str]]] = None) -> None:
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self._send_cors_headers()
        for key, value in (extra_headers or ()):
            self.send_header(key, value)
        self.end_headers()
        self.wfile.write(body)
        if log.isEnabledFor(logging.DEBUG):  # firehose: full outgoing body
            # The worker-register response carries a one-time PLAINTEXT token. The
            # RedactingFilter scrubs "token": shapes, but it is the only backstop —
            # suppress the body entirely here so a misconfigured/extra handler can
            # never leak the secret (LOCKED #4: never log the plaintext token).
            if urlparse(getattr(self, "path", "")).path == WORKER_REGISTER_PATH:
                log.debug("← resp %s %s · «register response body suppressed»",
                          code, _log_path(getattr(self, "path", "?")))
            else:
                log.debug("← resp %s %s · %s", code, _log_path(getattr(self, "path", "?")),
                          body.decode("utf-8", "replace")[:_BODY_LOG_MAX])

    def _send_json(self, code: int, ok: bool, data: Any = None,
                   error: Optional[str] = None,
                   extra_headers: Optional[list[tuple[str, str]]] = None,
                   error_code: Optional[str] = None) -> None:
        # Surface the *reason* for every failed response — the access log line
        # only carries method/path/status, so without this an error reads as a
        # bare "→ 400" with no explanation.
        if not ok and isinstance(code, int) and code >= 400:
            emit = log.error if code >= 500 else log.warning
            emit("API error · %s %s → %s · %s",
                 getattr(self, "command", "?"), _log_path(getattr(self, "path", "?")),
                 code, error or "(no message)")
        envelope: dict[str, Any] = {"ok": ok, "data": data, "error": error}
        # A machine-readable discriminator for the few errors a client must branch
        # on (currently only `campaign_exists`). Added ONLY when asked for, so every
        # other response keeps its exact three-key shape.
        if error_code is not None:
            envelope["code"] = error_code
        body = _json_bytes(envelope)
        self._send_json_body(code, body, extra_headers=extra_headers)

    def _send_internal_error(self, what: str) -> None:
        """Answer a generic 500 and put the real cause in the LOG, never the body.

        An internal message in an API response — a SQLite driver string, an absolute
        filesystem path — is useless to the panel (which renders it verbatim to an
        operator) and an information leak to anyone else. The client gets one stable,
        generic error; the operator keeps the full traceback. Typed, deliberate
        errors (a 400 for a bad brief, a 503 for unconfigured billing) are unaffected
        — this is only for the unexpected."""
        log.error("%s failed · %s %s", what, getattr(self, "command", "?"),
                  _log_path(getattr(self, "path", "?")), exc_info=True)
        self._send_json(500, False, error="internal server error")

    def _read_raw_body(self, max_bytes: int = MAX_BODY_BYTES) -> Optional[bytes]:
        """Read the raw request body bytes (no JSON parse). Used by the billing
        webhook, whose signature is computed over the exact bytes — re-encoding via
        json would break verification. None on a missing/oversized body."""
        try:
            length = int(self.headers.get("Content-Length", ""))
        except ValueError:
            return None
        if not 0 < length <= max_bytes:
            return None
        return self.rfile.read(length)

    def _read_json_body(self, max_bytes: int = MAX_BODY_BYTES
                        ) -> tuple[Optional[Any], Optional[str]]:
        try:
            length = int(self.headers.get("Content-Length", ""))
        except ValueError:
            return None, "missing or invalid Content-Length"
        if not 0 < length <= max_bytes:
            return None, f"body must be 1–{max_bytes} bytes"
        raw = self.rfile.read(length)
        if log.isEnabledFor(logging.DEBUG):  # firehose: full incoming body
            log.debug("→ body %s %s · %s", getattr(self, "command", "?"),
                      _log_path(getattr(self, "path", "?")),
                      raw.decode("utf-8", "replace")[:_BODY_LOG_MAX])
        try:
            return json.loads(raw), None
        except (json.JSONDecodeError, UnicodeDecodeError):
            return None, "body is not valid JSON"

    def _dispatch_guarded(self, route: Callable[[], None]) -> None:
        """Last-resort guard around a request router.

        An exception escaping do_GET/do_POST propagates out of
        `BaseHTTPRequestHandler.handle_one_request`, which dumps a full traceback
        (with absolute filesystem paths) to stderr and then drops the socket with
        NO HTTP response at all — the client sees an empty reply, not an error.
        That was reachable from a plain POST (a 400-digit number OverflowError-ing
        inside a validator, which runs before the handler's own try-block).

        Every EXPECTED failure is still answered by the typed errors inside the
        routers; this only catches the unexpected one, answers a generic 500, and
        keeps the detail in the log where it belongs — never in the response body.
        """
        try:
            route()
        except (BrokenPipeError, ConnectionResetError):
            raise            # handle_one_request logs these as a clean client hangup
        except Exception:    # noqa: BLE001 — the whole point is to catch everything
            log.error("unhandled error serving %s %s",
                      getattr(self, "command", "?"),
                      _log_path(getattr(self, "path", "?")), exc_info=True)
            if getattr(self, "_response_started", False):
                return       # a status line is already on the wire; do not append
            try:
                self._send_json(500, False, error="internal server error")
            except Exception:  # noqa: BLE001 — the socket itself is gone
                pass

    def do_POST(self):  # noqa: N802
        self._dispatch_guarded(self._route_post)

    def _route_post(self) -> None:
        path = urlparse(self.path).path
        # Same-origin fetches from the panel send no Origin or a local one (or,
        # on a hosted deployment, one named in AIZU_ALLOWED_ORIGINS); any other
        # website POSTing at the bridge sends its own — reject it.
        origin = self.headers.get("Origin", "")
        if origin and not _is_allowed_origin(origin):
            self._send_json(403, False, error="cross-origin request rejected")
            return
        # Logout needs neither a body nor a prior session payload — handle first.
        if path == AUTH_LOGOUT_PATH:
            self._handle_logout()
            return
        # v15 superadmin plane (separate MFA + IP-allowlist gate, NOT the org gate).
        # Login/logout are matched here so they bypass the org cookie/RBAC gate below.
        if path == ADMIN_LOGOUT_PATH:
            self._handle_admin_logout()
            return
        if path == ADMIN_LOGIN_PATH:
            payload, err = self._read_json_body(MAX_BODY_BYTES)
            if err is not None:
                self._send_json(400, False, error=err)
                return
            self._handle_admin_login(payload)
            return
        if path == ADMIN_IMPERSONATE_END_PATH:
            self._handle_admin_impersonate_end()
            return
        if path == ADMIN_IMPERSONATE_PATH:
            payload, err = self._read_json_body(MAX_BODY_BYTES)
            if err is not None:
                self._send_json(400, False, error=err)
                return
            self._handle_admin_impersonate(payload)
            return
        # Billing webhook is PUBLIC and provider-signed: it carries no session and
        # must be verified on the RAW body. Handle it before the protected gate
        # (mirrors the AUTH paths) and read the body itself — never via the JSON
        # parser, whose re-encode would break the signature.
        if path == BILLING_WEBHOOK_PATH:
            self._handle_billing_webhook()
            return
        # Worker plane: bearer-token gated, NOT cookie/RBAC. Matched before the
        # session gate. Each handler reads its own body with WORKER_MAX_BODY_BYTES
        # (register/presence can carry capability + metric blobs over the 64 KB
        # default). The cross-origin check above already ran; a browser with a
        # foreign Origin is rejected, while a headless worker (no Origin) passes.
        if path == WORKER_REGISTER_PATH:
            payload, err = self._read_json_body(WORKER_MAX_BODY_BYTES)
            if err is not None:
                self._send_json(400, False, error=err)
                return
            self._handle_worker_register(payload)
            return
        if path == WORKER_HEARTBEAT_PATH:
            payload, err = self._read_json_body(WORKER_MAX_BODY_BYTES)
            if err is not None:
                self._send_json(400, False, error=err)
                return
            self._handle_worker_heartbeat(payload)
            return
        if path == WORKER_LEASE_PATH:
            payload, err = self._read_json_body(WORKER_MAX_BODY_BYTES)
            if err is not None:
                self._send_json(400, False, error=err)
                return
            self._handle_worker_lease(payload)
            return
        job_route = _match_worker_job_route(path)
        if job_route is not None:
            job_id, action = job_route
            payload, err = self._read_json_body(WORKER_MAX_BODY_BYTES)
            if err is not None:
                self._send_json(400, False, error=err)
                return
            self._handle_worker_job_action(job_id, action, payload)
            return
        # Operator enqueue: cookie-session + interim platform-admin allowlist gated
        # (NOT bearer, NOT the org-RBAC routes) — the handler runs its own gate, like
        # the fleet view. Matched here so it bypasses the org/RBAC protected gate below.
        if path == ADMIN_ENQUEUE_PATH:
            payload, err = self._read_json_body(WORKER_MAX_BODY_BYTES)
            if err is not None:
                self._send_json(400, False, error=err)
                return
            self._handle_admin_enqueue(payload)
            return
        # Phase 4 lifecycle controls: same interim platform-admin allowlist gate as the
        # fleet view / enqueue (Phase 5 replaces it with the real admin plane).
        if path == ADMIN_CONTROL_FLAGS_PATH:
            payload, err = self._read_json_body(WORKER_MAX_BODY_BYTES)
            if err is not None:
                self._send_json(400, False, error=err)
                return
            self._handle_admin_control_flags(payload)
            return
        if path == ADMIN_WORKER_REVOKE_PATH:
            payload, err = self._read_json_body(WORKER_MAX_BODY_BYTES)
            if err is not None:
                self._send_json(400, False, error=err)
                return
            self._handle_admin_worker_revoke(payload)
            return
        # v22 (BUILD-PLAN B8 fix): per-worker enrolment token mint/revoke.
        if path == ADMIN_WORKER_ENROLMENT_TOKENS_PATH:
            payload, err = self._read_json_body(WORKER_MAX_BODY_BYTES)
            if err is not None:
                self._send_json(400, False, error=err)
                return
            self._handle_admin_worker_enrolment_mint(payload)
            return
        if path == ADMIN_WORKER_ENROLMENT_TOKEN_REVOKE_PATH:
            payload, err = self._read_json_body(WORKER_MAX_BODY_BYTES)
            if err is not None:
                self._send_json(400, False, error=err)
                return
            self._handle_admin_worker_enrolment_revoke(payload)
            return
        # v16 execution-backend switch: route EVERY run to the in-process RunManager
        # or the distributed worker fleet (real admin plane gate, handler-side).
        if path == ADMIN_EXECUTION_BACKEND_PATH:
            payload, err = self._read_json_body(MAX_BODY_BYTES)
            if err is not None:
                self._send_json(400, False, error=err)
                return
            self._handle_admin_set_execution_backend(payload)
            return
        # v17 model-comparison switch: superadmin on/off for the LLM fan-out.
        if path == ADMIN_MODEL_COMPARISON_PATH:
            payload, err = self._read_json_body(MAX_BODY_BYTES)
            if err is not None:
                self._send_json(400, False, error=err)
                return
            self._handle_admin_set_model_comparison(payload)
            return
        # Public auth endpoints (no session required); everything else is gated.
        auth_routes = {
            AUTH_SIGNUP_PATH: self._handle_signup,
            AUTH_LOGIN_PATH: self._handle_login,
        }
        protected_routes = {
            STATUS_PATH: self._handle_status,
            STATUS_BULK_PATH: self._handle_status_bulk,
            LEAD_NOTE_PATH: self._handle_lead_note,
            LEAD_REVEAL_PATH: self._handle_lead_reveal,
            CAMPAIGN_PATH: self._handle_campaign,
            CAMPAIGN_GENERATE_PATH: self._handle_generate_campaign,
            CAMPAIGN_INTERVIEW_PATH: self._handle_campaign_interview,
            CAMPAIGN_ARCHIVE_PATH: self._handle_campaign_archive,
            CAMPAIGN_SCHEDULE_PATH: self._handle_campaign_schedule,
            TEAM_PATH: self._handle_team,
            INVITE_PATH: self._handle_invite,
            ORG_PATH: self._handle_org,
            SETTINGS_PATH: self._handle_settings,
            INTEGRATION_PATH: self._handle_integration,
            TELEGRAM_START_PATH: self._handle_telegram_start,
            TELEGRAM_VERIFY_PATH: self._handle_telegram_verify,
            BILLING_CHECKOUT_PATH: self._handle_billing_checkout,
            BILLING_PORTAL_PATH: self._handle_billing_portal,
            RUN_PATH: self._handle_run,
            RUN_STOP_PATH: self._handle_run_stop,
            RUN_PAUSE_PATH: self._handle_run_pause,
            RUN_RESUME_PATH: self._handle_run_resume,
            AGENT_LAUNCH_LOGIN_PATH: self._handle_agent_launch_login,
        }
        handler = auth_routes.get(path)
        if handler is None:
            handler = protected_routes.get(path)
            if handler is None:
                self._send_json(404, False, error="unknown endpoint")
                return
            user = self._current_user()
            if user is None:
                self._send_json(401, False, error="authentication required")
                return
            if user.get("orgId") is None:
                self._send_json(403, False, error="account is not attached to an organization")
                return
            # The real RBAC gate: a session is necessary but not sufficient — the
            # role must permit this route's action (finer per-op checks in handlers).
            action = _ROUTE_ACTIONS.get(path)
            if action and not rbac.can(user.get("role"), action):
                self._send_json(403, False,
                                error="your role does not permit this action")
                return
        # A base64 screenshot on the AI-generate / interview routes needs a larger
        # body cap; every other route keeps the tight 64 KB default.
        cap = (GENERATE_MAX_BODY_BYTES
               if path in (CAMPAIGN_GENERATE_PATH, CAMPAIGN_INTERVIEW_PATH)
               else MAX_BODY_BYTES)
        payload, err = self._read_json_body(cap)
        if err is not None:
            self._send_json(400, False, error=err)
            return
        handler(payload)

    # ----- auth: session cookie helpers -----
    def _request_session_token(self) -> Optional[str]:
        raw = self.headers.get("Cookie")
        if not raw:
            return None
        try:
            jar: SimpleCookie = SimpleCookie()
            jar.load(raw)
        except CookieError:
            return None
        morsel = jar.get(SESSION_COOKIE)
        return morsel.value if morsel else None

    def _current_user(self) -> Optional[dict[str, Any]]:
        """The session identity {id, email, orgId, role, org*} or None (anonymous).
        Memoized per request — the gate and the handler share one DB lookup."""
        if getattr(self, "_user_cached", False):
            return self._user
        self._user: Optional[dict[str, Any]] = None
        token = self._request_session_token()
        if token:
            store = Store(self.db_path)
            try:
                self._user = store.get_auth_session_user(token)
            except Exception:  # noqa: BLE001 — a DB hiccup reads as anonymous, never a crash
                self._user = None
            finally:
                store.close()
        # A REAL org session always wins (a signed-in user is themselves). Only when
        # there is none do we fall back to a superadmin's ACTIVE impersonation — so an
        # admin who has not explicitly started impersonating has NO org identity (the
        # org path stays fail-closed; PRD §10).
        if self._user is None:
            self._user = self._impersonated_user()
        self._user_cached = True
        return self._user

    def _impersonated_user(self) -> Optional[dict[str, Any]]:
        """The effective org principal when a superadmin is actively impersonating, else
        None. This is the ONE place an admin acquires an org identity — behind the
        IP-allowlisted admin gate (_current_admin) and only with an effective principal
        set on the admin session (the audited impersonate route). Impersonating a
        specific user adopts THAT user's org + role; impersonating an org grants
        owner-level access to it."""
        admin = self._current_admin()
        if admin is None:
            return None
        eff_user, eff_org = admin["effectiveUserId"], admin["effectiveOrgId"]
        if eff_user is None and eff_org is None:
            return None  # admin present but not impersonating → no org identity
        store = Store(self.db_path)
        try:
            if eff_user is not None:
                u = store.get_user_by_id(eff_user)
                if u is None or u.get("org_id") is None:
                    return None  # stale/dangling target
                if eff_org is not None and u["org_id"] != eff_org:
                    return None  # inconsistent principal — fail closed
                org = store.get_organization(int(u["org_id"]))
                return self._impersonation_principal(
                    admin, user_id=int(u["id"]), email=u["email"],
                    org_id=int(u["org_id"]), role=u.get("role") or "owner", org=org)
            org = store.get_organization(int(eff_org))
            if org is None:
                return None  # impersonating a deleted org → fail closed
            return self._impersonation_principal(
                admin, user_id=None, email=admin["email"], org_id=int(eff_org),
                role="owner", org=org)
        except Exception:  # noqa: BLE001 — a DB hiccup reads as no impersonation
            return None
        finally:
            store.close()

    @staticmethod
    def _impersonation_principal(admin: dict[str, Any], *, user_id: Optional[int],
                                 email: str, org_id: int, role: str,
                                 org: Optional[dict[str, Any]]) -> dict[str, Any]:
        """Shape a synthetic org principal from an impersonation. Matches
        get_auth_session_user's keys so every org handler consumes it unchanged; the
        `_impersonatedByAdminId` breadcrumb lets write handlers audit the real actor."""
        return {"id": user_id, "email": email, "orgId": org_id, "role": role,
                "orgName": (org or {}).get("name"), "orgLogo": (org or {}).get("logo"),
                "orgDescription": (org or {}).get("description"),
                "_impersonatedByAdminId": admin["adminId"]}

    # ----- worker plane: bearer-token gate (v14) -----
    def _request_bearer_token(self) -> Optional[str]:
        """Extract the raw token from `Authorization: Bearer <token>` (worker plane).
        Distinct from the rr_session cookie gate. None if header absent/malformed."""
        raw = self.headers.get("Authorization", "")
        if not raw.startswith("Bearer "):
            return None
        token = raw[len("Bearer "):].strip()
        return token or None

    def _current_worker(self) -> Optional[dict[str, Any]]:
        """Resolve + authenticate the worker behind the bearer token (LOCKED #5).
        The store hashes the token, matches worker_token_hash WHERE revoked_at IS
        NULL AND not expired. Fail closed (None). Memoized per request, SEPARATE
        from _current_user so a worker request never triggers a cookie/session DB
        lookup (and vice versa).

        A store FAILURE is recorded separately from "no such row" (`_worker_auth_failed`):
        both fail closed here, but they must not answer the same way on the wire — see
        _reject_worker_auth (ledger B10)."""
        if getattr(self, "_worker_cached", False):
            return self._worker
        self._worker: Optional[dict[str, Any]] = None
        self._worker_auth_failed = False
        token = self._request_bearer_token()
        if token:
            try:
                store = Store(self.db_path)
            except Exception:  # noqa: BLE001 — DB missing/locked: a SERVER fault
                log.error("worker auth could not open the store", exc_info=True)
                self._worker_auth_failed = True
                self._worker_cached = True
                return None
            try:
                self._worker = store.get_worker_by_token(token)
            except Exception:  # noqa: BLE001 — a DB hiccup reads as unauthenticated
                log.error("worker auth lookup failed", exc_info=True)
                self._worker = None
                self._worker_auth_failed = True
            finally:
                store.close()
        self._worker_cached = True
        return self._worker

    def _reject_worker_auth(self) -> None:
        """Answer a worker-plane call whose bearer did not resolve.

        A store failure (DB locked, mid-restore, not yet mounted) is a SERVER fault, not a
        revocation, so it answers 503 — the box then backs off like any other transient
        error. Only a genuine no-such-row/revoked/expired lookup answers 401, which is the
        one signal the sidecar may eventually read as "this box is revoked" and act on by
        destroying its credential (ledger B10). Conflating the two let a blip on the
        bridge disenrol a whole fleet."""
        if getattr(self, "_worker_auth_failed", False):
            self._send_json(503, False,
                            error="worker authentication is temporarily unavailable")
            return
        self._send_json(401, False, error="invalid or revoked worker token")

    # ----- v15 superadmin plane: separate auth gate (MFA + IP-allowlist) -----
    def _client_ip(self) -> str:
        """The client IP for the IP-allowlist. Defaults to the transport peer
        (client_address) and IGNORES X-Forwarded-For — XFF is client-spoofable, so
        trusting it blindly would let anyone forge an allowlisted IP. When the admin
        plane runs behind a reverse proxy, set AIZU_TRUSTED_PROXIES to the proxy's
        IP/CIDR: only then is XFF honoured, and only the rightmost hop NOT itself a
        trusted proxy is taken as the real client (nginx-realip semantics)."""
        addr = getattr(self, "client_address", None)
        peer = addr[0] if addr else ""
        trusted = os.environ.get(admin_auth.ADMIN_TRUSTED_PROXIES_ENV, "")
        if not trusted:
            return peer  # default: peer is authoritative, XFF ignored
        return admin_auth.effective_client_ip(
            peer, self.headers.get("X-Forwarded-For"), trusted)

    def _request_admin_session_token(self) -> Optional[str]:
        raw = self.headers.get("Cookie")
        if not raw:
            return None
        try:
            jar: SimpleCookie = SimpleCookie()
            jar.load(raw)
        except CookieError:
            return None
        morsel = jar.get(admin_auth.ADMIN_SESSION_COOKIE)
        return morsel.value if morsel else None

    def _current_admin(self) -> Optional[dict[str, Any]]:
        """The authenticated platform admin behind the rr_admin_session cookie, or None.

        FAIL CLOSED on the IP-allowlist FIRST — a stolen admin cookie replayed from an
        off-allowlist host resolves to None even with a valid token. Then resolve the
        session (non-expired, admin not disabled). Memoized per request, SEPARATE from
        _current_user so the two planes never share identity. Returns the admin identity
        plus the impersonation principal (effectiveOrgId/effectiveUserId)."""
        if getattr(self, "_admin_cached", False):
            return self._admin
        self._admin: Optional[dict[str, Any]] = None
        allowlist = os.environ.get(admin_auth.ADMIN_IP_ALLOWLIST_ENV, "")
        if not admin_auth.ip_allowed(self._client_ip(), allowlist):
            self._admin_cached = True
            return None  # off-allowlist: no admin identity, full stop
        token = self._request_admin_session_token()
        if token:
            store = Store(self.db_path)
            try:
                self._admin = store.get_admin_session(token)
            except Exception:  # noqa: BLE001 — a DB hiccup reads as unauthenticated
                self._admin = None
            finally:
                store.close()
        self._admin_cached = True
        return self._admin

    def _set_admin_cookie_header(self, token: str) -> tuple[str, str]:
        # Same Secure rule as the org session cookie (_cookie_flags) — this one gates
        # the superadmin plane, so if anything it matters more.
        return ("Set-Cookie",
                f"{admin_auth.ADMIN_SESSION_COOKIE}={token}; {self._cookie_flags()}; "
                f"Max-Age={admin_auth.ADMIN_SESSION_TTL_SECONDS}")

    def _clear_admin_cookie_header(self) -> tuple[str, str]:
        return ("Set-Cookie",
                f"{admin_auth.ADMIN_SESSION_COOKIE}=; {self._cookie_flags()}; "
                f"Max-Age=0")

    @staticmethod
    def _shape_user(u: dict[str, Any]) -> dict[str, Any]:
        """The user shape the panel consumes (auth.ts authUserSchema). `impersonated` is
        True when this principal is a superadmin's active impersonation (id may be null for
        an org-level impersonation — no specific user) so the org app can banner it + offer
        an exit; a real signed-in user always reports False."""
        return {"id": u.get("id"), "email": u["email"], "role": u.get("role"),
                "orgId": u.get("orgId"),
                "impersonated": u.get("_impersonatedByAdminId") is not None,
                "org": {"id": u.get("orgId"), "name": u.get("orgName"),
                        "logo": u.get("orgLogo"), "description": u.get("orgDescription")}}

    @staticmethod
    def _campaign_in_org(store: Store, campaign_id: str, org_id: Optional[int]) -> bool:
        """The campaign-ownership gate for every campaign-scoped write/read. Delegates to
        the single repository-level composite tenant filter (store.campaign_in_org) so the
        WHERE org_id=:effective_org rule lives in ONE place handlers can't forget (PRD §10
        BOLA rule); `org_id` is the EFFECTIVE org (impersonation passes the foreign org)."""
        return store.campaign_in_org(campaign_id, org_id)

    @staticmethod
    def _resolve_org_lead(store: Store, org_id: Optional[int], campaign_id: str,
                          platform: str, lead_key: str) -> Optional[str]:
        """One org-facing lead key → the REAL `comment_id`, or None (v28).

        Every org-scoped lead write goes through here, and it accepts the opaque
        `lead_token` and NOTHING else. That strictness is the feature: if a raw
        `comment_id` still worked, the whole change would be decorative — a caller who
        already knows the real id (from a pre-v28 bookmark, an old export, or by
        guessing a permalink) would keep writing with it, and any code path that kept
        accepting one would be a standing invitation to keep SHIPPING one.

        Three checks, and the order matters:
          1. the token resolves at all (unique table-wide, so no campaign needed);
          2. the row belongs to the caller's org — done inside `resolve_lead_token`,
             which is why the campaign is not passed to it: pairing a caller-supplied
             campaign with someone else's token is exactly the probe to refuse;
          3. the row's OWN campaign and platform match what the request claimed, so a
             token cannot be used to write across the caller's own campaigns under a
             mismatched key.

        Returns None for every failure, which every caller renders as the same 404 as
        an unknown lead. Never distinguish them: a 403 or a distinct message would
        confirm the row exists and turn a write endpoint into an existence oracle,
        which is the property `_handle_lead_reveal` documents at length.
        """
        resolved = store.resolve_lead_token(org_id, lead_key)
        if resolved is None:
            return None
        if resolved["campaignId"] != campaign_id or resolved["platform"] != platform:
            return None
        return str(resolved["commentId"])

    def _request_is_https(self) -> bool:
        """Whether THIS request genuinely arrived over TLS.

        Two ways it can: the socket is itself wrapped in TLS, or a reverse proxy
        terminated TLS and said so in X-Forwarded-Proto. That header is
        client-spoofable, so — exactly like `_client_ip` does for the IP allowlist —
        it is honoured only when the peer is a configured trusted proxy. Unset
        AIZU_TRUSTED_PROXIES ⇒ the header is ignored outright.
        """
        if isinstance(getattr(self, "connection", None), ssl.SSLSocket):
            return True
        trusted = os.environ.get(admin_auth.ADMIN_TRUSTED_PROXIES_ENV, "")
        if not trusted:
            return False
        addr = getattr(self, "client_address", None)
        peer = addr[0] if addr else ""
        if not admin_auth.ip_allowed(peer, trusted):   # peer is not a trusted proxy
            return False
        forwarded = (self.headers.get("X-Forwarded-Proto") or "").split(",")[0]
        return forwarded.strip().lower() == "https"

    def _cookie_flags(self) -> str:
        # HttpOnly: JS can't read it (XSS can't exfiltrate the session). SameSite=Lax:
        # not sent on cross-site requests (CSRF defence). Secure: added only when the
        # request really came in over TLS. It cannot be unconditional — browsers drop
        # a Secure cookie set over plain http, which would break the local-first
        # loopback deployment entirely — and it cannot be omitted either, since the
        # documented hosted deployment (AIZU_ALLOWED_ORIGINS=https://aizu.uz behind a
        # reverse proxy) would otherwise ship a 30-day credential that any downgrade
        # to http replays in cleartext.
        return "HttpOnly; SameSite=Lax; Path=/" + ("; Secure" if self._request_is_https()
                                                   else "")

    def _set_cookie_header(self, token: str) -> tuple[str, str]:
        return ("Set-Cookie",
                f"{SESSION_COOKIE}={token}; {self._cookie_flags()}; "
                f"Max-Age={SESSION_TTL_SECONDS}")

    def _clear_cookie_header(self) -> tuple[str, str]:
        return ("Set-Cookie", f"{SESSION_COOKIE}=; {self._cookie_flags()}; Max-Age=0")

    # ----- auth: handlers -----
    def _handle_signup(self, payload: Any) -> None:
        fields, err = _validate_signup(payload)
        if err is not None:
            self._send_json(400, False, error=err)
            return
        token = new_session_token()
        store = Store(self.db_path)
        try:
            try:
                if fields["inviteToken"]:
                    # Join an existing org with the invited role (company fields ignored).
                    info = store.get_invite(fields["inviteToken"])
                    if not info or not info["valid"]:
                        self._send_json(400, False, error="invalid or expired invite")
                        return
                    accepted = store.accept_invite(
                        token=fields["inviteToken"], email=fields["email"],
                        password_hash=hash_password(fields["password"]),
                        session_token=token, expires_at=session_expiry())
                    store.record_audit(
                        accepted["orgId"], accepted["userId"], "invite_accepted",
                        detail=json.dumps({"role": accepted["role"]}))
                else:
                    # Create a brand-new company; the signer becomes its owner.
                    store.create_org_with_owner(
                        email=fields["email"], password_hash=hash_password(fields["password"]),
                        token=token, expires_at=session_expiry(),
                        company_name=fields["companyName"], logo=fields["companyLogo"],
                        description=fields["companyDescription"])
            except sqlite3.IntegrityError:
                self._send_json(409, False,
                                error="an account with that email already exists")
                return
            except ValueError as e:  # invalid/expired invite or blank company name
                self._send_json(400, False, error=str(e))
                return
            user = store.get_auth_session_user(token)
        except Exception:  # noqa: BLE001 — don't leak internal/DB detail on auth surfaces
            self._send_json(500, False, error="internal server error")
            return
        finally:
            store.close()
        self._send_json(200, True, data={"user": self._shape_user(user)},
                        extra_headers=[self._set_cookie_header(token)])

    def _handle_login(self, payload: Any) -> None:
        fields, err = _validate_credentials(payload, enforce_policy=False)
        if err is not None:
            self._send_json(400, False, error=err)
            return
        throttle = self.login_throttle
        if throttle is not None and throttle.is_locked(fields["email"]):
            log.warning("Login throttled · %s", fields["email"])
            self._send_json(429, False,
                            error="too many failed attempts — try again later")
            return
        store = Store(self.db_path)
        try:
            user = store.get_user_by_email(fields["email"])
            # Always run a verify (real hash, or the dummy) so timing doesn't
            # disclose whether the email exists.
            stored_hash = user["password_hash"] if user else _DUMMY_PASSWORD_HASH
            valid = verify_password(fields["password"], stored_hash) and user is not None
            if not valid:
                if throttle is not None:
                    throttle.record_failure(fields["email"])
                log.warning("Login failed · %s", fields["email"])
                self._send_json(401, False, error="invalid email or password")
                return
            if throttle is not None:
                throttle.reset(fields["email"])
            log.success("Login ok · %s", fields["email"])
            token = new_session_token()
            store.create_auth_session(token, int(user["id"]), session_expiry())
            # Opportunistic housekeeping — keep the session table from accreting
            # dead rows over time (cheap; login is the natural sweep point).
            store.purge_expired_auth_sessions()
            shaped = self._shape_user(store.get_auth_session_user(token))
        except Exception:  # noqa: BLE001 — don't leak internal/DB detail on auth surfaces
            self._send_json(500, False, error="internal server error")
            return
        finally:
            store.close()
        self._send_json(200, True, data={"user": shaped},
                        extra_headers=[self._set_cookie_header(token)])

    def _handle_logout(self) -> None:
        token = self._request_session_token()
        if token:
            store = Store(self.db_path)
            try:
                store.delete_auth_session(token)
            except Exception:  # noqa: BLE001 — clearing the cookie still logs the client out
                pass
            finally:
                store.close()
        self._send_json(200, True, data={"loggedOut": True},
                        extra_headers=[self._clear_cookie_header()])

    def _handle_me(self) -> None:
        user = self._current_user()
        if user is None:
            self._send_json(401, False, error="not authenticated")
            return
        self._send_json(200, True, data={"user": self._shape_user(user)})

    def _handle_invite_lookup(self, query: dict[str, list[str]]) -> None:
        """Public invite-landing lookup: branding + intended role for a raw token."""
        token = (query.get("token") or [None])[0]
        if not token or not token.strip():
            self._send_json(400, False, error="missing invite token")
            return
        store = Store(self.db_path)
        try:
            info = store.get_invite(token.strip())
        except Exception:  # noqa: BLE001
            info = None
        finally:
            store.close()
        if not info or not info["valid"]:
            self._send_json(404, False, error="invalid or expired invite")
            return
        self._send_json(200, True, data={
            "orgName": info["orgName"], "orgLogo": info["orgLogo"],
            "email": info["email"], "role": info["role"], "valid": True})

    def _handle_status(self, payload: Any) -> None:
        fields, err = _validate_status_request(payload)
        if err is not None:
            self._send_json(400, False, error=err)
            return
        user = self._current_user()  # non-None: route is behind the auth gate
        store = Store(self.db_path)
        try:
            if not self._campaign_in_org(store, fields["campaignId"], user["orgId"]):
                self._send_json(404, False, error="unknown campaign")  # cross-org → hide
                return
            # v28: the body carries the OPAQUE lead key, never the platform's own
            # comment id — see `_resolve_org_lead`. An unresolvable key is the same
            # 404 as a lead that does not exist, deliberately.
            comment_id = self._resolve_org_lead(
                store, user["orgId"], fields["campaignId"], fields["platform"],
                fields["commentId"])
            if comment_id is None:
                self._send_json(404, False, error="unknown lead")
                return
            updated = store.set_status(fields["campaignId"], comment_id,
                                       fields["status"], platform=fields["platform"],
                                       user=user, reason=fields["note"])
        except ValueError as e:  # invalid status or missing forced reason → client error
            self._send_json(400, False, error=str(e))
            return
        except Exception:  # noqa: BLE001 — generic 500; detail to the log only
            self._send_internal_error("lead status write")
            return
        finally:
            store.close()
        if not updated:
            # The key the CALLER sent, not the resolved comment id: echoing the real
            # one back would hand over the very field v28 stopped shipping.
            self._send_json(404, False,
                            error=f"no match for lead {fields['commentId']!r}")
            return
        self._send_json(200, True,
                        data={"commentId": fields["commentId"], "status": fields["status"]})

    def _handle_status_bulk(self, payload: Any) -> None:
        fields, err = _validate_bulk_status(payload)
        if err is not None:
            self._send_json(400, False, error=err)
            return
        user = self._current_user()  # non-None: route is behind the auth gate
        store = Store(self.db_path)
        try:
            if not self._campaign_in_org(store, fields["campaignId"], user["orgId"]):
                self._send_json(404, False, error="unknown campaign")  # cross-org → hide
                return
            updated, missing = 0, []
            for item in fields["items"]:
                # v28: resolve each opaque key on its own. An item that does not
                # resolve joins `missing` rather than aborting the batch — same shape
                # as a lead that was deleted between the page load and the write, and
                # `missing` echoes the key the CALLER sent, never the real comment id.
                comment_id = self._resolve_org_lead(
                    store, user["orgId"], fields["campaignId"], item["platform"],
                    item["commentId"])
                if comment_id is None:
                    missing.append(item["commentId"])
                    continue
                # One shared reason applies to every item's audit row.
                if store.set_status(fields["campaignId"], comment_id,
                                    fields["status"], platform=item["platform"],
                                    user=user, reason=fields["note"]):
                    updated += 1
                else:
                    missing.append(item["commentId"])
        except ValueError as e:  # forced-reason pre-validated; a bad status aborts the batch
            self._send_json(400, False, error=str(e))
            return
        except Exception:  # noqa: BLE001
            self._send_internal_error("bulk lead status write")
            return
        finally:
            store.close()
        # Partial misses are not an error — the caller learns which ids missed.
        self._send_json(200, True, data={"updated": updated, "missing": missing,
                                         "status": fields["status"]})

    def _handle_lead_note(self, payload: Any) -> None:
        fields, err = _validate_lead_note(payload)
        if err is not None:
            self._send_json(400, False, error=err)
            return
        user = self._current_user()  # non-None: route is behind the auth gate
        store = Store(self.db_path)
        try:
            if fields["op"] == "create":
                if not self._campaign_in_org(store, fields["campaignId"], user["orgId"]):
                    self._send_json(404, False, error="unknown campaign")
                    return
                # v28: opaque key in, real comment id resolved here (see
                # `_resolve_org_lead`). Without this a note would be the one write
                # that still accepted a permalink-bearing id from a customer.
                comment_id = self._resolve_org_lead(
                    store, user["orgId"], fields["campaignId"], fields["platform"],
                    fields["commentId"])
                if comment_id is None:
                    self._send_json(404, False, error="unknown lead")
                    return
                note = store.add_note(fields["campaignId"], comment_id,
                                      fields["body"], author=user,
                                      platform=fields["platform"])
                # `add_note` echoes back the comment id it was GIVEN — which is the
                # real one, because we just resolved it. Re-mask it before it leaves:
                # the note response is org-facing, and shipping the resolved id here
                # would hand back through the write path exactly the value v28 stopped
                # shipping on the read path. Overwrite rather than rebuild, so a future
                # column added to a note record still reaches the panel.
                note = {**note, "commentId": fields["commentId"]}
                self._send_json(200, True, data=note)
                return
            # delete is gated by authorship (delete_note); a note's author can only
            # have authored notes on their own org's campaigns, so this is org-safe.
            result = store.delete_note(fields["noteId"], (user or {}).get("id"))
        except ValueError as e:  # empty/oversized body → client error
            self._send_json(400, False, error=str(e))
            return
        except Exception:  # noqa: BLE001
            self._send_internal_error("lead note")
            return
        finally:
            store.close()
        if result == "not_found":
            self._send_json(404, False, error=f"no note with id {fields['noteId']}")
            return
        if result == "forbidden":
            self._send_json(403, False, error="only the note's author may delete it")
            return
        self._send_json(200, True, data={"noteId": fields["noteId"], "op": "delete"})

    def _handle_lead_reveal(self, payload: Any) -> None:
        """v27 reveal-on-demand: un-redact ONE lead's identity, and audit the attempt.

        Leads are anonymized by default (`panel._build_matches` drops `username` and
        the comment `text`), which also removed the customer's only way to REACH a
        lead. This is the sanctioned way back: an explicit, per-lead, audited
        disclosure instead of a payload that leaks by default.

        Four rules, each of which the naive implementation gets wrong:

        * The org comes from the SESSION, never the body. The body names a lead;
          it never names whose lead it is (BOLA).
        * A lead that is not this org's — or does not exist at all — is a **404**,
          not a 403. A 403 would confirm the row exists, turning the endpoint into a
          cross-tenant existence oracle for any (campaign, comment) pair someone can
          guess. Both cases answer with the same message for the same reason.
        * The `reveal_lead` role check runs HERE rather than in `_ROUTE_ACTIONS`, so
          a refusal still writes its audit row (see the comment there).
        * Reveal is a READ. It writes no status, no history row, no `updated_at` —
          the only thing it writes is the audit row.

        Returns `{username}` and NOTHING else about the person. The comment body and
        `reelId` are BOTH superadmin-only (`panel._build_matches(include_identity=True)`,
        `GET /api/admin/orgs/{id}/leads`) and neither has an org-facing route at all —
        not here, not on any list payload.

        The earlier build shipped `text` and `reelId` from this handler, reasoning that
        a handle already unlocks the public post, so withholding the words was
        incoherent. That reasoning is retired, and deliberately: the product promise is
        that an org learns WHAT a lead wants (`intent`, derived at capture) and WHO to
        contact (this handle), never the words the person wrote. `reelId` goes with the
        text rather than with the handle because it is a POINTER to the text — a post
        link is the comment one hand-built URL away, so shipping it would re-open by
        redirection exactly what dropping `text` closes.

        FIFTH rule, and the one that makes the other four hold at scale: the reveal is
        CAPPED by the plan's period lead allowance (free 10 … scale = override). An
        uncapped per-lead endpoint is a bulk export with extra round-trips — a script
        walks the anonymized list and reveals every row. The meter counts DISTINCT
        leads, never calls, and a re-reveal of a lead already revealed this period is
        always free; see `Store.count_reveals_this_period`.
        """
        fields, err = _validate_lead_reveal(payload)
        if err is not None:
            self._send_json(400, False, error=err)
            return
        user = self._current_user()  # non-None: route is behind the auth gate
        org_id = user["orgId"]
        store = Store(self.db_path)
        # v28: the body names the lead by its OPAQUE key. Resolve to the real comment
        # id FIRST, because everything below is keyed on it — including `uid`, the
        # audit target that doubles as the reveal meter.
        #
        # `uid` stays built from the REAL comment id on purpose, even though the
        # customer never sees one. It is a server-side identity, and the period cap
        # counts DISTINCT targets: re-deriving it from the token would orphan every
        # audit row written before v28 and silently hand every org a fresh allowance
        # the moment this shipped. The meter has to keep pointing at the same lead.
        comment_id = self._resolve_org_lead(
            store, org_id, fields["campaignId"], fields["platform"],
            fields["commentId"])
        uid = (lead_uid(fields["campaignId"], fields["platform"], comment_id)
               if comment_id is not None
               # Unresolvable: still audit the ATTEMPT (a denial is audited before the
               # lead is even looked up), keyed by the token the caller offered. It
               # resolves to no lead, so it can never collide with a real target.
               else lead_uid(fields["campaignId"], fields["platform"],
                             fields["commentId"]))

        def audit(result: str) -> None:
            """One row per call, whatever the outcome. `result` is the only thing that
            differs between them; `record_audit` swallows its own failures by contract,
            so this can never take the reveal down.

            These rows are ALSO the meter (`Store.count_reveals_this_period`), which is
            why the detail blob is built by `Store.reveal_audit_detail` rather than
            inline: the writer and the reader's LIKE have to be one definition.
            """
            store.record_audit(
                org_id, user["id"], REVEAL_ACTION, target=uid,
                detail=Store.reveal_audit_detail(fields["campaignId"],
                                                 fields["platform"], result))

        try:
            # Denial is audited FIRST, before the row is even looked up: what we are
            # recording is that this actor asked, and the answer must not depend on
            # whether the lead happens to exist.
            if not rbac.can(user.get("role"), "reveal_lead"):
                audit("denied")
                self._send_json(403, False,
                                error="your role does not permit this action")
                return
            if not self._campaign_in_org(store, fields["campaignId"], org_id):
                audit("not_found")
                self._send_json(404, False, error="unknown lead")  # cross-org → hide
                return
            # v28: an opaque key that resolves to nothing is indistinguishable from a
            # lead that is not ours — same audit result, same 404, same message.
            if comment_id is None:
                audit("not_found")
                self._send_json(404, False, error="unknown lead")
                return
            match = None
            for m in store.matches(fields["campaignId"]):
                if (m.get("comment_id") == comment_id
                        and (m.get("platform") or DEFAULT_PLATFORM) == fields["platform"]):
                    match = m
                    break
            if match is None:
                audit("not_found")
                self._send_json(404, False, error="unknown lead")
                return
            # v27 plan cap. LAST of the four gates on purpose: a 402 therefore always
            # means "this really is your lead, the allowance is spent", and the two
            # earlier refusals stay indistinguishable from each other, so an org that
            # is over its cap still cannot use the endpoint as an existence oracle.
            sub = store.get_subscription(org_id)
            since = store.period_since(org_id)
            # A lead already revealed this period is FREE, and checking that first is
            # what makes the cap survive contact with the product: revealed data is
            # never cached client-side (Section F), so reopening a drawer re-reveals.
            # Metering CALLS would spend a Free org's ten-lead allowance on one lead
            # opened ten times. The meter counts DISTINCT leads for the same reason.
            if not store.lead_revealed_this_period(org_id, uid, since):
                cap = int(sub["lead_cap"] or 0)
                if store.count_reveals_this_period(org_id, since) >= cap:
                    # Audited like every other outcome — a refusal at the cap is
                    # precisely the row that explains a support ticket, and the row a
                    # scripted enumeration attempt writes over and over.
                    audit("capped")
                    self._send_json(402, False,
                                    error=_billing_reveal_cap_message(sub))
                    return
            audit("revealed")
        except Exception:  # noqa: BLE001 — generic 500; detail to the log only
            self._send_internal_error("lead reveal")
            return
        finally:
            store.close()
        # CONSTRUCT the response from named fields — never `dict(match)` minus keys.
        # An inverted filter would ship every future `matches` column (source, tier,
        # found_by_models, …) to the customer the day it is added.
        # NOTE: `text` and `reelId` are absent BY CONTRACT, not by oversight — see the
        # docstring. Re-adding either key here is the single edit that undoes the whole
        # policy, which is why `tests/test_lead_reveal.py` pins the key set exactly.
        # `id` and `commentId` are the OPAQUE key the caller sent, echoed back so the
        # drawer can match the answer to the lead it asked about. NOT `uid`, which is
        # built from the real comment id: it is the audit/meter key and stays server-
        # side. Echoing it here would ship the permalink-bearing id on the one
        # endpoint whose entire job is to disclose less than it used to.
        self._send_json(200, True, data={
            "id": lead_uid(fields["campaignId"], fields["platform"],
                           fields["commentId"]),
            "commentId": fields["commentId"],
            "platform": fields["platform"],
            "username": match.get("username") or "",
        })

    def _resolve_campaign_target(
            self, store: Store, org_id: Optional[int], requested: str,
            op: Optional[str], *,
            has_brief: bool) -> tuple[Optional[str], int, Optional[str]]:
        """Which campaign row this write targets → (campaign_id, status, error).

        Returns (None, status, message) when the request must be refused. The rule,
        in order, and every clause of it is load-bearing:

        * The caller's OWN row is edited in place — that is the edit path, whether or
          not `op` says so. An explicit ``op="create"`` aimed at it is refused 409
          instead of silently overwriting its brief (and, because `matches` is keyed
          on campaign_id, re-pointing its whole lead history at what the operator
          believes is a brand-new campaign).
        * An EDIT of anything else — explicit ``op="edit"``, or a legacy payload with
          no brief, which cannot stand a campaign up on its own — is a 404 when the
          row belongs to another org (undisclosed) or names the reserved per-org
          namespace. An UNREGISTERED id stays editable: the file-backed campaign from
          `config/campaign.md` has no campaign_meta row until its first write, and the
          legacy path has always let that through (the write stamps it to the caller's
          org). Refusing it here would have made `op="edit"` reject the very campaign
          the no-`op` request one line down accepts.
        * A CREATE — explicit, or a legacy brief-carrying payload — ALWAYS allocates a
          key in the caller's own namespace (`_org_scoped_campaign_id`), even when the
          bare id is globally free. Never reusing the bare id is what removes the
          cross-tenant existence oracle: the key handed back is a pure function of the
          caller's own org and the requested slug, so it cannot tell the caller whether
          somebody else already holds that name. It is also what makes two tenants able
          to hold 'Q4 Outbound' at once, and what makes the 409 below reachable for the
          legacy client (a second create of the same slug re-derives the same scoped
          key and collides with the caller's own first campaign).
        """
        existing_org = store.org_for_campaign(requested)
        mine = existing_org is not None and existing_org == org_id
        if mine:
            if op == "create":
                return None, 409, CAMPAIGN_EXISTS_MESSAGE
            return requested, 200, None
        # Not the caller's row. Registered to someone else, or sitting in the reserved
        # `o<org>.` namespace the caller does not own ⇒ never writable through here.
        unwritable = existing_org is not None or _is_org_scoped_campaign_id(requested)
        if op == "edit" or (op != "create" and not has_brief):
            if unwritable:
                return None, 404, "unknown campaign"
            return requested, 200, None      # unregistered → stamped to the caller
        scoped = _org_scoped_campaign_id(org_id, requested)
        if store.org_for_campaign(scoped) is not None:
            return None, 409, CAMPAIGN_EXISTS_MESSAGE
        return scoped, 200, None

    def _handle_campaign(self, payload: Any) -> None:
        fields, err = _validate_campaign(payload)
        if err is not None:
            self._send_json(400, False, error=err)
            return
        user = self._current_user()
        org_id = user["orgId"]
        store = Store(self.db_path)
        try:
            campaign_id, err_code, err_msg = self._resolve_campaign_target(
                store, org_id, fields["campaignId"], fields["op"],
                has_brief=bool(fields["brief"]))
            if campaign_id is None:
                self._send_json(err_code, False, error=err_msg,
                                error_code="campaign_exists" if err_code == 409 else None)
                return
            fields = {**fields, "campaignId": campaign_id}
            # v27 plan limits: the campaign allowance is a CREATE-time gate. Checked
            # after the target id is resolved (so we know this really is a new row) and
            # before the first write, since upsert_campaign_meta commits in its own
            # transaction. `Store.get_subscription` is the SAME single choke point the
            # run gate uses, so the panel's meter and enforcement can never disagree.
            #
            # The predicate MIRRORS _resolve_campaign_target's own notion of a create:
            # an explicit `op="create"`, or the legacy brief-carrying payload with no
            # `op` (which also allocates a fresh org-scoped key). Gating on the literal
            # `op == "create"` alone would leave that legacy shape as a free bypass;
            # gating on "the row does not exist yet" would over-reach and block a
            # legacy status-only write against an unregistered id. An EDIT — explicit,
            # or inferred — is never blocked, whatever the plan.
            if fields["op"] == "create" or (fields["op"] is None and bool(fields["brief"])):
                sub = store.get_subscription(org_id)
                cap = billing.tier_campaign_cap(sub["tier"])
                # `cap is not None` — None means UNLIMITED. A falsy check would read
                # unlimited as zero and block every create on the paid tiers.
                if cap is not None:
                    # Archived campaigns do not count: the cap bounds the WORKING set,
                    # so an org at its limit can archive its way forward instead of
                    # being wedged with no self-serve move but an upgrade.
                    used = sum(1 for r in store.list_campaign_meta(org_id)
                               if r.get("archived_at") is None)
                    if used >= cap:
                        self._send_json(402, False, error=_billing_campaign_cap_message(
                            cap, sub["tier"]))
                        return
            # VALIDATE EVERYTHING BEFORE THE FIRST WRITE. upsert_campaign_meta
            # commits in its own transaction, so building the merged brief after it
            # meant a brief that failed campaign_from_brief (e.g. an unsupported
            # platform) still left a committed, brief-less campaign row behind — a
            # full card in the panel that can never run ("no platforms") and that
            # the very same request answered 400 for. The merge base is read-only,
            # so hoisting the whole check above the writes costs nothing.
            brief = fields["brief"]
            merged: Optional[dict[str, Any]] = None
            if brief:
                # MERGE over the campaign's current brief, never replace: the panel
                # form only carries the editable fields (defs/seeds/platform/threshold/
                # prompts), not escalate_band / seed_direction / engagement knobs.
                # The shallow `{**base, **brief}` is exactly the C3 `channels` merge
                # sentinel: `channels` absent from `brief` (not emitted by
                # _brief_to_snake) keeps the stored channels (no-change); `channels: []`
                # overwrites to empty (clear to single-platform); a list replaces atomically.
                merged = {**_campaign_merge_base(store, self.config_dir,
                                                 fields["campaignId"]), **brief}
                # Buildable? campaign_from_brief raises ValueError on a bad shape.
                campaign_from_brief(fields["campaignId"], merged)
            # v12: a live<->paused change carries paused_reason semantics — route it
            # through set_campaign_paused so an operator 'user' resume cannot clear a
            # system 'auto' halt. Other status values (draft/ended) and field-only
            # edits flow straight through the upsert.
            incoming_status = fields["status"]
            is_pause_toggle = incoming_status in ("live", "paused")
            meta = store.upsert_campaign_meta(
                fields["campaignId"], org_id=org_id, display_name=fields["displayName"],
                status=None if is_pause_toggle else incoming_status,
                budget_cap=fields["budgetCap"], goal_target=fields["goalTarget"])
            if is_pause_toggle:
                meta = store.set_campaign_paused(
                    fields["campaignId"], paused=(incoming_status == "paused"),
                    reason="user") or meta
            if merged is not None:
                store.upsert_campaign_brief(fields["campaignId"], merged, org_id=org_id)
                meta = {**meta, "hasBrief": True}
        except ValueError as e:  # bad brief shape → client error, not 500
            self._send_json(400, False, error=str(e))
            return
        except Exception:  # noqa: BLE001 — never echo an internal/driver message
            log.error("campaign upsert failed · campaign=%s org=%s",
                      fields["campaignId"], org_id, exc_info=True)
            self._send_json(500, False, error="internal server error")
            return
        finally:
            store.close()
        self._send_json(200, True, data=meta)

    def _handle_campaign_archive(self, payload: Any) -> None:
        """v12: archive or un-archive a campaign. Archive-while-live first stops the
        campaign's active run (if any) and transitions status live->paused atomically,
        so the (archived, live) contradiction is never reachable and archived
        campaigns are runnable by no path."""
        fields, err = _validate_campaign_archive(payload)
        if err is not None:
            self._send_json(400, False, error=err)
            return
        user = self._current_user()
        org_id = user["orgId"]
        store = Store(self.db_path)
        try:
            if not self._campaign_in_org(store, fields["campaignId"], org_id):
                self._send_json(404, False, error="unknown campaign")
                return
            new_status = None
            if fields["archived"]:
                meta = store.get_campaign_meta(fields["campaignId"])
                # Archiving a live campaign also stops its in-flight run and parks it
                # at 'paused' — you can't archive something that's actively engaging.
                if meta and meta.get("status") == "live":
                    new_status = "paused"
                    if self.run_manager is not None:
                        self.run_manager.stop_campaign(fields["campaignId"], org_id)
            updated = store.set_campaign_archived(
                fields["campaignId"], fields["archived"], new_status=new_status)
            if updated is None:
                self._send_json(404, False, error="unknown campaign")
                return
            store.record_audit(
                org_id, user["id"],
                "campaign_archived" if fields["archived"] else "campaign_unarchived",
                detail=json.dumps({"campaignId": fields["campaignId"]}))
        except Exception:  # noqa: BLE001
            self._send_internal_error("campaign archive")
            return
        finally:
            store.close()
        self._send_json(200, True,
                        data={"campaignId": fields["campaignId"],
                              "archived": fields["archived"]})

    def _handle_campaign_schedule(self, payload: Any) -> None:
        """v12: arm/replace (enabled) or clear (disabled) a campaign's recurring
        schedule. The server is authoritative for next_run_at — it is computed here
        from the fixed cadence, never trusted from the client."""
        fields, err = _validate_campaign_schedule(payload)
        if err is not None:
            self._send_json(400, False, error=err)
            return
        user = self._current_user()
        org_id = user["orgId"]
        store = Store(self.db_path)
        try:
            if not self._campaign_in_org(store, fields["campaignId"], org_id):
                self._send_json(404, False, error="unknown campaign")
                return
            if not fields["enabled"]:
                updated = store.clear_campaign_schedule(fields["campaignId"])
            else:
                nxt = next_fire(fields["kind"], fields["hour"], fields["minute"],
                                dow=fields["dow"], after_ts=time.time())
                updated = store.set_campaign_schedule(
                    fields["campaignId"], kind=fields["kind"], hour=fields["hour"],
                    minute=fields["minute"], dow=fields["dow"], tz=fields["tz"],
                    next_run_at=nxt, target_leads=fields["targetLeads"],
                    duration_minutes=fields["durationMinutes"])
            if updated is None:
                self._send_json(404, False, error="unknown campaign")
                return
            store.record_audit(org_id, user["id"], "campaign_scheduled",
                               detail=json.dumps({"campaignId": fields["campaignId"],
                                                  "enabled": fields["enabled"]}))
        except ValueError as e:  # bad cadence → client error
            self._send_json(400, False, error=str(e))
            return
        except Exception:  # noqa: BLE001
            self._send_internal_error("campaign schedule")
            return
        finally:
            store.close()
        self._send_json(200, True,
                        data={"campaignId": fields["campaignId"],
                              "scheduleEnabled": bool(updated.get("schedule_enabled")),
                              "nextRunAt": updated.get("next_run_at")})

    def _handle_generate_campaign(self, payload: Any) -> None:
        """AI-draft a campaign from a product url/screenshot/description. Returns the
        flat draft for the panel to pre-fill its create form — it persists NOTHING
        (the user reviews/edits, then saves through POST /api/campaign as usual)."""
        fields, err = _validate_generate(payload)
        if err is not None:
            self._send_json(400, False, error=err)
            return
        if not os.environ.get("OPENROUTER_API_KEY") \
                and not os.environ.get("AIZU_LLM_BASE_URL", "").strip():
            self._send_json(503, False, error="AI generation is not configured "
                            "(set OPENROUTER_API_KEY or AIZU_LLM_BASE_URL).")
            return
        store = Store(self.db_path)
        try:
            router = build_router(store=store, spend_cap_usd=GENERATE_SPEND_CAP_USD)
            # Campaign Lab (Remedy Sheet #1/D): tell the generator what this org's
            # past runs actually proved about seed terms, so it stops re-inventing
            # tags that have already been shown to be dead. Best-effort — a draft
            # must never fail because the ledger could not be read.
            try:
                org_id = self._current_user()["orgId"]
                plat = (fields["platforms"] or [None])[0]
                # Split by kind: a proven HANDLE and a proven HASHTAG are
                # different evidence, and merging them invites the model to
                # propose one where the other belongs.
                seed_history = {
                    kind: store.seed_history(org_id, platform=plat, kind=kind)
                    for kind in ("hashtag", "account")
                }
            except Exception:  # noqa: BLE001 — the ledger is advice, not a gate
                log.debug("seed_history unavailable for generation", exc_info=True)
                seed_history = None
            draft = campaign_gen.generate_campaign(
                url=fields["url"], image_b64=fields["imageB64"], text=fields["text"],
                router=router, config_dir=self.config_dir,
                campaign_id_hint=fields["campaignIdHint"],
                product_context=fields["productContext"],
                interview=fields["interview"], platforms=fields["platforms"],
                seed_history=seed_history)
        except campaign_gen.CampaignGenError as e:  # expected, user-facing
            self._send_json(422, False, error=e.public)
            return
        except Exception as e:  # noqa: BLE001 — never leak internals to the panel
            log.error("campaign generation failed: %s", e)
            self._send_json(500, False, error="generation failed")
            return
        finally:
            store.close()
        self._send_json(200, True, data=draft)

    def _handle_campaign_interview(self, payload: Any) -> None:
        """Run one round of the conversational campaign interview. Returns the next
        questions (or done=true) plus the serialized productContext the panel echoes
        back next round. Persists NOTHING — the brief is synthesized later via
        /api/campaign/generate, then saved through /api/campaign as usual."""
        fields, err = _validate_interview(payload)
        if err is not None:
            self._send_json(400, False, error=err)
            return
        if not os.environ.get("OPENROUTER_API_KEY") \
                and not os.environ.get("AIZU_LLM_BASE_URL", "").strip():
            self._send_json(503, False, error="AI generation is not configured "
                            "(set OPENROUTER_API_KEY or AIZU_LLM_BASE_URL).")
            return
        store = Store(self.db_path)
        try:
            router = build_router(store=store, spend_cap_usd=GENERATE_SPEND_CAP_USD)
            result = campaign_gen.run_interview(
                url=fields["url"], image_b64=fields["imageB64"], text=fields["text"],
                product_context=fields["productContext"],
                interview=fields["interview"], round=fields["round"], router=router)
        except campaign_gen.CampaignGenError as e:  # expected, user-facing
            self._send_json(422, False, error=e.public)
            return
        except Exception as e:  # noqa: BLE001 — never leak internals to the panel
            log.error("campaign interview failed: %s", e)
            self._send_json(500, False, error="interview failed")
            return
        finally:
            store.close()
        self._send_json(200, True, data={
            "done": result.done, "questions": result.questions,
            "productContext": result.product_context, "round": fields["round"]})

    def _handle_team(self, payload: Any) -> None:
        fields, err = _validate_team(payload)
        if err is not None:
            self._send_json(400, False, error=err)
            return
        actor = self._current_user()
        org_id = actor["orgId"]
        actor_role = actor.get("role")
        store = Store(self.db_path)
        try:
            op = fields["op"]
            if op == "create":  # direct-add a teammate with a password set by the inviter
                if not rbac.can_assign_role(actor_role, fields["role"]):
                    self._send_json(403, False, error="you cannot assign that role")
                    return
                try:
                    new_id = store.create_user_in_org(
                        org_id=org_id, email=fields["email"],
                        password_hash=hash_password(fields["password"]), role=fields["role"])
                except sqlite3.IntegrityError:
                    self._send_json(409, False,
                                    error="an account with that email already exists")
                    return
                store.record_audit(org_id, actor["id"], "member_added",
                                   target=str(new_id),
                                   detail=json.dumps({"role": fields["role"]}))
                self._send_json(200, True, data={"id": str(new_id), "email": fields["email"],
                                                 "role": fields["role"]})
                return
            # updateRole / remove — the target must be in the actor's org, and the
            # actor must be allowed to manage that target's CURRENT role (owner-only
            # for admins). Last-owner protection guards against orphaning the org.
            target = store.get_org_user(org_id, fields["userId"])
            if target is None:
                self._send_json(404, False, error="no such teammate")
                return
            if target["id"] == actor["id"] and op == "remove":
                self._send_json(400, False, error="you cannot remove yourself")
                return
            if not rbac.can_manage_target(actor_role, target["role"]):
                self._send_json(403, False,
                                error="only an owner can manage an admin")
                return
            # updateRole: the NEW role must also be one the actor may manage, else an
            # admin could PROMOTE a member to admin (an admin it then can't edit) —
            # privilege escalation. Only an owner can move someone to/from admin.
            if op == "updateRole" and not rbac.can_manage_target(actor_role, fields["role"]):
                self._send_json(403, False, error="you cannot assign that role")
                return
            if (target["role"] == "owner"
                    and (op == "remove" or fields.get("role") != "owner")
                    and store.count_owners(org_id) <= 1):
                self._send_json(400, False, error="an organization must keep one owner")
                return
            if op == "remove":
                ok = store.delete_user(org_id, fields["userId"])
                if ok:
                    store.record_audit(org_id, actor["id"], "member_removed",
                                       target=str(fields["userId"]),
                                       detail=json.dumps({"role": target["role"]}))
            else:  # updateRole
                ok = store.update_user_role(org_id, fields["userId"], fields["role"])
                if ok:
                    store.record_audit(
                        org_id, actor["id"], "role_changed",
                        target=str(fields["userId"]),
                        detail=json.dumps({"from": target["role"], "to": fields["role"]}))
        except Exception:  # noqa: BLE001
            self._send_internal_error("team update")
            return
        finally:
            store.close()
        if not ok:
            self._send_json(404, False, error="no such teammate")
            return
        self._send_json(200, True, data={"userId": fields["userId"], "op": op})

    def _handle_invite(self, payload: Any) -> None:
        fields, err = _validate_invite(payload)
        if err is not None:
            self._send_json(400, False, error=err)
            return
        actor = self._current_user()
        org_id = actor["orgId"]
        store = Store(self.db_path)
        try:
            if fields["op"] == "revoke":
                ok = store.revoke_invite(org_id, fields["id"])
                if not ok:
                    self._send_json(404, False, error="no such pending invite")
                    return
                self._send_json(200, True, data={"id": fields["id"], "op": "revoke"})
                return
            if not rbac.can_assign_role(actor.get("role"), fields["role"]):
                self._send_json(403, False, error="you cannot invite that role")
                return
            throttle = self.invite_throttle
            actor_key = str(actor["id"])
            if throttle is not None and throttle.is_throttled(actor_key):
                self._send_json(429, False,
                                error="too many invites — try again later")
                return
            token = new_session_token()
            store.create_invite(org_id=org_id, role=fields["role"], token=token,
                                expires_at=time.time() + INVITE_TTL_SECONDS,
                                invited_by_user_id=actor["id"], email=fields["email"])
            if throttle is not None:
                throttle.record_create(actor_key)
            store.record_audit(org_id, actor["id"], "invite_created",
                               detail=json.dumps({"role": fields["role"]}))
        except Exception:  # noqa: BLE001
            self._send_internal_error("invite")
            return
        finally:
            store.close()
        # The raw token is returned ONCE — the inviter copies the link and shares it.
        self._send_json(200, True, data={
            "token": token, "role": fields["role"], "email": fields["email"],
            "path": f"/signup?invite={token}"})

    def _handle_org(self, payload: Any) -> None:
        fields, err = _validate_org(payload)
        if err is not None:
            self._send_json(400, False, error=err)
            return
        org_id = self._current_user()["orgId"]
        store = Store(self.db_path)
        try:
            org = store.update_organization(
                org_id, name=fields["name"], logo=fields["logo"],
                description=fields["description"])
        except ValueError as e:
            self._send_json(400, False, error=str(e))
            return
        except Exception:  # noqa: BLE001
            self._send_internal_error("org update")
            return
        finally:
            store.close()
        if org is None:
            self._send_json(404, False, error="organization not found")
            return
        self._send_json(200, True, data={"id": org["id"], "name": org["name"],
                                         "logo": org["logo"], "description": org["description"]})

    def _handle_settings(self, payload: Any) -> None:
        fields, err = _validate_settings(payload)
        if err is not None:
            self._send_json(400, False, error=err)
            return
        org_id = self._current_user()["orgId"]
        store = Store(self.db_path)
        try:
            for key, value in fields["settings"].items():
                store.set_setting(org_id, key, value)
            effective = store.get_settings(org_id)
        except Exception:  # noqa: BLE001
            self._send_internal_error("settings update")
            return
        finally:
            store.close()
        self._send_json(200, True, data=effective)

    def _handle_integration(self, payload: Any) -> None:
        fields, err = _validate_integration(payload)
        if err is not None:
            self._send_json(400, False, error=err)
            return
        platform = fields["platform"]
        api_key = fields["apiKey"]
        reddit_secret = fields["redditSecret"]
        # A connect carries a credential → validate live before capture; reject a bad
        # one with a 400 so the panel can surface it. Do this BEFORE opening the DB.
        # `secret` is the platform-generic dict to store (None for a toggle/disconnect).
        secret: Optional[dict] = None
        if api_key is not None:                         # YouTube key connect
            try:
                connections.validate_youtube_api_key(api_key)
            except connections.ConnectionValidationError as e:
                self._send_json(400, False, error=str(e))
                return
            secret = {"api_key": api_key}
        elif reddit_secret is not None:                 # Reddit app-credential connect
            try:
                connections.validate_reddit_credentials(
                    reddit_secret["client_id"], reddit_secret["client_secret"],
                    reddit_secret["user_agent"])
            except connections.ConnectionValidationError as e:
                self._send_json(400, False, error=str(e))
                return
            secret = reddit_secret
        actor = self._current_user()
        org_id = actor["orgId"]
        store = Store(self.db_path)
        try:
            row = self._apply_integration(store, org_id, platform, secret,
                                          fields["connected"], fields["detail"])
            # Audit only the state transitions (a credential connect / explicit
            # disconnect); a plain detail/toggle update is not a connect event.
            if secret is not None:
                store.record_audit(org_id, actor["id"], "integration_connected",
                                   target=platform)
            elif fields["connected"] is False:
                store.record_audit(org_id, actor["id"], "integration_disconnected",
                                   target=platform)
        except SecretCipherError as e:        # no/invalid AIZU_SECRET_KEY
            self._send_json(500, False, error=str(e))
            return
        except Exception:  # noqa: BLE001
            self._send_internal_error("integration update")
            return
        finally:
            store.close()
        self._send_json(200, True, data=row)

    @staticmethod
    def _apply_integration(store: Store, org_id: int, platform: str,
                           secret: Optional[dict], connected: Optional[bool],
                           detail: Optional[str]) -> dict[str, Any]:
        """Persist a connect (store the secret + mark connected), a disconnect
        (revoke the secret + mark disconnected), or a plain detail/toggle update.

        `secret` is the platform-generic encrypted credential dict (YouTube:
        {"api_key": …}; Reddit: {"client_id","client_secret","user_agent"})."""
        if secret is not None:                        # connect with a validated secret
            store.set_integration_secret(org_id, platform, secret)
            return store.set_integration(org_id, platform, connected=True,
                                         detail="connected")
        if connected is False:                        # disconnect = revoke + delete
            store.delete_integration_secret(org_id, platform)
            return store.set_integration(org_id, platform, connected=False,
                                         detail="not connected")
        return store.set_integration(org_id, platform, connected=connected,
                                     detail=detail)

    def _handle_telegram_start(self, payload: Any) -> None:
        """Wizard step 1: send a login code to the phone, return a wizard token."""
        if self.telegram_login is None:
            self._send_json(503, False, error="Telegram login is not enabled on this server")
            return
        phone = payload.get("phone") if isinstance(payload, dict) else None
        if not isinstance(phone, str) or not phone.strip():
            self._send_json(400, False, error="phone is required")
            return
        try:
            token = self.telegram_login.start(phone.strip())
        except TelegramLoginError as e:
            self._send_json(400, False, error=str(e))
            return
        except Exception:  # noqa: BLE001
            self._send_internal_error("telegram login start")
            return
        self._send_json(200, True, data={"token": token})

    def _handle_telegram_verify(self, payload: Any) -> None:
        """Wizard step 2: submit the code (+ 2FA). On success persist the session
        secret + flip connected; if 2FA is required, ask the panel for the password."""
        if self.telegram_login is None:
            self._send_json(503, False, error="Telegram login is not enabled on this server")
            return
        fields, err = _validate_telegram_verify(payload)
        if err is not None:
            self._send_json(400, False, error=err)
            return
        try:
            result = self.telegram_login.verify(
                fields["token"], fields["code"], fields["password"])
        except TelegramLoginError as e:
            self._send_json(400, False, error=str(e))
            return
        except Exception:  # noqa: BLE001
            self._send_internal_error("telegram login verify")
            return
        if not result.get("connected"):
            self._send_json(200, True, data={"needsPassword": True})
            return
        actor = self._current_user()
        org_id = actor["orgId"]
        store = Store(self.db_path)
        try:
            store.set_integration_secret(org_id, "telegram", result["session"])
            store.set_integration(org_id, "telegram", connected=True, detail="connected")
            # Mirror the YouTube connect path: a successful connect is an auditable
            # security event, so the audit trail has no silent gap for Telegram.
            store.record_audit(org_id, actor["id"], "integration_connected",
                               target="telegram")
        except SecretCipherError as e:
            self._send_json(500, False, error=str(e))
            return
        except Exception:  # noqa: BLE001
            self._send_internal_error("telegram login verify")
            return
        finally:
            store.close()
        self._send_json(200, True, data={"needsPassword": False})

    # ----- billing (v13) -----

    def _billing_provider(self, name: str) -> Optional["billing.BillingProvider"]:
        return (self.billing_providers or {}).get(name)

    def _billing_return_base(self) -> str:
        """Absolute base URL Polar redirects back to after checkout. Same-origin as
        the panel (the POST carries the panel's Origin), falling back to Host."""
        origin = self.headers.get("Origin") or ""
        if origin and _is_local_origin(origin):
            return origin.rstrip("/")
        if origin:
            return origin.rstrip("/")
        return f"http://{self.headers.get('Host', '127.0.0.1')}"

    def _handle_billing_checkout(self, payload: Any) -> None:
        fields, err = _validate_billing_checkout(payload)
        if err is not None:
            self._send_json(400, False, error=err)
            return
        provider = self._billing_provider("polar")
        if provider is None:
            self._send_json(503, False, error="billing is not configured on this server")
            return
        user = self._current_user()
        # A checkout ALWAYS creates a NEW subscription; Polar rejects it (422
        # "You already have an active subscription") for a customer who already
        # has one. Plan CHANGES go through the customer portal, not checkout.
        # Guard here so the user gets an actionable message, not Polar's raw 422.
        store = Store(self.db_path)
        try:
            existing = store.get_subscription(user["orgId"])
        finally:
            store.close()
        if existing["tier"] != "free" and existing["status"] in ("active", "trialing"):
            self._send_json(409, False,
                            error="You already have an active plan. Use “Manage billing” "
                                  "to change or cancel it.")
            return
        # Land on the Billing settings tab (a real, auth-gated route — NOT the bare
        # origin root) with a ?checkout=success marker so the panel shows the
        # celebration + plan-summary modal. The SPA is hosted at /app/ (the origin
        # root now serves the marketing landing page), so the return URL must land
        # there, not at "/". The hash fragment is the SPA route; the query after it
        # is read via react-router search params on that route.
        success_url = self._billing_return_base() + "/app/#/settings/billing?checkout=success"
        try:
            result = provider.create_checkout(
                fields["tier"], fields["interval"], user["orgId"],
                user.get("email", ""), success_url)
        except billing.BillingError as e:
            self._send_json(502, False, error=str(e))
            return
        self._send_json(200, True, data={"checkoutUrl": result.url})

    def _handle_billing_portal(self, _payload: Any) -> None:
        provider = self._billing_provider("polar")
        if provider is None:
            self._send_json(503, False, error="billing is not configured on this server")
            return
        org_id = self._current_user()["orgId"]
        try:
            result = provider.create_portal(org_id)
        except billing.BillingError as e:
            self._send_json(502, False, error=str(e))
            return
        # A Free org has no Polar customer yet → degrade cleanly, not a 500.
        self._send_json(200, True,
                        data={"portalUrl": result.url, "hasAccount": result.has_account})

    def _handle_billing_webhook(self) -> None:
        """PUBLIC, provider-signed. Verify on the RAW bytes → parse → resolve org →
        monotonic upsert. ACK 200 for any verified event (even unknown/unparseable)
        so the provider does not retry-storm; only a bad signature is 401."""
        provider = self._billing_provider("polar")
        if provider is None:
            self._send_json(503, False, error="billing is not configured on this server")
            return
        raw = self._read_raw_body(MAX_BODY_BYTES)
        if raw is None:
            self._send_json(400, False, error="missing or oversized body")
            return
        headers = {k: v for k, v in self.headers.items()}
        if not provider.verify_webhook(raw, headers):
            self._send_json(401, False, error="invalid webhook signature")
            return
        event = provider.parse_event(raw, headers)
        if isinstance(event, billing.ParseError):
            log.warning("Billing webhook parse error: %s", event.error)
            self._send_json(200, True, data={"received": True})
            return
        if event.org_id is None:
            log.warning("Billing webhook %s could not resolve an org", event.event_type)
            self._send_json(200, True, data={"received": True})
            return
        # ONLY subscription.* events carry subscription STATE. checkout.*/order.*/
        # product.*/customer.* events reuse the same `data.id`/`status` shape but
        # mean something else (a checkout's `status:"open"` and its checkout-id would
        # otherwise overwrite the row — and its now()-based event_ts would then block
        # the real subscription.active as "stale"). Ack-and-ignore everything else.
        if not event.event_type.startswith("subscription."):
            log.info("Billing webhook %s org=%s ack-and-ignore (not subscription state)",
                     event.event_type, event.org_id)
            self._send_json(200, True, data={"received": True})
            return
        store = Store(self.db_path)
        try:
            existing = store.get_subscription(event.org_id)
            # Single-active-rail invariant: ignore an event from a provider other
            # than the org's stored one (a provisioned org pays via exactly one rail).
            if existing.get("provider") not in (None, event.provider):
                log.warning("Billing webhook provider mismatch org=%s (%s vs stored %s)",
                            event.org_id, event.provider, existing.get("provider"))
                self._send_json(200, True, data={"received": True})
                return
            applied = store.upsert_subscription(
                event.org_id, last_event_ts=event.event_ts,
                provider=event.provider, tier=event.tier, interval=event.interval,
                status=event.status,
                cancel_at_period_end=1 if event.cancel_at_period_end else 0,
                current_period_start=event.current_period_start,
                current_period_end=event.current_period_end,
                provider_subscription_id=event.provider_subscription_id,
                provider_customer_id=event.provider_customer_id)
        except Exception as e:  # noqa: BLE001 — surface as 500, never crash the thread
            log.error("Billing webhook upsert failed org=%s: %s", event.org_id, e)
            self._send_json(500, False, error="webhook processing failed")
            return
        finally:
            store.close()
        log.info("Billing webhook %s org=%s tier=%s status=%s applied=%s",
                 event.event_type, event.org_id, event.tier, event.status, applied)
        self._send_json(200, True, data={"received": True})

    def _readiness_snapshot(self, *, force_refresh: bool = False,
                            platforms: Optional[Iterable[str]] = None) -> dict:
        """The agent-readiness contract dict, measured against whichever backend will
        actually execute a live run. in_process => probe THIS box's warmed Chrome;
        distributed => the cloud has no browser at all, so presence in the worker
        fleet is the real gate (readiness.fleet_readiness). The extra `backend` key
        is additive — the panel's Zod schema ignores keys it doesn't declare, and the
        banner uses it to hide the "launch a login browser" action on a cloud host
        where there is no browser to launch.

        `platforms` narrows the DISTRIBUTED answer to the platforms the caller's run
        actually needs — without it a fleet whose only online box advertises youtube
        answers ready:true for an instagram campaign, which is the exact "capable box,
        wrong platform" lie fleet_readiness' filter exists to prevent. It is ignored on
        the in_process path: there is one warmed Chrome, and check_readiness already
        reports its per-platform login state itself."""
        store = Store(self.db_path)
        try:
            backend = store.execution_backend()
            workers = store.list_workers() if backend == EXECUTION_DISTRIBUTED else []
        finally:
            store.close()
        if backend == EXECUTION_DISTRIBUTED:
            snapshot = readiness.fleet_readiness(workers, platforms=platforms)
        else:
            # A live run owns the ONE CDP connection this architecture allows;
            # check_readiness serves its last-known result instead of attaching a
            # second Playwright client while one is in flight.
            run_active = self.run_manager.is_active if self.run_manager is not None else None
            probe = self.readiness_probe or readiness.check_readiness
            snapshot = probe(readiness.default_cdp_url(),
                             force_refresh=force_refresh, run_active=run_active)
        return {**snapshot, "backend": backend}

    def _campaign_platform_scope(self, campaign_id: str,
                                 org_id: Optional[int]) -> Optional[set[str]]:
        """The platforms one campaign discovers on, for narrowing a readiness answer to
        the run the caller is actually about to start. None = no narrowing at all.

        Degrades to None (never an error) for a blank, unknown, not-ours or malformed
        campaign id: this backs a banner polled every 60s, so a stale id in a URL must
        cost the operator the SCOPE, not the whole verdict. The ownership check runs
        first — a campaign id is org-scoped data, and answering "that one is instagram"
        for another tenant's id would leak across the boundary."""
        if not campaign_id:
            return None
        store = Store(self.db_path)
        try:
            if not self._campaign_in_org(store, campaign_id, org_id):
                return None
            campaign = resolve_campaign(store, self.config_dir, campaign_id)
        except Exception:  # noqa: BLE001 — a malformed brief just means "no narrowing"
            log.warning("readiness scope: campaign %r could not be resolved", campaign_id)
            return None
        finally:
            store.close()
        return None if campaign is None else _campaign_platforms(campaign)

    def _handle_agent_readiness(self, query: str) -> None:
        """`GET /api/agent/readiness`: can a live run start right now? Raw dict (no
        {ok,data,error} envelope) — the panel's global banner polls it every 60s and
        `?refresh=1` forces a live probe past the <=60s server-side cache.

        `?campaign=<id>` scopes the answer to that campaign's platforms. It is the
        ONLY thing that makes `fleet_readiness`' platform filter live on the wire (B4:
        the filter shipped with no caller passing it, so the distributed answer was
        "some box is online", not "a box that can run THIS"). Unscoped stays the
        default — the global banner has no campaign in hand."""
        user = self._current_user()
        if user is None:
            self._send_json(401, False, error="authentication required")
            return
        params = parse_qs(query)
        refresh = params.get("refresh", ["0"])[0].strip().lower()
        force = refresh not in ("", "0", "false", "no")
        campaign_id = params.get("campaign", [""])[0].strip()
        try:
            scope = self._campaign_platform_scope(campaign_id, user["orgId"])
            snapshot = self._readiness_snapshot(force_refresh=force, platforms=scope)
        except Exception:  # noqa: BLE001 — a probe/DB failure must not 500 the banner
            log.error("agent readiness check failed", exc_info=True)
            self._send_json(500, False, error="internal server error")
            return
        self._send_json_body(200, _json_bytes(snapshot))

    def _handle_agent_launch_login(self, payload: Any) -> None:
        """`POST /api/agent/launch-login`: open (or focus) a Chrome tab on instagram.com
        so a human can sign the warmed browser back in. RBAC `fix_agent` (owner/admin)
        is already enforced by the protected-route gate. Raw {launched, readiness} —
        `launched` is best-effort, so a false with a still-unready readiness is a normal
        answer, not an error."""
        del payload  # the body is an empty object; nothing to read from it
        # Attaching Playwright mid-run would open a SECOND connection to the one
        # browser this architecture allows — refuse instead, in the {error, detail}
        # shape the panel already parses off a non-200.
        if self.run_manager is not None and self.run_manager.is_active():
            self._send_json_body(409, _json_bytes({
                "error": "run_active",
                "detail": "a run is active — stop it before opening a login browser "
                          "(only one Chrome connection is allowed at a time)",
            }))
            return
        try:
            before = self._readiness_snapshot(force_refresh=True)
            if before.get("backend") == EXECUTION_DISTRIBUTED:
                # No browser lives on the control plane in distributed mode — the
                # warmed Chrome is on the worker PC, and its login tab is opened from
                # that box's own desktop app.
                self._send_json_body(200, _json_bytes({
                    "launched": False, "readiness": before}))
                return
            launched = readiness.open_login_tab(readiness.default_cdp_url(),
                                                opener=self.login_opener)
            # The tab changes what a probe would see, so re-check rather than echo the
            # pre-launch snapshot back at a panel that is about to render it.
            after = self._readiness_snapshot(force_refresh=True)
        except Exception as e:  # noqa: BLE001 — Chrome itself failed to start
            log.error("agent launch-login failed", exc_info=True)
            self._send_json_body(500, _json_bytes({
                "error": "launch_failed",
                "detail": f"could not open a login browser: {e}"}))
            return
        self._send_json_body(200, _json_bytes(
            {"launched": launched, "readiness": after}))

    def _handle_run(self, payload: Any) -> None:
        fields, err = _validate_run(payload)
        if err is not None:
            self._send_json(400, False, error=err)
            return
        # (The in-process runner is required only for the in-process path; the
        # distributed backend enqueues to the fleet and needs no RunManager — the
        # 503 guard is deferred to just before run_manager.launch below.)
        # A single-campaign run must be runnable now (brief present, or it is the
        # file campaign). 'all' resolves its own live set when it runs.
        user = self._current_user()
        org_id = user["orgId"]
        # Which channels this run needs the shared warmed browser for (readiness gate
        # below). Left False for scope='all', which resolves its live set inside the
        # engine — pre-resolving every brief here just to classify it would put a
        # whole campaign set's parsing on the request path.
        needs_browser = needs_instagram = False
        if fields["scope"] == "campaign":
            store = Store(self.db_path)
            try:
                campaign = resolve_campaign(store, self.config_dir, fields["campaignId"])
                # Only run a campaign your org owns (registry check at the boundary).
                owned = self._campaign_in_org(store, fields["campaignId"], org_id)
            except ValueError as e:  # malformed brief → client error
                self._send_json(400, False, error=str(e))
                return
            except Exception:  # noqa: BLE001 — surface DB errors as JSON
                self._send_internal_error("run launch")
                return
            finally:
                store.close()
            if campaign is None or not owned:
                self._send_json(400, False,
                                error=f"campaign {fields['campaignId']!r} is not runnable "
                                      "(unknown, not yours, or no brief)")
                return
            platforms = _campaign_platforms(campaign)
            needs_browser = bool(platforms & CDP_PLATFORMS)
            needs_instagram = "instagram" in platforms
        # v13 billing soft-enforcement. get_subscription is the single choke point
        # (never None). A non-active plan or an exhausted period lead cap blocks
        # STARTING a run with 402 — reads/leads/exports stay open. The remaining cap
        # also CLAMPS this run's lead target so the period total can never exceed the
        # plan (no mid-run kill — the clamp bounds the target up front).
        bstore = Store(self.db_path)
        try:
            sub = bstore.get_subscription(org_id)
            since = bstore.period_since(org_id)
            used = bstore.count_leads_this_period(org_id, since)
        finally:
            bstore.close()
        if sub["status"] not in billing.RUNNABLE_STATUSES:
            self._send_json(402, False, error=_billing_inactive_message(sub))
            return
        remaining = sub["lead_cap"] - used
        if remaining <= 0:
            self._send_json(402, False, error=_billing_cap_message(sub))
            return
        requested = fields.get("targetLeadCount")
        clamped = remaining if requested is None else min(requested, remaining)
        if requested is not None and clamped < requested:
            log.info("Run lead target clamped by plan · org=%s requested=%s remaining=%s",
                     org_id, requested, remaining)
        # v27: echo the resolved bounds on every successful start. The clamp above is the
        # enforcement; this is the SURFACING of it, so the run UI can say "10 of 10 leads
        # left on Free" and bound its own input without a second round-trip — and so a
        # silently clamped target is visible rather than a run that quietly stops early.
        plan_bounds = {"targetLeads": clamped,
                       "maxRunLeads": billing.tier_max_run_leads(sub["tier"]),
                       "leadsRemaining": remaining}
        # An omitted durationMinutes stays None on the spec: the run is bounded by its
        # LEAD TARGET, not by wall clock. Each execution path then applies its OWN
        # runaway guard behind that target — `runner.TARGET_RUN_GUARD_MINUTES` for an
        # in-process run, the box's `max_job_minutes` for a fleet one. Injecting one here
        # would silently stretch a worker's supervisor deadline too.
        spec = RunSpec(scope=fields["scope"], campaign_id=fields["campaignId"],
                       mode=fields["mode"], org_id=org_id,
                       target_leads=clamped,
                       duration_minutes=fields.get("durationMinutes"))
        # v16: honor the platform-wide execution backend (superadmin switch). A LIVE run
        # in distributed mode is enqueued for the worker fleet instead of run in-process;
        # dry runs are a local sanity check (no real account) and always run in-process.
        estore = Store(self.db_path)
        try:
            backend = estore.execution_backend()
        finally:
            estore.close()
        if backend == EXECUTION_DISTRIBUTED and spec.mode == "live":
            self._dispatch_run_to_fleet(spec, org_id, plan_bounds)
            return
        if self.run_manager is None:
            self._send_json(503, False, error="run control is not enabled on this server")
            return
        # Agent-readiness gate. An in-process LIVE run drives the warmed Chrome on THIS
        # box, so launching against an unreachable or logged-out browser buys nothing
        # but a run that dies minutes later inside the engine, with the failure buried
        # in a run log. Answer 409 agent_not_ready up front instead, carrying the whole
        # snapshot so the panel names the exact problem without a second round-trip.
        # Narrowly scoped: dry runs use a fake feed and need no browser, and an
        # API-only campaign (youtube/reddit/telegram) never touches CDP.
        if spec.mode == "live" and needs_browser:
            try:
                snapshot = self._readiness_snapshot()
            except Exception:  # noqa: BLE001 — never let a probe failure block a run
                log.error("readiness gate check failed — allowing the run", exc_info=True)
                snapshot = None
            if snapshot is not None and (
                    snapshot["cdp"] != "ok"
                    or (needs_instagram and snapshot["instagram"] != "logged_in")):
                log.info("Run blocked by readiness gate · campaign=%s org=%s detail=%s",
                         fields["campaignId"], org_id, snapshot.get("detail"))
                self._send_json_body(409, _json_bytes({
                    "error": "agent_not_ready",
                    "detail": snapshot.get("detail") or "the agent is not ready",
                    "readiness": snapshot,
                }))
                return
        active, err = self.run_manager.launch(spec)
        if err == "a run is already active":
            self._send_json(409, False, error=err)
            return
        if err is not None:
            self._send_json(500, False, error=err)
            return
        log.info("Run accepted · scope=%s campaign=%s mode=%s org=%s id=%s",
                 fields["scope"], fields["campaignId"], fields["mode"], org_id,
                 getattr(active, "run_id", "?"))
        self._send_json(202, True, data={"accepted": True, "scope": fields["scope"],
                                         "campaignId": fields["campaignId"],
                                         "mode": fields["mode"], **plan_bounds})

    def _dispatch_run_to_fleet(self, spec: RunSpec, org_id: Optional[int],
                               plan_bounds: dict[str, Any]) -> None:
        """Distributed backend: enqueue the run as job(s) for the worker fleet instead of
        launching in-process. scope='campaign' → one job; scope='all' → one job per LIVE
        campaign that has a capable worker. A capability miss for a single-campaign run is
        a clear 409; for 'all' the incapable/brief-less campaigns are reported as skipped.
        Jobs carry the billing-clamped target so the fleet run respects the plan too."""
        store = Store(self.db_path)
        try:
            if spec.scope == "campaign":
                targets = [spec.campaign_id]
            else:
                # Mirror the CLI run-all / scheduler runnable predicate EXACTLY:
                # status='live' AND not archived (an archived-but-live campaign must
                # never run — the in-process path guards this, so must the fleet path).
                targets = [r["campaign_id"] for r in store.list_campaign_meta(org_id)
                           if r.get("status") == "live" and r.get("archived_at") is None]
            skipped: list[dict] = []
            # Pass 1: resolve + capability-check to find the dispatchable set, so the
            # billing budget can be split across exactly those jobs (pass 2).
            dispatchable: list[tuple[str, str, Campaign]] = []  # (campaign_id, platform, campaign)
            spend_cap = _fleet_spend_cap_usd()
            for cid in targets:
                try:
                    campaign = resolve_campaign(store, self.config_dir, cid)
                except Exception:  # noqa: BLE001 — a malformed brief just skips this one
                    campaign = None
                if campaign is None:
                    skipped.append({"campaignId": cid, "reason": "no runnable brief"})
                    continue
                if store.count_capable_workers(platform=campaign.platform, org_id=org_id,
                                               account_handle=None) == 0:
                    skipped.append({"campaignId": cid, "reason": "no capable worker"})
                    continue
                # B9: a campaign already at/over its spend cap would trip the box's guard
                # on its very first LLM call, so skip it here instead of dispatching a
                # run that can only degrade. Filtered in pass 1, not pass 2, so an
                # over-budget campaign does not also consume a slice of the billing lead
                # budget. `spend_cap is None` means the bridge does NOT know the fleet's
                # ceiling (the normal hosted split, where AIZU_SPEND_CAP lives on the
                # boxes) — then we must NOT skip, or the cloud would enforce a guessed
                # number forever; `run_one_job` refuses the run box-side instead. The
                # reason carries both figures so an operator can see WHY it stopped.
                if spend_cap is not None:
                    spent = store.total_spend(cid)
                    if spent >= spend_cap:
                        skipped.append({
                            "campaignId": cid,
                            "reason": (f"spend cap reached (${spent:.2f} spent of "
                                       f"${spend_cap:.2f})")})
                        continue
                dispatchable.append((cid, campaign.platform, campaign))
            # Pass 2: split the billing-clamped lead budget across the dispatchable jobs so
            # the fleet total can NEVER exceed the period cap (the slices sum to exactly the
            # clamp — no N× multiplication). A single-campaign run gets the full remainder;
            # campaigns past the budget (slice 0) are skipped, not run unbounded.
            budgets = _split_lead_budget(spec.target_leads or 0, len(dispatchable))
            # Bake the soul into each job spec (BUILD-PLAN C5): a REMOTE worker has no
            # soul.md on disk, so the cloud must ship it at enqueue time (the admin
            # enqueue path already does). Best-effort from the server's config dir; None
            # if absent → the worker falls back to a box-local soul.md, else nacks
            # soul_missing. Loaded once (soul is campaign-agnostic) — UNLIKE the campaign
            # brief below, which is the opposite: NOT campaign-agnostic, so it is
            # computed per-job inside this loop from the already-resolved Campaign.
            try:
                soul_text: Optional[str] = load_soul(
                    Path(self.config_dir) / "soul.md").text
            except Exception:  # noqa: BLE001 — no/unreadable soul.md just leaves it unbaked
                soul_text = None
            enqueued: list[str] = []
            run_ids: list[str] = []
            for (cid, platform, campaign), tgt in zip(dispatchable, budgets):
                if tgt <= 0:
                    skipped.append({"campaignId": cid, "reason": "period lead cap reached"})
                    continue
                # Bake the campaign brief into the job spec (B4/mirrors soul_text, C5's
                # sibling): a REMOTE worker has no shared DB row for this campaign_id, so
                # the server must ship the already-resolved brief at enqueue time — see
                # job_runner._resolve_campaign. Enforced against MAX_CAMPAIGN_BRIEF_BYTES
                # here (skip, not crash/truncate) mirroring the 'no runnable brief' /
                # 'no capable worker' skip reasons above.
                brief = campaign_to_brief(campaign)
                brief_bytes = len(json.dumps(brief, ensure_ascii=False).encode("utf-8"))
                if brief_bytes > MAX_CAMPAIGN_BRIEF_BYTES:
                    log.error(
                        "campaign %s brief is %d bytes, exceeds the %d-byte fleet-dispatch "
                        "cap -- skipping", cid, brief_bytes, MAX_CAMPAIGN_BRIEF_BYTES)
                    skipped.append({"campaignId": cid, "reason": "brief too large"})
                    continue
                # SECURITY REVIEW (CRITICAL): this used to also bake the org's DECRYPTED
                # per-platform credential (store.get_integration_secret) into the job spec
                # here, mirroring brief/soul above. That put a plaintext tenant secret into
                # the `jobs.spec` TEXT column, which nothing ever scrubs (ack_job only
                # touches status/result/session_id/leased_by — see store.py) — a durable
                # cloud-side copy that undoes the Fernet-at-rest protection in
                # core/secrets.py for as long as the row exists (forever). Credentials are
                # now decrypt-on-demand instead: the worker fetches its OWN job's credential
                # fresh, at job start, via POST /api/worker/jobs/{id}/credential, gated on
                # actually holding that job's lease right now (see
                # Handler._handle_job_credential / Store.get_leased_job_for_worker) — never
                # baked into anything that gets JSON-serialized to disk here.
                # Assign the run_id HERE (not on the worker) so the org's activity drawer
                # can poll this run live and the worker emits run_events under it.
                run_id = uuid.uuid4().hex[:12]
                try:
                    job = store.enqueue_job_deduped(
                        job_id=f"job-{uuid.uuid4().hex[:12]}", campaign_id=cid,
                        platform=platform, org_id=org_id, required_account_handle=None,
                        # engine_mode is 'harvest': only LIVE harvest runs route here (dry
                        # stays in-process above; warming has its own scheduler path).
                        spec={"engine_mode": "harvest", "target_leads": tgt,
                              "duration_minutes": spec.duration_minutes,
                              "run_id": run_id, "soul_text": soul_text,
                              "campaign_brief": brief})
                except Exception:  # noqa: BLE001 — one campaign's failure just skips it,
                    # never aborting the batch and stranding the already-enqueued jobs.
                    log.error("enqueue failed for campaign %s (continuing)", cid,
                              exc_info=True)
                    skipped.append({"campaignId": cid, "reason": "enqueue failed"})
                    continue
                if job is None:
                    # The campaign already has an in-flight job — a rapid double-Run.
                    # Skip rather than duplicate the job set (the double-Run guard).
                    skipped.append({"campaignId": cid, "reason": "already running"})
                    continue
                enqueued.append(job["id"])
                run_ids.append(run_id)
        except Exception:  # noqa: BLE001 — generic 500; detail to the log only
            self._send_internal_error("dispatch run to fleet")
            return
        finally:
            store.close()
        if not enqueued:
            # The deliberate distributed switch has no capable worker (or no runnable
            # brief) for this run — a clear 409 so the caller knows why nothing started.
            reason = (skipped[0]["reason"] if len(skipped) == 1
                      else "no capable worker in the fleet for this run")
            self._send_json(409, False, error=f"run not dispatched: {reason}")
            return
        log.info("Run dispatched to fleet · scope=%s org=%s jobs=%s skipped=%d",
                 spec.scope, org_id, enqueued, len(skipped))
        # `runId` (first job) lets the panel open the live activity drawer immediately —
        # the worker streams run_events under it via the job heartbeat; `runIds` covers a
        # scope=all fan-out.
        self._send_json(202, True, data={"accepted": True, "backend": "distributed",
                                         "scope": spec.scope, "jobs": enqueued,
                                         "runId": run_ids[0] if run_ids else None,
                                         "runIds": run_ids, "skipped": skipped,
                                         # v27: same plan bounds as the in-process 202 —
                                         # `targetLeads` is the whole clamped budget,
                                         # which pass 2 above SPLIT across these jobs.
                                         **plan_bounds})

    def _handle_run_stop(self, _payload: Any) -> None:
        """Stop the in-flight run early (the panel's Stop button). org-scoped: a 409
        'no run is active' covers both 'nothing running' and another org's run."""
        if self.run_manager is None:
            self._send_json(503, False, error="run control is not enabled on this server")
            return
        org_id = self._current_user()["orgId"]
        stopped, err = self.run_manager.stop(org_id)
        if not stopped:
            self._send_json(409, False, error=err or "no run is active")
            return
        log.info("Run stopped by operator · org=%s", org_id)
        self._send_json(200, True, data={"stopped": True})

    def _handle_run_pause(self, _payload: Any) -> None:
        """Cooperatively pause the in-flight run (resumable; the child keeps living).
        org-scoped; idempotent — re-pausing an already-paused run is 200. 409 only when
        nothing of yours is active."""
        if self.run_manager is None:
            self._send_json(503, False, error="run control is not enabled on this server")
            return
        org_id = self._current_user()["orgId"]
        paused, err = self.run_manager.pause(org_id)
        if not paused:
            self._send_json(409, False, error=err or "no run is active")
            return
        log.info("Run paused by operator · org=%s", org_id)
        self._send_json(200, True, data={"paused": True})

    def _handle_run_resume(self, _payload: Any) -> None:
        """Resume a cooperatively-paused run. org-scoped; idempotent — resuming a
        not-paused active run is 200. 409 only when nothing of yours is active."""
        if self.run_manager is None:
            self._send_json(503, False, error="run control is not enabled on this server")
            return
        org_id = self._current_user()["orgId"]
        resumed, err = self.run_manager.resume(org_id)
        if not resumed:
            self._send_json(409, False, error=err or "no run is active")
            return
        log.info("Run resumed by operator · org=%s", org_id)
        self._send_json(200, True, data={"paused": False})

    # ----- v14 distributed-workers plane -----
    def _handle_worker_register(self, payload: Any) -> None:
        """Register / re-register a worker box (LOCKED #4, #8). Trust model: a
        re-register presents its current bearer token, and the worker row's OWN
        `enrolment_scope_kind` (stamped once, at whichever call first enrolled it —
        see below) re-clamps org_id/capabilities on THIS call too, exactly as it did
        at enrolment (v22.1, BUILD-PLAN B8 follow-up: a re-register used to trust the
        box's freshly self-declared orgId/capabilities verbatim, which let any
        already-enrolled box silently walk itself into another org's scope using
        nothing but its own bearer token — see memory/known-issues.md B8). A brand
        new box's first register (v22, BUILD-PLAN B8 fix) presents, through that SAME
        bearer slot, either a per-worker, single-use, admin-minted ENROLMENT token
        (tried first — its scope is SERVER-ASSIGNED and clamps what gets written, see
        below) or, only if that fails to redeem, the shared AIZU_WORKER_BOOTSTRAP_TOKEN
        as a deprecated fallback (gated by AIZU_WORKER_LEGACY_BOOTSTRAP_ENABLED,
        default ON so upgrade day is a no-op for every already-provisioned box) — a
        legacy-fallback enrolment stores enrolment_scope_kind=NULL, so THAT box's
        later re-registers stay fully self-declared (unchanged, pre-v22.1 behaviour),
        same as any worker that already existed before this column did. Either way
        the server mints a fresh plaintext token, persists ONLY its hash, and returns
        the plaintext EXACTLY ONCE. Never logs the token.

        OPS NOTE: the one-time token rides in the response body. Body suppression for
        this path lives only in the application layer (see _send_json_body), so NEVER
        route /api/worker/register through a logging reverse proxy — the token would
        appear in that proxy's access log (security review L)."""
        fields, err = _validate_worker_register(payload)
        if err is not None:
            self._send_json(400, False, error=err)
            return
        # Auth: re-register (existing token) OR first-register (enrolment token,
        # falling back to the legacy shared bootstrap secret).
        worker = self._current_worker()
        enrolment_scope: Optional[tuple[str, Optional[int]]] = None
        fresh_scope_kind: Optional[str] = None  # only set when THIS call redeemed a
        # token — passed to register_worker so a plain re-register (None) leaves the
        # worker's already-stored scope kind untouched (COALESCE, store.py).
        if worker is not None:
            worker_id = worker["id"]  # rotate the token for this same box
            # v22.1: re-derive the clamp from the worker's OWN stored scope kind
            # (stamped at whichever call first enrolled it), not from anything in
            # THIS request body — closes the re-register self-escalation gap above.
            stored_scope_kind = worker.get("enrolmentScopeKind")
            if stored_scope_kind in ("org", "pool"):
                enrolment_scope = (stored_scope_kind, worker.get("orgId"))
        else:
            if not fields["machineId"]:
                self._send_json(400, False,
                                error="machineId is required on first registration")
                return
            worker_id = fields["machineId"]
            bearer = self._request_bearer_token()
            redemption = None
            if bearer:
                # Try the per-worker enrolment token FIRST — a valid one always wins,
                # even while the legacy fallback below is still enabled. Its own
                # short-lived Store, closed immediately, mirroring _current_worker.
                token_store = Store(self.db_path)
                try:
                    redemption = token_store.redeem_worker_enrolment_token(
                        token=bearer, worker_id=worker_id)
                except Exception:  # noqa: BLE001 — a DB hiccup reads as "no token"
                    log.error("enrolment-token redemption failed", exc_info=True)
                    redemption = None
                finally:
                    token_store.close()
            if redemption is not None:
                enrolment_scope = (redemption["scopeKind"], redemption["orgId"])
                fresh_scope_kind = redemption["scopeKind"]  # persist: stamps the row
                # so every FUTURE re-register on this box's own bearer re-derives the
                # same clamp from `worker.enrolmentScopeKind` above, instead of
                # trusting that later call's self-declared orgId/capabilities.
            else:
                secret = os.environ.get(WORKER_BOOTSTRAP_ENV, "")
                legacy_enabled = os.environ.get(
                    WORKER_LEGACY_BOOTSTRAP_ENV, "1").strip().lower() not in (
                        "0", "false", "no", "")
                if (not legacy_enabled or not secret or not bearer
                        or not hmac.compare_digest(bearer, secret)):
                    self._send_json(401, False,
                                    error="worker registration requires a valid token")
                    return
                # Ledger B10: revocation must survive the box. `register_worker` UPSERTs
                # `revoked_at = NULL`, so without this a revoked box that dropped its
                # token (which is exactly what the sidecar does when it is revoked) would
                # resurrect itself on the next process start using the still-configured
                # SHARED bootstrap secret — silently undoing an operator's revoke. Only an
                # admin-minted, single-use enrolment token (redeemed above) or an explicit
                # un-revoke may bring a revoked worker back.
                revoked_store = Store(self.db_path)
                try:
                    already_revoked = revoked_store.is_worker_revoked(worker_id)
                except Exception:  # noqa: BLE001 — cannot prove it is NOT revoked
                    log.error("revocation check failed", exc_info=True)
                    self._send_json(503, False, error="registration is temporarily "
                                                      "unavailable")
                    return
                finally:
                    revoked_store.close()
                if already_revoked:
                    log.warning(
                        "REFUSED a shared-bootstrap register for REVOKED worker %s — "
                        "re-enrol it with a per-worker enrolment token", worker_id)
                    self._send_json(401, False,
                                    error="worker is revoked; re-enrol it with a "
                                          "per-worker enrolment token")
                    return
                log.warning(
                    "Worker registered via DEPRECATED shared bootstrap token — "
                    "migrate to per-worker enrolment tokens · id=%s host=%s",
                    worker_id, fields["host"])
        token = new_session_token()  # plaintext minted at the HTTP boundary
        # Phase 4: long-lived token (1 year) — revocation, not expiry, is the off-switch
        # (PRD §7). A re-register rotates the token and refreshes this window.
        token_expires_at = time.time() + WORKER_TOKEN_TTL_SEC
        # v22/v22.1: an enrolment scope CLAMPS what gets written — overriding
        # whatever the box self-declared — rather than trusting the box's own
        # orgId/capabilities (the B8 gap). This applies BOTH on the call that redeems
        # a fresh enrolment token AND on every later re-register of that same worker
        # (enrolment_scope re-derived above from the worker's own stored
        # enrolment_scope_kind — the v22.1 fix). 'org' forces org_id AND every
        # capability's cap_org to the token's org; 'pool' is the deliberate
        # multi-org grant (PRD: one managed box serving ~10 companies) and leaves
        # capabilities UNCLAMPED (cap_org=None or whatever the box declared passes
        # through), org_id=None. No enrolment scope at all (a worker that was itself
        # enrolled via the legacy bootstrap fallback, or that pre-dates this column)
        # ⇒ completely unchanged self-declared behaviour, every call.
        org_id, capabilities = fields["orgId"], fields["capabilities"]
        if enrolment_scope is not None:
            scope_kind, scope_org_id = enrolment_scope
            org_id = scope_org_id if scope_kind == "org" else None
            if scope_kind == "org":
                capabilities = [[scope_org_id, plat, handle]
                                for (_cap_org, plat, handle) in capabilities]
        store = Store(self.db_path)
        try:
            store.register_worker(
                worker_id=worker_id, token=token, org_id=org_id,
                display_name=fields["displayName"], host=fields["host"],
                os=fields["os"], agent_version=fields["agentVersion"],
                enrolment_scope_kind=fresh_scope_kind,
                max_sessions=fields["maxSessions"], capabilities=capabilities,
                token_expires_at=token_expires_at,
                # v23 (F9/F10/F12): the box's own launch self-check, stored verbatim and
                # surfaced in the fleet console. Diagnostic ONLY — deliberately read
                # AFTER every auth/clamp decision above so nothing on the trust path can
                # ever branch on a worker-authored field.
                preflight=fields["preflight"])
        except Exception:  # noqa: BLE001 — off-cloud caller: detail stays server-side
            log.error("worker register failed", exc_info=True)
            self._send_json(500, False, error="internal server error")
            return
        finally:
            store.close()
        # `id` only — never echo the token to the log (the RedactingFilter also
        # scrubs "token": shapes, but we keep it out of our own log line entirely).
        log.info("Worker registered · id=%s host=%s", worker_id, fields["host"])
        self._send_json(200, True, data={
            "workerId": worker_id, "token": token,
            "heartbeatIntervalSec": int(WORKER_HEARTBEAT_INTERVAL_SEC)})

    def _handle_worker_heartbeat(self, payload: Any) -> None:
        """Worker-level presence heartbeat (LOCKED #8). Bearer-gated: the token IS the
        identity — the body's workerId is ignored so one worker can never stamp
        another's row. Updates last_heartbeat_at (+ current_sessions if carried) and
        returns the RESOLVED worker-level control flags (Phase 4, C6): global + the
        worker's org + the worker itself + every platform it can serve, OR-merged.
        updateRequired also fires when the worker's agent_version is below the gate."""
        worker = self._current_worker()
        if worker is None:
            self._reject_worker_auth()
            return
        fields, err = _validate_worker_heartbeat(payload)
        if err is not None:
            self._send_json(400, False, error=err)
            return
        store = Store(self.db_path)
        try:
            ok = store.record_worker_heartbeat(
                worker_id=worker["id"], current_sessions=fields["currentSessions"],
                # v23: omitted ⇒ None ⇒ COALESCE keeps the stored summary. The sidecar
                # only re-sends on change (or every 10th beat), so most beats are None.
                preflight=fields["preflight"])
            flags = self._resolve_worker_flags(store, worker) if ok else None
        except Exception:  # noqa: BLE001 — off-cloud caller: detail stays server-side
            log.error("worker heartbeat failed", exc_info=True)
            self._send_json(500, False, error="internal server error")
            return
        finally:
            store.close()
        if not ok:
            # Token resolved at the gate but the row vanished/was revoked mid-flight.
            self._send_json(404, False, error="worker not found")
            return
        self._send_json(200, True, data=flags)

    def _resolve_worker_flags(self, store: "Store", worker: dict) -> dict:
        """OR-merge the control flags that apply to a whole worker (no single job in
        hand): global + its org + itself + every platform in its capabilities. Folds the
        agent-version gate into updateRequired. Returns the wire shape
        {drain, halt, updateRequired}."""
        merged = store.resolve_control_flags(
            org_id=worker.get("orgId"), worker_id=worker["id"])
        platforms = {c[1] for c in (worker.get("capabilities") or [])
                     if isinstance(c, (list, tuple)) and len(c) == 3}
        for plat in platforms:
            pf = store.resolve_control_flags(platform=plat)
            merged = {k: merged[k] or pf[k] for k in merged}
        update_required = (merged["update_required"]
                           or _agent_version_below(worker.get("agentVersion"),
                                                    _min_agent_version()))
        return {"drain": merged["drain"], "halt": merged["halt"],
                "updateRequired": update_required}

    # ----- v15 superadmin plane: login / logout / whoami -----
    def _handle_admin_login(self, payload: Any) -> None:
        """Authenticate a platform admin: IP-allowlist + password + TOTP MFA, all three
        required (PRD §10). On success mints an rr_admin_session cookie and audits the
        login; every failure is throttled (DB-backed) and audited. Errors are opaque
        (`invalid credentials`) so the response never distinguishes a bad password from a
        bad code, a missing admin, or a disabled one."""
        allowlist = os.environ.get(admin_auth.ADMIN_IP_ALLOWLIST_ENV, "")
        if not admin_auth.ip_allowed(self._client_ip(), allowlist):
            self._send_json(403, False, error="forbidden")
            return
        fields, err = _validate_admin_login(payload)
        if err is not None:
            self._send_json(400, False, error=err)
            return
        store = Store(self.db_path)
        try:
            if store.admin_login_is_locked(fields["email"]):
                log.warning("Admin login throttled · %s", fields["email"])
                self._send_json(429, False,
                                error="too many failed attempts — try again later")
                return
            admin = store.get_platform_admin_by_email(fields["email"])
            # Always run a real verify (dummy hash when absent) so timing doesn't
            # disclose whether the admin exists.
            stored_hash = admin["password_hash"] if admin else _DUMMY_PASSWORD_HASH
            password_ok = verify_password(fields["password"], stored_hash) and admin is not None
            disabled = admin is not None and admin.get("disabled_at") is not None
            matched_counter = None
            if admin is not None and not disabled:
                try:
                    secret = store.get_admin_totp_secret(int(admin["id"]))
                except SecretCipherError:
                    # Misconfigured/absent AIZU_SECRET_KEY → 500, never a silent
                    # MFA bypass (fail closed).
                    log.error("admin MFA secret decrypt failed", exc_info=True)
                    self._send_json(500, False, error="internal server error")
                    return
                if secret:
                    matched_counter = admin_auth.matched_totp_counter(
                        secret, fields["totpCode"])
            if disabled or not (password_ok and matched_counter is not None):
                store.admin_login_record_failure(fields["email"])
                store.append_admin_audit(
                    acting_admin_id=(int(admin["id"]) if admin else None),
                    action="admin.login.failed", ip=self._client_ip(),
                    user_agent=self.headers.get("User-Agent"),
                    reason="disabled" if disabled else "bad credentials or mfa")
                log.warning("Admin login failed · %s", fields["email"])
                self._send_json(401, False, error="invalid credentials")
                return
            # Password + TOTP are both valid — now CONSUME the code's step counter so it
            # can't be replayed inside its ~90s acceptance window (security review HIGH
            # #1). Consumed only here (after password passes) so an attacker can't burn a
            # victim's code without also knowing the password.
            if not store.claim_totp_counter(int(admin["id"]), matched_counter):
                store.admin_login_record_failure(fields["email"])
                store.append_admin_audit(
                    acting_admin_id=int(admin["id"]), action="admin.login.failed",
                    ip=self._client_ip(), user_agent=self.headers.get("User-Agent"),
                    reason="totp code replay")
                log.warning("Admin login replay rejected · %s", fields["email"])
                self._send_json(401, False, error="invalid credentials")
                return
            store.admin_login_reset(fields["email"])
            token = new_session_token()
            store.create_admin_session(token, int(admin["id"]),
                                       admin_auth.admin_session_expiry())
            store.purge_expired_admin_sessions()
            store.append_admin_audit(acting_admin_id=int(admin["id"]),
                                     action="admin.login", ip=self._client_ip(),
                                     user_agent=self.headers.get("User-Agent"))
            log.success("Admin login ok · %s", fields["email"])
            data = {"admin": {"id": int(admin["id"]), "email": admin["email"]}}
        except Exception:  # noqa: BLE001 — never leak internal detail on the auth surface
            log.error("admin login failed", exc_info=True)
            self._send_json(500, False, error="internal server error")
            return
        finally:
            store.close()
        self._send_json(200, True, data=data,
                        extra_headers=[self._set_admin_cookie_header(token)])

    def _handle_admin_logout(self) -> None:
        # IP-allowlist gate like every admin route — a stolen cookie replayed off-network
        # can't even force-logout the admin (security review MEDIUM #4).
        allowlist = os.environ.get(admin_auth.ADMIN_IP_ALLOWLIST_ENV, "")
        if not admin_auth.ip_allowed(self._client_ip(), allowlist):
            self._send_json(403, False, error="forbidden")
            return
        token = self._request_admin_session_token()
        if token:
            store = Store(self.db_path)
            try:
                sess = store.get_admin_session(token)
                store.delete_admin_session(token)
                if sess is not None:
                    store.append_admin_audit(
                        acting_admin_id=sess["adminId"], action="admin.logout",
                        ip=self._client_ip(), user_agent=self.headers.get("User-Agent"))
            except Exception:  # noqa: BLE001 — clearing the cookie still logs the admin out
                log.warning("admin logout housekeeping failed", exc_info=True)
            finally:
                store.close()
        self._send_json(200, True, data={"loggedOut": True},
                        extra_headers=[self._clear_admin_cookie_header()])

    def _handle_admin_whoami(self) -> None:
        admin = self._current_admin()
        if admin is None:
            self._send_json(401, False, error="not authenticated")
            return
        impersonating = (admin["effectiveOrgId"] is not None
                         or admin["effectiveUserId"] is not None)
        self._send_json(200, True, data={"admin": {
            "id": admin["adminId"], "email": admin["email"],
            "impersonating": impersonating,
            "effectiveOrgId": admin["effectiveOrgId"],
            "effectiveUserId": admin["effectiveUserId"],
            "impersonationReason": admin["impersonationReason"]}})

    def _require_admin(self) -> Optional[dict[str, Any]]:
        """The real v15 admin gate (IP-allowlist + admin session). Sends 401 and returns
        None when the caller is not an authenticated platform admin; else the admin."""
        admin = self._current_admin()
        if admin is None:
            self._send_json(401, False, error="platform admin authentication required")
            return None
        return admin

    def _handle_admin_impersonate(self, payload: Any) -> None:
        """Start impersonation (Phase 5c): stamp the effective principal onto the admin
        session so the org plane serves the target org, and write a hash-chained audit
        row. The target must exist (no impersonating a ghost). Exactly one sanctioned
        place a foreign org/user is adopted (PRD §10)."""
        admin = self._require_admin()
        if admin is None:
            return
        fields, err = _validate_impersonate(payload)
        if err is not None:
            self._send_json(400, False, error=err)
            return
        token = self._request_admin_session_token()
        store = Store(self.db_path)
        try:
            if fields["userId"] is not None:
                target = store.get_user_by_id(fields["userId"])
                if target is None or target.get("org_id") is None:
                    self._send_json(404, False, error="unknown user")
                    return
                target_org_id, target_user_id = int(target["org_id"]), fields["userId"]
            else:
                if store.get_organization(fields["orgId"]) is None:
                    self._send_json(404, False, error="unknown organization")
                    return
                target_org_id, target_user_id = fields["orgId"], None
            now = time.time()
            # If the admin session lapsed between the gate and here, the UPDATE writes
            # nothing — return 409 and DON'T audit a ghost impersonation that never took
            # effect (security review MEDIUM #5).
            if not store.set_admin_impersonation(
                    token, effective_org_id=target_org_id,
                    effective_user_id=target_user_id, reason=fields["reason"], now=now):
                self._send_json(409, False, error="admin session no longer active")
                return
            store.append_admin_audit(
                acting_admin_id=admin["adminId"], action="impersonate.start",
                target_org_id=target_org_id, target_user_id=target_user_id,
                ip=self._client_ip(), user_agent=self.headers.get("User-Agent"),
                reason=fields["reason"], impersonation_start=now, at=now)
        except Exception:  # noqa: BLE001
            log.error("impersonation start failed", exc_info=True)
            self._send_json(500, False, error="internal server error")
            return
        finally:
            store.close()
        self._send_json(200, True, data={"impersonating": {
            "orgId": target_org_id, "userId": target_user_id}})

    def _handle_admin_impersonate_end(self) -> None:
        """End impersonation: clear the effective principal + audit the end. Idempotent —
        ending when not impersonating still succeeds (and is audited)."""
        admin = self._require_admin()
        if admin is None:
            return
        token = self._request_admin_session_token()
        store = Store(self.db_path)
        try:
            now = time.time()
            store.clear_admin_impersonation(token)
            store.append_admin_audit(
                acting_admin_id=admin["adminId"], action="impersonate.end",
                target_org_id=admin["effectiveOrgId"],
                target_user_id=admin["effectiveUserId"], ip=self._client_ip(),
                user_agent=self.headers.get("User-Agent"),
                impersonation_end=now, at=now)
        except Exception:  # noqa: BLE001
            log.error("impersonation end failed", exc_info=True)
            self._send_json(500, False, error="internal server error")
            return
        finally:
            store.close()
        self._send_json(200, True, data={"impersonating": None})

    def _handle_admin_audit_verify(self) -> None:
        admin = self._require_admin()
        if admin is None:
            return
        store = Store(self.db_path)
        try:
            result = store.verify_admin_audit_chain()
        except Exception:  # noqa: BLE001
            log.error("audit verify failed", exc_info=True)
            self._send_json(500, False, error="internal server error")
            return
        finally:
            store.close()
        self._send_json(200, True, data=result)

    def _handle_admin_audit_list(self, query: dict[str, list[str]]) -> None:
        admin = self._require_admin()
        if admin is None:
            return
        try:
            limit = int((query.get("limit") or ["200"])[0])
        except (ValueError, TypeError):
            limit = 200
        store = Store(self.db_path)
        try:
            entries = store.list_admin_audit(limit=limit)
        except Exception:  # noqa: BLE001
            log.error("audit list failed", exc_info=True)
            self._send_json(500, False, error="internal server error")
            return
        finally:
            store.close()
        self._send_json(200, True, data={"entries": entries})

    def _handle_admin_orgs(self) -> None:
        """Cross-org index (Phase 5d): every org with member + campaign + lead counts.
        Cross-tenant BY DESIGN — behind the real admin gate. This is a READ view (no
        impersonation, no writes), the console's org picker."""
        admin = self._require_admin()
        if admin is None:
            return
        store = Store(self.db_path)
        try:
            orgs = store.list_organizations()
            for o in orgs:
                # N+1 over the org list — fine at PRD scale (~10 orgs); documented as the
                # Postgres-migration trigger if the fleet grows.
                o["campaign_count"] = len(store.list_campaign_meta(o["id"]))
        except Exception:  # noqa: BLE001
            log.error("admin orgs listing failed", exc_info=True)
            self._send_json(500, False, error="internal server error")
            return
        finally:
            store.close()
        self._send_json(200, True, data={"orgs": orgs})

    def _handle_admin_org_read(self, org_id: int, subresource: str,
                               query: dict[str, list[str]]) -> None:
        """Cross-org READ of one org's campaigns, leads or runs (Phase 5d), reusing the
        same org-page builders the org plane uses — with the target org_id and owner role
        for full visibility. Distinct from impersonation: read-only, no effective principal
        is set, and it is gated by the admin session (not an org cookie)."""
        admin = self._require_admin()
        if admin is None:
            return
        store = Store(self.db_path)
        try:
            if store.get_organization(org_id) is None:
                self._send_json(404, False, error="unknown organization")
                return
            if subresource == "campaigns":
                data = build_admin_org_campaigns(store, org_id=org_id)
                self._send_json(200, True, data=data)
                return
            if subresource == "runs":
                # v27: the run picker for the narrative feed the org plane no longer
                # serves. Modes come from the in-memory RunManager UNFILTERED (org_id
                # None) — this is the cross-tenant admin plane, and the map is keyed by
                # run id, so only the target org's own runs can pick anything out of it.
                status = (self.run_manager.status(None) if self.run_manager is not None
                          else {"active": None, "recent": []})
                modes = {r["id"]: r.get("mode") for r in status.get("recent", [])
                         if r.get("id")}
                active = status.get("active")
                if active and active.get("id"):
                    modes[active["id"]] = active.get("mode")
                self._send_json(200, True, data={
                    "runs": _build_admin_org_runs(store, org_id, modes)})
                return
            # leads — server-side filtered/sorted/paginated, like /api/leads
            page = _query_int(query.get("page"), 1)
            page_size = _query_int(query.get("pageSize"), LEADS_PAGE_SIZE_DEFAULT)
            descending = (query.get("dir") or ["desc"])[0] != "asc"
            data = build_admin_org_leads(
                store, org_id=org_id, page=page,
                page_size=page_size, q=(query.get("q") or [None])[0],
                status=(query.get("status") or [None])[0],
                platform=(query.get("platform") or [None])[0],
                campaign_filter=(query.get("campaign") or [None])[0],
                sort=(query.get("sort") or ["capturedAt"])[0], descending=descending)
            self._send_json(200, True, data=data)
        except ValueError as e:
            self._send_json(400, False, error=str(e))
        except Exception:  # noqa: BLE001
            log.error("admin org read failed", exc_info=True)
            self._send_json(500, False, error="internal server error")
        finally:
            store.close()

    def _handle_admin_run_activity(self, query: dict[str, list[str]]) -> None:
        """The FULL narrative feed for one run (v27): messages, details, identities — the
        rows /api/run/activity no longer hands an org. Cross-tenant BY DESIGN and NOT
        org-scoped: a superadmin picks a run off /api/admin/orgs/{id}/runs and inspects it
        whoever owns it. Same posture as the other Phase 5d read views — the real admin
        gate (IP-allowlist + admin session), read-only, no impersonation, and no audit row
        (only the writes and impersonation are audited on this plane).

        An unknown run answers an empty feed rather than 404: there is no tenant boundary
        left to protect here, and a fleet run that has not yet heartbeated its first event
        is exactly the run an operator is trying to watch."""
        if self._require_admin() is None:
            return
        run_id = (query.get("runId") or [""])[0].strip()
        if not run_id:
            self._send_json(400, False, error="runId is required")
            return
        try:  # same lenient cursor parse as the org endpoint — a junk `after` is not a 400
            after = max(0, int((query.get("after") or ["0"])[0]))
        except (ValueError, TypeError):
            after = 0
        store = Store(self.db_path)
        try:
            events = store.fetch_run_events(run_id, after_id=after)
            sessions = store.sessions_for_run(run_id)
            flags = store.run_events_open_flags(run_id)
            actions = store.action_counts_for_run(run_id)
        except Exception:  # noqa: BLE001 — keep DB/schema detail server-side
            log.error("admin run activity failed", exc_info=True)
            self._send_json(500, False, error="internal server error")
            return
        finally:
            store.close()
        # `finished` so the console's drawer can stop polling. The in-memory active run is
        # read unfiltered (admin plane) and matched by id; otherwise fall back to the
        # durable session rows, exactly like the org endpoint.
        active = (self.run_manager.status(None).get("active")
                  if self.run_manager is not None else None)
        is_active_run = active is not None and active.get("id") == run_id
        has_running_session = any(
            s.get("status") == "running" or s.get("ended_at") is None for s in sessions)
        self._send_json(200, True, data={
            "runId": run_id,
            "finished": not is_active_run and not has_running_session,
            "counters": _aggregate_run_counters(sessions, actions),
            "events": events,
            "flags": [{"kind": f.get("kind"), "severity": f.get("severity"),
                       "detail": f.get("detail")} for f in flags],
            "cursor": events[-1]["id"] if events else after,
        })

    def _handle_admin_fleet(self) -> None:
        # v15 Phase 5d: gated by the REAL platform-admin plane (IP-allowlist + admin
        # session), replacing the interim AIZU_PLATFORM_ADMINS env allowlist.
        if self._require_admin() is None:
            return
        store = Store(self.db_path)
        try:
            workers = store.list_workers()
        except Exception:  # noqa: BLE001 — keep DB/schema detail server-side
            log.error("fleet listing failed", exc_info=True)
            self._send_json(500, False, error="internal server error")
            return
        finally:
            store.close()
        self._send_json(200, True, data={"workers": workers})

    # ----- worker plane: lease / job heartbeat / ack / nack (v14 Phase 3) -----
    def _handle_worker_lease(self, payload: Any) -> None:
        """Lease one job for the authenticated worker (bearer-gated). The worker's
        capabilities come from its registered row — NOT the body — so a worker can only
        lease what it declared. An empty queue returns ``{ok:true, data:null}`` (HTTP
        200, never 204), after an optional bounded long-poll. Each poll iteration opens
        its own short-lived Store so a held connection never blocks the single writer."""
        worker = self._current_worker()
        if worker is None:
            self._reject_worker_auth()
            return
        fields, err = _validate_worker_lease(payload)
        if err is not None:
            self._send_json(400, False, error=err)
            return
        deadline = time.monotonic() + fields["leasePollTimeoutSec"]
        worker_id, capabilities = worker["id"], worker["capabilities"]
        while True:
            store = Store(self.db_path)
            try:
                lease = store.lease_one_job(worker_id=worker_id,
                                            capabilities=capabilities)
            except Exception:  # noqa: BLE001 — off-cloud caller: detail stays server-side
                log.error("worker lease failed", exc_info=True)
                self._send_json(500, False, error="internal server error")
                return
            finally:
                store.close()
            if lease is not None:
                self._send_json(200, True, data={"job": lease,
                                                 "leaseExpiresAt": lease["leaseExpiresAt"]})
                return
            if time.monotonic() >= deadline:
                self._send_json(200, True, data=None)  # empty lease — never 204
                return
            time.sleep(_WORKER_LEASE_POLL_STEP_SEC)

    def _handle_worker_job_action(self, job_id: str, action: str,
                                  payload: Any) -> None:
        """Dispatch the bearer-gated job-scoped routes. The token (not the body) is the
        authoritative worker identity, and the URL job_id (not the body) the
        authoritative job — so one worker can never heartbeat/ack/nack/credential
        another's job."""
        worker = self._current_worker()
        if worker is None:
            self._reject_worker_auth()
            return
        if action == "heartbeat":
            self._handle_job_heartbeat(worker["id"], job_id, payload)
        elif action == "ack":
            self._handle_job_ack(worker["id"], job_id, payload)
        elif action == "nack":
            self._handle_job_nack(worker["id"], job_id, payload)
        else:  # "credential" — the only remaining allowed action (matcher guarantees this)
            self._handle_job_credential(worker["id"], job_id)

    def _handle_job_heartbeat(self, worker_id: str, job_id: str,
                              payload: Any = None) -> None:
        """Job heartbeat: extend the lease + mark the job running, then return the
        RESOLVED control flags for THIS job's scope (Phase 4, C6): global + the job's
        org + its platform + the worker, OR-merged. A lost lease (reclaimed/finished/
        never owned) returns ``halt:true`` regardless so the worker tears the run down
        rather than working a job it no longer owns. `halt` → engine teardown + nack;
        `drain` → finish this job, stop leasing after.

        The heartbeat also carries any new run_events the worker captured for its live
        job (`runEvents`) — synced into the cloud feed under the JOB's own run_id/org/
        campaign (forced, BOLA) so the org's activity drawer shows the fleet run live."""
        store = Store(self.db_path)
        try:
            extended = store.extend_lease(job_id=job_id, worker_id=worker_id)
            if not extended:
                data = {"halt": True, "drain": False, "updateRequired": False,
                        "leaseExpiresAt": None}
            else:
                job = store.get_job(job_id)
                # get_job → _job_row_to_dict returns camelCase keys ("orgId", not
                # "org_id"); reading the snake_case key would silently drop org-scoped
                # halt/drain on this path (code review HIGH).
                flags = store.resolve_control_flags(
                    org_id=(job or {}).get("orgId"),
                    platform=(job or {}).get("platform"),
                    worker_id=worker_id)
                update_required = (flags["update_required"]
                                   or self._worker_update_required(worker_id))
                self._sync_job_run_events(store, job, payload)
                data = {"halt": flags["halt"], "drain": flags["drain"],
                        "updateRequired": update_required,
                        "leaseExpiresAt": time.time() + default_lease_ttl_sec()}
        except Exception:  # noqa: BLE001
            log.error("job heartbeat failed", exc_info=True)
            self._send_json(500, False, error="internal server error")
            return
        finally:
            store.close()
        self._send_json(200, True, data=data)

    def _sync_job_run_events(self, store: "Store", job: Optional[dict],
                             payload: Any) -> None:
        """Fold the worker-shipped `runEvents` from a job heartbeat into the cloud feed.
        The run_id/org/campaign are taken from the JOB (BOLA — a worker can't stream into
        another tenant's run), never from the payload. Best-effort: never fails the beat."""
        if job is None or not isinstance(payload, dict):
            return
        events = payload.get("runEvents")
        if not isinstance(events, list) or not events:
            return
        # The job's own run_id (assigned at enqueue) is authoritative; a legacy job with
        # none falls back to the worker-reported id (still org/campaign-forced below).
        run_id = (job.get("spec") or {}).get("run_id") or payload.get("runId")
        if not isinstance(run_id, str) or not run_id:
            return
        try:
            store.sync_run_events(run_id, events, org_id=job.get("orgId"),
                                  campaign_id=job.get("campaignId"))
        except Exception:  # noqa: BLE001 — activity sync must never fail the heartbeat
            log.warning("run_events sync failed for job %s", job.get("id"), exc_info=True)

    def _worker_update_required(self, worker_id: str) -> bool:
        """Version-gate the authenticated worker (already resolved at the gate). The
        cached _current_worker carries agentVersion — no extra DB read."""
        worker = self._current_worker()
        if worker is None or worker.get("id") != worker_id:
            return False
        return _agent_version_below(worker.get("agentVersion"), _min_agent_version())

    def _handle_job_ack(self, worker_id: str, job_id: str, payload: Any) -> None:
        fields, err = _validate_worker_ack(payload)
        if err is not None:
            self._send_json(400, False, error=err)
            return
        store = Store(self.db_path)
        try:
            recorded = store.ack_job(job_id=job_id, worker_id=worker_id,
                                     summary=fields["summary"], leads=fields.get("leads"),
                                     spend=fields.get("spend"),
                                     worker_db_id=fields.get("dbId"))
        except Exception:  # noqa: BLE001
            log.error("job ack failed", exc_info=True)
            self._send_json(500, False, error="internal server error")
            return
        finally:
            store.close()
        # recorded False = idempotent no-op (already terminal / not this worker's job):
        # still HTTP 200 so the worker doesn't retry-storm a job that is already done.
        self._send_json(200, True, data={"recorded": recorded})

    def _handle_job_nack(self, worker_id: str, job_id: str, payload: Any) -> None:
        fields, err = _validate_worker_nack(payload)
        if err is not None:
            self._send_json(400, False, error=err)
            return
        store = Store(self.db_path)
        try:
            result = store.nack_job(
                job_id=job_id, worker_id=worker_id, reason=fields["reason"],
                poison=fields["poison"], retry_after_at=fields["retryAfterAt"],
                leads=fields.get("leads"),
                spend=fields.get("spend"), worker_db_id=fields.get("dbId"))
        except Exception:  # noqa: BLE001
            log.error("job nack failed", exc_info=True)
            self._send_json(500, False, error="internal server error")
            return
        finally:
            store.close()
        # `leadsSynced` is echoed so the worker can tell "stored" from "silently
        # dropped": a bridge that predates lead-on-nack omits the key entirely, and
        # that absence is the ONLY signal a worker has that its lead sync is inert.
        self._send_json(200, True, data={
            "recorded": result["outcome"] != "ignored",
            "outcome": result["outcome"], "retryAfterAt": result["retryAfterAt"],
            "leadsSynced": result.get("leadsSynced", 0)})

    def _handle_job_credential(self, worker_id: str, job_id: str) -> None:
        """Decrypt-on-demand credential fetch (SECURITY REVIEW CRITICAL/HIGH — closes
        the durable-plaintext-in-jobs.spec hole that server._dispatch_run_to_fleet used
        to open by baking the org's decrypted secret at enqueue time). Nothing decrypted
        is ever written to any DB column or lease response; it is decrypted fresh, on
        this request, and handed straight to the caller.

        AUTHORIZATION is the load-bearing part, and it is deliberately TIGHTER than the
        pool-wide capability matching `_job_capability_covers` does for leasing: the
        requesting worker must be the worker that CURRENTLY HOLDS THE LEASE on THIS
        job (store.get_leased_job_for_worker checks `leased_by` + status, not just
        platform/org capability), and the org/platform used to decrypt come from the
        JOB ROW, never from anything the client could send. A worker that doesn't hold
        this lease — including a same-capability worker that could lease a similar job,
        or one whose lease already expired — gets a 404, matching the existing cross-org
        'unknown campaign'/'unknown run' convention elsewhere in this file: the 404
        neither confirms the job exists nor discloses whether it once did (BOLA-safe;
        NOT 403, which would leak existence)."""
        store = Store(self.db_path)
        try:
            job = store.get_leased_job_for_worker(job_id, worker_id)
            if job is None:
                self._send_json(404, False, error="unknown job")
                return
            credential = None
            if job["platform"] in PER_ORG_CREDENTIAL_PLATFORMS and job["orgId"] is not None:
                try:
                    credential = store.get_integration_secret(job["orgId"], job["platform"])
                except Exception:  # noqa: BLE001 — never leak a decrypt failure's detail
                    log.error("integration secret decrypt failed for job=%s org=%s "
                              "platform=%s", job_id, job["orgId"], job["platform"],
                              exc_info=True)
                    self._send_json(500, False, error="internal server error")
                    return
        except Exception:  # noqa: BLE001
            log.error("job credential fetch failed", exc_info=True)
            self._send_json(500, False, error="internal server error")
            return
        finally:
            store.close()
        # credential is None both for a CDP platform (instagram/linkedin/x — no per-org
        # secret exists) and for a per-org-credentialed platform the org simply hasn't
        # connected yet; the worker's own PER_ORG_CREDENTIAL_PLATFORMS gate means it
        # should never even ask for the former, but this endpoint answers either the
        # same tolerant way rather than distinguishing "wrong platform" from "not
        # connected" — both fall back to cli._resolve_platform_credentials' existing
        # env/local-store fallback on the worker side.
        self._send_json(200, True, data={"credential": credential})

    def _handle_admin_enqueue(self, payload: Any) -> None:
        """Operator enqueue (v15 real platform-admin gate). Rejects (400) a job no
        registered worker can ever serve so it never idles forever (BUILD-PLAN Phase 3
        enqueue validation)."""
        if self._require_admin() is None:
            return
        fields, err = _validate_enqueue(payload)
        if err is not None:
            self._send_json(400, False, error=err)
            return
        store = Store(self.db_path)
        try:
            capable = store.count_capable_workers(
                platform=fields["platform"], org_id=fields["orgId"],
                account_handle=fields["requiredAccountHandle"])
            if capable == 0:
                self._send_json(400, False, error=(
                    "no registered worker can serve this (org, platform, account) — "
                    "register/declare a capable worker before enqueueing"))
                return
            job = store.enqueue_job(
                job_id=fields["jobId"] or f"job-{uuid.uuid4().hex[:12]}",
                campaign_id=fields["campaignId"], platform=fields["platform"],
                spec=fields["spec"], org_id=fields["orgId"],
                required_account_handle=fields["requiredAccountHandle"])
        except Exception:  # noqa: BLE001
            log.error("job enqueue failed", exc_info=True)
            self._send_json(500, False, error="internal server error")
            return
        finally:
            store.close()
        self._send_json(200, True, data={"job": job})

    # ----- Phase 4 lifecycle controls (v15 real platform-admin gate) -----
    def _handle_admin_control_flags(self, payload: Any) -> None:
        """Set or clear a control flag (BUILD-PLAN C6 source of truth). `clear:true`
        removes the row; otherwise the passed drain/halt/updateRequired flags are merged
        onto the (scope, scopeKey) row. The acting admin's email is recorded in set_by."""
        admin = self._require_admin()
        if admin is None:
            return
        fields, err = _validate_control_flags(payload)
        if err is not None:
            self._send_json(400, False, error=err)
            return
        store = Store(self.db_path)
        try:
            if fields["clear"] and not fields["flags"]:
                cleared = store.clear_control_flags(
                    scope=fields["scope"], scope_key=fields["scopeKey"])
                data = {"cleared": cleared}
            else:
                row = store.set_control_flag(
                    scope=fields["scope"], scope_key=fields["scopeKey"],
                    reason=fields["reason"],
                    set_by=admin["email"], **fields["flags"])
                data = {"flag": row}
        except ValueError as e:
            self._send_json(400, False, error=str(e))
            return
        except Exception:  # noqa: BLE001
            log.error("control-flag write failed", exc_info=True)
            self._send_json(500, False, error="internal server error")
            return
        finally:
            store.close()
        self._send_json(200, True, data=data)

    def _handle_admin_control_flags_list(self) -> None:
        """List all set control flags for the admin console (GET)."""
        if self._require_admin() is None:
            return
        store = Store(self.db_path)
        try:
            flags = store.list_control_flags()
        except Exception:  # noqa: BLE001
            log.error("control-flag listing failed", exc_info=True)
            self._send_json(500, False, error="internal server error")
            return
        finally:
            store.close()
        self._send_json(200, True, data={"flags": flags})

    def _handle_admin_worker_revoke(self, payload: Any) -> None:
        """Revoke a worker's bearer token. After this the worker fails auth at its NEXT
        request (request-time revocation, LOCKED #5) — it must re-register (with the
        bootstrap secret) to rejoin the pool."""
        if self._require_admin() is None:
            return
        fields, err = _validate_worker_revoke(payload)
        if err is not None:
            self._send_json(400, False, error=err)
            return
        store = Store(self.db_path)
        try:
            revoked = store.revoke_worker(fields["workerId"])
        except Exception:  # noqa: BLE001
            log.error("worker revoke failed", exc_info=True)
            self._send_json(500, False, error="internal server error")
            return
        finally:
            store.close()
        self._send_json(200, True, data={"revoked": revoked})

    # ----- v22 worker enrolment tokens (real platform-admin gate; BUILD-PLAN B8 fix) -----
    def _handle_admin_worker_enrolment_mint(self, payload: Any) -> None:
        """Mint a per-worker, single-use enrolment token with an admin-chosen,
        server-assigned scope ('org'+org_id, or explicit 'pool'). Mirrors
        _handle_worker_register's mint-plaintext-at-the-HTTP-boundary discipline: the
        plaintext token is generated HERE, persisted ONLY as a hash, and returned in
        THIS response exactly once — never stored, never logged, never returned by
        the list endpoint."""
        admin = self._require_admin()
        if admin is None:
            return
        fields, err = _validate_worker_enrolment_mint(payload)
        if err is not None:
            self._send_json(400, False, error=err)
            return
        token_id = f"wet-{uuid.uuid4().hex[:12]}"
        token = new_session_token()  # plaintext minted at the HTTP boundary
        expires_at = time.time() + fields["ttlHours"] * 3600
        store = Store(self.db_path)
        try:
            rec = store.create_worker_enrolment_token(
                token_id=token_id, token=token, scope_kind=fields["scope"],
                org_id=fields["orgId"], label=fields["label"],
                created_by_admin_id=admin["adminId"], expires_at=expires_at)
            store.append_admin_audit(
                acting_admin_id=admin["adminId"], action="worker_enrolment_token.mint",
                target_org_id=fields["orgId"], target_resource=token_id,
                ip=self._client_ip(), user_agent=self.headers.get("User-Agent"))
        except Exception:  # noqa: BLE001
            log.error("worker enrolment-token mint failed", exc_info=True)
            self._send_json(500, False, error="internal server error")
            return
        finally:
            store.close()
        # `id` only — never log the plaintext (kept out of our own log line entirely,
        # same discipline as _handle_worker_register).
        log.info("Worker enrolment token minted · id=%s scope=%s org=%s by=%s",
                 token_id, fields["scope"], fields["orgId"], admin["email"])
        self._send_json(200, True, data={**rec, "token": token})

    def _handle_admin_worker_enrolment_list(self) -> None:
        """List all enrolment tokens (pending/redeemed/revoked) for the admin
        console. Never returns the plaintext or the hash."""
        if self._require_admin() is None:
            return
        store = Store(self.db_path)
        try:
            tokens = store.list_worker_enrolment_tokens()
        except Exception:  # noqa: BLE001
            log.error("worker enrolment-token listing failed", exc_info=True)
            self._send_json(500, False, error="internal server error")
            return
        finally:
            store.close()
        self._send_json(200, True, data={"tokens": tokens})

    def _handle_admin_worker_enrolment_revoke(self, payload: Any) -> None:
        """Cancel a still-pending enrolment token. Idempotent: {revoked:false} for an
        unknown/already-redeemed/already-revoked token. Audited UNCONDITIONALLY, even
        on a no-op — an attempted revoke is itself security-relevant (mirrors the
        execution-backend precedent's unconditional audit, not
        _handle_admin_worker_revoke's no-audit gap)."""
        admin = self._require_admin()
        if admin is None:
            return
        fields, err = _validate_worker_enrolment_revoke(payload)
        if err is not None:
            self._send_json(400, False, error=err)
            return
        store = Store(self.db_path)
        try:
            revoked = store.revoke_worker_enrolment_token(
                fields["tokenId"], by_admin_id=admin["adminId"])
            store.append_admin_audit(
                acting_admin_id=admin["adminId"], action="worker_enrolment_token.revoke",
                target_resource=fields["tokenId"], ip=self._client_ip(),
                user_agent=self.headers.get("User-Agent"),
                reason=None if revoked else
                       "no-op: token already redeemed or already revoked")
        except Exception:  # noqa: BLE001
            log.error("worker enrolment-token revoke failed", exc_info=True)
            self._send_json(500, False, error="internal server error")
            return
        finally:
            store.close()
        self._send_json(200, True, data={"revoked": revoked})

    # ----- v16 execution-backend switch (real platform-admin gate) -----
    def _handle_admin_execution_backend_get(self) -> None:
        """Report the active run execution backend + the selectable options (GET)."""
        if self._require_admin() is None:
            return
        store = Store(self.db_path)
        try:
            backend = store.execution_backend()
        except Exception:  # noqa: BLE001
            log.error("execution-backend read failed", exc_info=True)
            self._send_json(500, False, error="internal server error")
            return
        finally:
            store.close()
        self._send_json(200, True,
                        data={"backend": backend, "options": list(EXECUTION_BACKENDS)})

    def _handle_admin_set_execution_backend(self, payload: Any) -> None:
        """Switch the platform-wide run execution backend (in_process|distributed).
        This reroutes EVERY subsequent campaign run — org 'Run' buttons included — so
        the change is recorded in the tamper-evident admin audit log."""
        admin = self._require_admin()
        if admin is None:
            return
        backend = payload.get("backend") if isinstance(payload, dict) else None
        if backend not in EXECUTION_BACKENDS:
            self._send_json(400, False, error=(
                f"backend must be one of {list(EXECUTION_BACKENDS)}"))
            return
        store = Store(self.db_path)
        try:
            store.set_execution_backend(backend, by=admin["email"])
            store.append_admin_audit(
                acting_admin_id=admin["adminId"], action="execution_backend.set",
                target_resource=backend, ip=self._client_ip(),
                user_agent=self.headers.get("User-Agent"))
        except Exception:  # noqa: BLE001
            log.error("execution-backend write failed", exc_info=True)
            self._send_json(500, False, error="internal server error")
            return
        finally:
            store.close()
        log.info("Execution backend set to %s by admin %s", backend, admin["email"])
        self._send_json(200, True, data={"backend": backend})

    # ----- v17 model-comparison switch (real platform-admin gate) -----
    def _handle_admin_model_comparison_get(self) -> None:
        """Report the fan-out on/off state + the env-configured model list (GET).
        The model list is display-only here — it is never stored, only read from
        this box's own MODEL_COMPARISON_MODELS at call time."""
        if self._require_admin() is None:
            return
        store = Store(self.db_path)
        try:
            enabled = store.model_comparison_enabled()
        except Exception:  # noqa: BLE001
            log.error("model-comparison read failed", exc_info=True)
            self._send_json(500, False, error="internal server error")
            return
        finally:
            store.close()
        self._send_json(200, True, data={
            "enabled": enabled, "models": _parse_csv_env("MODEL_COMPARISON_MODELS")})

    def _handle_admin_set_model_comparison(self, payload: Any) -> None:
        """Switch the platform-wide model-comparison fan-out on/off. Governs
        in-process runs immediately; a distributed worker box needs its own
        MODEL_COMPARISON_ENABLED env (see cli.py) since it never reads this DB."""
        admin = self._require_admin()
        if admin is None:
            return
        enabled = payload.get("enabled") if isinstance(payload, dict) else None
        if not isinstance(enabled, bool):
            self._send_json(400, False, error="enabled must be a boolean")
            return
        store = Store(self.db_path)
        try:
            store.set_model_comparison_enabled(enabled, by=admin["email"])
            store.append_admin_audit(
                acting_admin_id=admin["adminId"], action="model_comparison.set",
                target_resource=str(enabled), ip=self._client_ip(),
                user_agent=self.headers.get("User-Agent"))
        except Exception:  # noqa: BLE001
            log.error("model-comparison write failed", exc_info=True)
            self._send_json(500, False, error="internal server error")
            return
        finally:
            store.close()
        log.info("Model comparison set to %s by admin %s", enabled, admin["email"])
        self._send_json(200, True, data={"enabled": enabled})

    def _handle_admin_model_comparison_stats_get(self) -> None:
        """Aggregate per-model stats + recent raw calls for the Model Performance
        page (GET, admin-gated)."""
        if self._require_admin() is None:
            return
        store = Store(self.db_path)
        try:
            stats = store.model_comparison_stats()
            recent = store.model_comparison_recent(limit=200)
        except Exception:  # noqa: BLE001
            log.error("model-comparison stats read failed", exc_info=True)
            self._send_json(500, False, error="internal server error")
            return
        finally:
            store.close()
        self._send_json(200, True, data={"stats": stats, "recent": recent})

    def do_OPTIONS(self):  # noqa: N802 — CORS preflight for the dev panel
        origin = self.headers.get("Origin", "")
        self.send_response(204)
        if origin and _is_local_origin(origin):
            self.send_header("Access-Control-Allow-Origin", origin)
            self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
            self.send_header("Access-Control-Allow-Headers", "Content-Type")
            self.send_header("Access-Control-Max-Age", "600")
            self.send_header("Vary", "Origin")
        self.end_headers()

    def do_GET(self):  # noqa: N802
        self._dispatch_guarded(self._route_get)

    def do_HEAD(self):  # noqa: N802
        """HEAD routes exactly like GET, minus the body.

        There was no do_HEAD, so HEAD fell through to SimpleHTTPRequestHandler's raw
        path→filesystem mapping and bypassed EVERY route in `_route_get`:
        /app/campaigns answered GET 200 / HEAD 404, /api/state GET 401-JSON /
        HEAD 404-html, /app GET 200 / HEAD 301 — contradicting the two design
        comments in `_route_get` that explain why those paths never fall through to
        statics. Re-running the same router behind a body-suppressing writer keeps
        the two methods honest by construction rather than by a parallel routing
        table someone has to remember to update.
        """
        real = self.wfile
        self.wfile = _HeadBodySuppressor(real)
        try:
            self._dispatch_guarded(self._route_get)
        finally:
            self.wfile = real

    def _route_get(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path == AUTH_ME_PATH:
            self._handle_me()
            return
        if parsed.path == INVITE_PATH:
            # Public: lets an invitee see the org branding + role before signing up.
            self._handle_invite_lookup(parse_qs(parsed.query))
            return
        if parsed.path == RUN_ACTIVITY_PATH:
            self._serve_run_activity(parsed.query)
            return
        if parsed.path == DASHBOARD_PATH:
            self._serve_org_page("view_dashboard", build_dashboard_org, attach_run=True)
            return
        if parsed.path == CAMPAIGNS_PATH:
            self._serve_org_page("view_campaigns", build_campaigns_org,
                                 attach_run=True, enrich=_attach_fleet_run_ids)
            return
        if parsed.path == REPORTS_PATH:
            self._serve_org_page("view_reports", build_reports_org)
            return
        if parsed.path == SETTINGS_PATH:
            self._serve_org_page("view_settings", build_settings_org)
            return
        if parsed.path == LEADS_PATH:
            self._serve_leads_page(parsed.query)
            return
        if parsed.path == ADMIN_WHOAMI_PATH:
            self._handle_admin_whoami()
            return
        if parsed.path == ADMIN_AUDIT_VERIFY_PATH:
            self._handle_admin_audit_verify()
            return
        if parsed.path == ADMIN_AUDIT_PATH:
            self._handle_admin_audit_list(parse_qs(parsed.query))
            return
        if parsed.path == ADMIN_ORGS_PATH:
            self._handle_admin_orgs()
            return
        if parsed.path == ADMIN_RUN_ACTIVITY_PATH:
            self._handle_admin_run_activity(parse_qs(parsed.query))
            return
        admin_org_route = _match_admin_org_route(parsed.path)
        if admin_org_route is not None:
            self._handle_admin_org_read(admin_org_route[0], admin_org_route[1],
                                        parse_qs(parsed.query))
            return
        if parsed.path == ADMIN_FLEET_PATH:
            self._handle_admin_fleet()
            return
        if parsed.path == ADMIN_WORKER_ENROLMENT_TOKENS_PATH:
            self._handle_admin_worker_enrolment_list()
            return
        if parsed.path == ADMIN_CONTROL_FLAGS_PATH:
            self._handle_admin_control_flags_list()
            return
        if parsed.path == ADMIN_EXECUTION_BACKEND_PATH:
            self._handle_admin_execution_backend_get()
            return
        if parsed.path == ADMIN_MODEL_COMPARISON_STATS_PATH:
            self._handle_admin_model_comparison_stats_get()
            return
        if parsed.path == ADMIN_MODEL_COMPARISON_PATH:
            self._handle_admin_model_comparison_get()
            return
        if parsed.path == AGENT_READINESS_PATH:
            self._handle_agent_readiness(parsed.query)
            return
        if parsed.path == STATE_PATH:
            if self._current_user() is None:
                self._send_json(401, False, error="authentication required")
                return
            self._serve_state(parsed.query)
            return
        if parsed.path in ("/", "/index.html"):
            self._serve_index()
            return
        # The SPA lives at /app/ (the landing owns "/" and its nav is in-page anchors,
        # which createHashRouter would otherwise swallow as routes — see repo docs).
        # "/app/index.html" is included here too: it IS a real file under panel_dir
        # once the panel is built, so without this it would fall through to the
        # generic _maps_to_existing_file branch below and be served by plain
        # SimpleHTTPRequestHandler statics — which never sets Cache-Control, unlike
        # every other path that reaches this shell. Naming it explicitly keeps
        # no-store consistent no matter how a client spells the request. No redirect
        # from "/app" to "/app/": the hash fragment (e.g. "#/leads") never reaches the
        # server either way, so a 301 round trip buys nothing.
        if parsed.path in ("/app", "/app/", "/app/index.html"):
            self._serve_app_index()
            return
        # Any other /api/* GET is an unknown endpoint, not a client route — answer
        # with a JSON 404. Falling through to an HTML shell here would return
        # 200 text/html for a request the panel parses as JSON, surfacing as an
        # opaque "unparseable response" instead of a clear 404.
        if parsed.path == "/api" or parsed.path.startswith("/api/"):
            self._send_json(404, False, error="unknown endpoint")
            return
        # Real files (hashed JS/CSS under /assets, landing's own css/js/vendor/fonts/
        # photos under /landing, favicon, etc.) serve directly.
        if self._maps_to_existing_file(self.path):
            # HEAD takes the base class's own head path so a big asset is stat'd,
            # not read off disk only to be discarded by the body suppressor.
            if self.command == "HEAD":
                super().do_HEAD()
            else:
                super().do_GET()
            return
        # An unknown path under /app/ (e.g. a stale path-based bookmark from before
        # this split, or someone typing a route by hand) still resolves to the SPA
        # shell so createHashRouter gets a chance to parse the fragment client-side.
        if parsed.path.startswith("/app/"):
            self._serve_app_index()
            return
        # Any other unknown non-API path → soft-landing fallback on the marketing
        # page (not the SPA shell — the SPA no longer lives at "/"), but ONLY for a
        # bare top-level word like "/matches": the landing is a single page whose
        # nav is in-page anchors, and its stylesheet/script URLs are RELATIVE. Served
        # under a nested path such as "/pricing/enterprise", the browser resolves
        # those against "/pricing/", every one of them 404s back into this same
        # fallback — HTML, status 200 — and the page renders unstyled while a
        # sniffing browser is invited to treat markup as CSS/JS. Anything nested, or
        # carrying a file extension (a genuinely missing asset), is an honest 404.
        if _is_soft_landing_path(parsed.path):
            self._serve_index()
            return
        self.send_error(404, "Not Found")

    def _default_campaign_for_org(self, store: Store, cfg: Path,
                                  org_id: Optional[int]) -> Optional[Campaign]:
        """The org's home campaign for an unscoped /api/state: prefer the file
        campaign if this org owns it (preserves the migrated default org's view),
        else the org's first registered campaign; None if the org has none."""
        try:
            file_campaign = load_campaign(cfg / "campaign.md")
        except (FileNotFoundError, ValueError):
            file_campaign = None
        if file_campaign and store.campaign_in_org(file_campaign.campaign_id, org_id):
            return file_campaign
        metas = store.list_campaign_meta(org_id)
        if metas:
            return resolve_campaign(store, cfg, metas[0]["campaign_id"])
        return None

    def _gated_org_user(self, action: str) -> Optional[tuple[int, str]]:
        """The 401→403→rbac ladder shared by every per-page GET. Returns (org_id, role)
        when the caller may perform `action`, else sends the error response and None."""
        user = self._current_user()
        if user is None:
            self._send_json(401, False, error="authentication required")
            return None
        org_id = user.get("orgId")
        if org_id is None:
            self._send_json(403, False, error="account is not attached to an organization")
            return None
        role = user.get("role")
        if not rbac.can(role, action):
            self._send_json(403, False, error="your role does not permit this action")
            return None
        return org_id, role

    def _serve_org_page(self, action: str, builder: Any, *,
                        attach_run: bool = False,
                        enrich: Optional[Any] = None) -> None:
        """Build one org-wide page via `builder(store, campaign, org_id=, role=)` and
        send it as a raw keys dict (same envelope-free shape as /api/state). `attach_run`
        adds the in-memory RUN control-plane block (dashboard + campaigns). `enrich`,
        when given, is called as `enrich(store, raw, org_id)` to fold in extra
        DB-derived fields while the store is still open."""
        gated = self._gated_org_user(action)
        if gated is None:
            return
        org_id, role = gated
        try:
            store = Store(self.db_path)
            try:
                campaign = self._default_campaign_for_org(store, Path(self.config_dir), org_id)
                raw = builder(store, campaign, org_id=org_id, role=role)
                if enrich is not None:
                    enrich(store, raw, org_id)
            finally:
                store.close()
            if attach_run and self.run_manager is not None:
                raw["RUN"] = self.run_manager.status(org_id)
            self._send_json_body(200, _json_bytes(raw))
        except ValueError as e:  # malformed brief for the org's home campaign
            self._send_json(400, False, error=str(e))
        except Exception:  # noqa: BLE001 — generic 500; detail to the log only
            self._send_internal_error("org page build")

    def _serve_leads_page(self, query: str) -> None:
        """`/api/leads`: org-wide, server-side filtered/sorted/paginated. Enveloped in
        {ok,data,error} because the pagination metadata has no top-level record key."""
        gated = self._gated_org_user("view_leads")
        if gated is None:
            return
        org_id, role = gated
        qs = parse_qs(query)
        page = _query_int(qs.get("page"), 1)
        page_size = _query_int(qs.get("pageSize"), LEADS_PAGE_SIZE_DEFAULT)
        descending = (qs.get("dir") or ["desc"])[0] != "asc"
        try:
            store = Store(self.db_path)
            try:
                campaign = self._default_campaign_for_org(store, Path(self.config_dir), org_id)
                data = build_leads_org(
                    store, campaign, org_id=org_id, role=role, page=page,
                    page_size=page_size, q=(qs.get("q") or [None])[0],
                    status=(qs.get("status") or [None])[0],
                    platform=(qs.get("platform") or [None])[0],
                    campaign_filter=(qs.get("campaign") or [None])[0],
                    sort=(qs.get("sort") or ["capturedAt"])[0], descending=descending)
            finally:
                store.close()
            self._send_json(200, True, data=data)
        except ValueError as e:
            self._send_json(400, False, error=str(e))
        except Exception:  # noqa: BLE001 — generic 500; detail to the log only
            self._send_internal_error("leads page build")

    def _serve_state(self, query: str) -> None:
        # State is scoped to the caller's org and PRUNED to their role. `?campaign=`
        # picks one campaign (verified to be in the org); absent, the org's home
        # campaign is used, or an empty state if the org has none yet.
        user = self._current_user()  # non-None: /api/state is behind the auth gate
        org_id = user.get("orgId")
        role = user.get("role")
        if org_id is None:
            self._send_json(403, False, error="account is not attached to an organization")
            return
        campaign_id = (parse_qs(query).get("campaign") or [None])[0]
        try:
            store = Store(self.db_path)
            try:
                cfg = Path(self.config_dir)
                soul = load_soul(cfg / "soul.md")
                if campaign_id and campaign_id.strip():
                    cid = campaign_id.strip()
                    campaign = resolve_campaign(store, cfg, cid)
                    # 404 (not 403) on a cross-org id so existence isn't disclosed.
                    if campaign is None or not self._campaign_in_org(store, cid, org_id):
                        self._send_json(404, False, error=f"unknown campaign {cid!r}")
                        return
                    raw = build_raw(store, soul, campaign, org_id=org_id, role=role)
                else:
                    campaign = self._default_campaign_for_org(store, cfg, org_id)
                    if campaign is None:
                        raw = build_empty_raw(store, soul, org_id=org_id, role=role)
                    else:
                        raw = build_raw(store, soul, campaign, org_id=org_id, role=role)
            finally:
                store.close()
            # Additive control-plane status (in-memory; build_raw stays DB-only).
            # Scoped to the caller's org so it never discloses another tenant's runs,
            # AND gated on the same permission the other RUN-carrying surfaces use.
            # panel.build_raw deliberately prunes a member's state down to CONFIG +
            # campaign stubs + MATCHES (a member sees leads, nothing else); bolting
            # RUN back on with only an ORG check handed that member the org's whole
            # run history and spend — the very thing /api/dashboard and
            # /api/campaigns (both `attach_run=True` behind view_dashboard /
            # view_campaigns) and /api/run/activity all 403 them for.
            if self.run_manager is not None and rbac.can(role, "view_dashboard"):
                raw["RUN"] = self.run_manager.status(org_id)
            body = _json_bytes(raw)
            self._send_json_body(200, body)
        except ValueError as e:  # malformed brief for the requested campaign
            self._send_json(400, False, error=str(e))
        except Exception:  # noqa: BLE001 — generic 500; detail to the log only
            self._send_internal_error("state build")

    def _serve_run_activity(self, query: str) -> None:
        """Live PROGRESS for one run, org-facing: aggregated counters, the redaction-safe
        scalars folded out of the narrative events, `finished`, the fleet job and the open
        health flags. Same auth gate as /api/run (run_campaigns). Ownership is proven
        in-memory by RunManager.status(org_id) when available, and durably by the
        org-stamped DB rows otherwise (so the feed survives a server restart). A
        foreign/unknown run is a 404 — never disclosed.

        v27: the narrative events themselves DO NOT leave this method. `events` stays in
        the payload (always empty) so the panel's poll contract and its `after`/cursor
        plumbing are unchanged, with `eventsRedacted` telling it why; the full feed lives
        behind the superadmin gate at ADMIN_RUN_ACTIVITY_PATH. The open `flags` stay:
        they drive the "fix your agent" UX and are a state, not a log."""
        user = self._current_user()
        if user is None:
            self._send_json(401, False, error="authentication required")
            return
        org_id = user.get("orgId")
        if org_id is None:
            self._send_json(403, False, error="account is not attached to an organization")
            return
        if not rbac.can(user.get("role"), "run_campaigns"):
            self._send_json(403, False, error="your role does not permit this action")
            return
        qs = parse_qs(query)
        run_id = (qs.get("runId") or [None])[0]
        if not run_id or not run_id.strip():
            self._send_json(400, False, error="runId is required")
            return
        run_id = run_id.strip()
        try:
            after = max(0, int((qs.get("after") or ["0"])[0]))
        except (ValueError, TypeError):
            after = 0
        # In-memory ownership (org-scoped): the active run or one of the org's recent.
        run_status = (self.run_manager.status(org_id) if self.run_manager is not None
                      else {"active": None, "recent": []})
        active = run_status.get("active")
        is_active_run = active is not None and active.get("id") == run_id
        owned_by_manager = is_active_run or any(
            r.get("id") == run_id for r in run_status.get("recent", []))
        try:
            store = Store(self.db_path)
            try:
                # When ownership is already proven in-memory the run's campaign may be
                # unregistered (org_id NULL on its rows), so don't double-filter or the
                # feed would read empty. Otherwise rely on org-stamped rows to gate.
                scope = None if owned_by_manager else org_id
                # v27: read from the START of the run, not from `after`. The events are
                # never returned — they are folded into scalars — and every one of those
                # aggregates is a property of the whole run, so paging them would make
                # the numbers a function of poll timing.
                events = store.fetch_run_events(
                    run_id, after_id=0, org_id=scope,
                    limit=RUN_ACTIVITY_AGGREGATE_EVENTS)
                sessions = store.sessions_for_run(run_id, org_id=scope)
                flags = store.run_events_open_flags(run_id, org_id=scope)
                actions = store.action_counts_for_run(run_id, org_id=scope)
                # The authoritative lead count once the run's rows exist (an in-process
                # run writes them live; a fleet run only at ack). Computed before the
                # ownership gate below purely to share this one connection — it is
                # discarded, never emitted, when that gate 404s.
                try:
                    lead_rows = len(store.matches_for_run(run_id))
                except Exception:  # noqa: BLE001 — the event estimate still stands
                    lead_rows = 0
                # FIX 2: fold the DB-derived fleet job into the SAME connection (one
                # open per poll, not two). Best-effort — a read hiccup just leaves
                # fleetJob None and never fails the feed.
                try:
                    job = store.get_job_for_run(run_id, org_id)
                    last_event_at = (store.last_event_at_for_run(run_id, org_id)
                                     if job is not None else None)
                except Exception:  # noqa: BLE001 — fleetJob is additive, never fatal
                    job, last_event_at = None, None
            finally:
                store.close()
        except Exception:  # noqa: BLE001 — generic 500; detail to the log only
            self._send_internal_error("run activity build")
            return
        # Unknown to this org in-memory AND no org-scoped rows → don't disclose it.
        #
        # A fleet JOB row counts as an org-scoped row. `store.get_job_for_run` is
        # itself org-filtered (BOLA guard), so `job is not None` proves the run is
        # this org's — while a foreign run still resolves to None and stays a 404,
        # so this is not an existence oracle. Without this the B6 failure surfacing
        # below was unreachable in exactly the case it was built for: a dispatched
        # run whose worker died before opening a session (its Chrome was
        # unattachable, say) emits ZERO events and ZERO sessions, so the operator
        # got a bare 404 for a run that really is theirs instead of the reason.
        if not owned_by_manager and not sessions and not events and job is None:
            self._send_json(404, False, error="unknown run")
            return
        # Finished = not the live run AND no session still open. Derived from the
        # durable sessions table so a mid-run server restart still reads correctly.
        has_running_session = any(
            s.get("status") == "running" or s.get("ended_at") is None for s in sessions)
        finished = not is_active_run and not has_running_session
        # FIX 2: expose the DB-derived fleet job (if this run is fleet-routed) so the
        # panel can keep polling a silent-but-alive worker run, and resolve `finished`
        # so the poller never stalls on a dead job nor cuts off before counters land.
        fleet_job = None
        target_leads: Optional[int] = None
        if job is not None:
            fleet_job = {
                "jobId": job["id"],
                "status": job["status"],
                "lastEventAt": last_event_at,
                "leaseExpiresAt": job.get("leaseExpiresAt"),
                # B6: the failure code the worker nacked with (or the engine summary's
                # halt_reason on an acked job), so a fleet run that died before it could
                # do anything — e.g. its Chrome was unattachable — reads as something
                # other than a blank red "Finished on the fleet". Worker-authored, so
                # re-cap it here; the panel renders it as text, never markup.
                "reason": _fleet_job_reason(job.get("result")),
                "attempts": job.get("attempts"),
                "maxAttempts": job.get("maxAttempts"),
            }
            finished = _fleet_run_finished(fleet_job, sessions, finished)
            # The plan-clamped target this run was dispatched with, so the panel can show
            # "7 of 10 leads" instead of a bare count. Only a fleet job carries it durably;
            # an in-process run's target reaches the panel in the POST /api/run response.
            spec_target = (job.get("spec") or {}).get("target_leads")
            if isinstance(spec_target, (int, float)) and spec_target > 0:
                target_leads = int(spec_target)
        counters = _aggregate_run_counters(sessions, actions)
        progress = _aggregate_run_progress(
            events, counters=counters, lead_rows=lead_rows, finished=finished,
            failed=bool(fleet_job and fleet_job["status"] in ("failed", "interrupted")))
        # Reconcile the session counters with the event-derived numbers rather than
        # shipping both: the aggregate is a max() over the same quantities, so it is
        # never smaller, and a payload that carried "0 reels scanned" next to
        # "searching · 40 scanned" would just make the panel pick a side.
        counters = {**counters, "reelsSeen": progress["itemsScanned"],
                    "relevancePasses": progress["relevantFound"],
                    "matches": progress["leadsFound"]}
        data = {
            "runId": run_id,
            "finished": finished,
            "fleetJob": fleet_job,
            "counters": counters,
            # v27: CONSTRUCTED scalars, never an event row with keys deleted — that
            # inverts the failure mode, and the next detail key an engine invents would
            # ship straight to the customer. No message, no detail, no session/campaign id.
            "phase": progress["phase"],
            "leadsFound": progress["leadsFound"],
            # E.5: what the run DISCOVERED and what actually REACHED the account, plus
            # the word that reconciles them. They converge on a healthy run; a
            # dead-lettered one never acks, so its leads are stranded on the worker and
            # `leadsDelivered` stays 0 while `leadsFound` keeps the estimate. Rendering
            # either number alone beside this run's (correctly banked) spend would be a
            # lie in one direction or the other.
            "leadsDelivered": progress["leadsDelivered"],
            "delivery": progress["delivery"],
            "itemsScanned": progress["itemsScanned"],
            "relevantFound": progress["relevantFound"],
            "lastEventAt": progress["lastEventAt"],
            "targetLeads": target_leads,
            # Always empty for an org caller — the key (and the cursor with it) stays so
            # the panel's poll contract is unchanged. The real feed is superadmin-only.
            "events": [],
            "eventsRedacted": True,
            # `detail` is the flag's ENGINE-AUTHORED prose and is never customer-safe:
            # youtube/telegram raise `parse_skip` with f"comment {comment_id}", where
            # that id is f"{reel_id}/{comment_id}" — a permalink. Redacting `events`
            # here while shipping this verbatim left the same disclosure open under a
            # different key, which is precisely how it survived the v27 sweep. The
            # superadmin feed (`_handle_admin_run_activity`) still carries the raw
            # detail, because that is the plane an operator debugs a flag from.
            "flags": [{"kind": f.get("kind"), "severity": f.get("severity"),
                       "detail": org_flag_summary(f.get("kind") or "")}
                      for f in flags],
            # Never advances: there is nothing to page. Kept so a client that echoes it
            # back keeps working, and so `after` stays a no-op rather than a 400.
            "cursor": after,
        }
        self._send_json(200, True, data=data)

    def _maps_to_existing_file(self, path: str) -> bool:
        # Mirror SimpleHTTPRequestHandler's path→filesystem mapping, then check
        # the target is a file inside panel_dir (translate_path already guards
        # traversal and strips the query string).
        target = Path(self.translate_path(path))
        return target.is_file()


def serve(db_path: str, panel_dir: str, config_dir: str,
          host: str = "127.0.0.1", port: int = 8765,
          run_manager: Optional[RunManager] = None,
          schedule_manager: Optional["ScheduleManager"] = None,
          reclaim_manager: Optional["ReclaimManager"] = None,
          session_watchdog: Optional["SessionWatchdog"] = None,
          billing_providers: Optional[dict] = None,
          readiness_probe: Optional[Callable[..., dict]] = None,
          login_opener: Optional[Callable[[], bool]] = None) -> ThreadingHTTPServer:
    configure_logging()  # idempotent: a no-op if the CLI already configured it
    # BIND FIRST — before touching the DB or starting a single daemon thread.
    # Binding used to be the LAST statement, so starting on a busy port (the common
    # case: a stale bridge from an unclean exit) raised a bare OSError(EADDRINUSE)
    # out of the CLI as a raw traceback — after the run manager had already migrated
    # a brand-new SQLite file into existence, leaving a stray DB behind for a process
    # that never served a byte. Failing at the very first side effect leaves nothing.
    try:
        httpd = ThreadingHTTPServer((host, port), PanelHandler)
    except OSError as e:
        if e.errno in (errno.EADDRINUSE, errno.EACCES):
            raise PortInUseError(
                f"{host}:{port} is already in use — another aizu panel is probably "
                f"running; stop it or pass --port") from None
        raise
    try:
        _configure_and_start(httpd, db_path, panel_dir, config_dir,
                             run_manager=run_manager, schedule_manager=schedule_manager,
                             reclaim_manager=reclaim_manager,
                             session_watchdog=session_watchdog,
                             billing_providers=billing_providers,
                             readiness_probe=readiness_probe, login_opener=login_opener)
    except BaseException:
        httpd.server_close()   # never leak the listening socket on a failed setup
        raise
    return httpd


def _configure_and_start(httpd: ThreadingHTTPServer, db_path: str, panel_dir: str,
                         config_dir: str, *,
                         run_manager: Optional[RunManager],
                         schedule_manager: Optional["ScheduleManager"],
                         reclaim_manager: Optional["ReclaimManager"],
                         session_watchdog: Optional["SessionWatchdog"],
                         billing_providers: Optional[dict],
                         readiness_probe: Optional[Callable[..., dict]],
                         login_opener: Optional[Callable[[], bool]]) -> None:
    """Everything `serve()` does AFTER the socket is bound: wire the handler class,
    reconcile orphan state, and start the background daemons. Split out only so the
    bind can happen first — see the comment at the call site."""
    PanelHandler.panel_dir = str(Path(panel_dir).resolve())
    PanelHandler.db_path = str(Path(db_path).resolve())
    PanelHandler.config_dir = str(Path(config_dir).resolve())
    PanelHandler.login_throttle = LoginThrottle()
    PanelHandler.invite_throttle = InviteThrottle()
    # `run_manager is None` is the production default path; tests always inject a
    # manager. The schedule daemon is auto-started ONLY on that default path (or when
    # a schedule_manager is passed explicitly) — so existing server-test fixtures,
    # which inject a run_manager but no schedule_manager, never spawn a DB-touching
    # daemon thread.
    default_path = run_manager is None
    if run_manager is None:
        # engine_root (parent of the aizu package) is the cwd for `-m
        # aizu.cli`; per-run logs land under engine/run-logs/.
        engine_root = Path(__file__).resolve().parent.parent
        run_manager = RunManager(
            db_path=PanelHandler.db_path, config_dir=PanelHandler.config_dir,
            engine_root=engine_root, log_dir=engine_root / "run-logs")
    PanelHandler.run_manager = run_manager
    # v12: a restart loses the in-memory active slot but a paused child's pause
    # sentinel survives on disk — sweep orphans so a fresh process can't leave a
    # zombie frozen on a file no live code removes.
    run_manager.sweep_orphan_pause_files()
    # A crashed/killed in-process run can leave its session row stuck at 'running'
    # (ended_at NULL) forever, which the panel renders as a permanent, unstoppable
    # "Running" badge. The active-run slot is in-memory, so a restart means any such
    # row is an orphan — reconcile it to a terminal state. Gated on default_path
    # (like the scheduler/reclaim daemons) so injected-run_manager test fixtures get
    # no surprise DB writes. A fresh Store is used and closed here.
    if default_path:
        _reconcile_store = Store(PanelHandler.db_path)
        try:
            _n = _reconcile_store.reconcile_orphan_sessions()
        finally:
            _reconcile_store.close()
        if _n:
            log.info("Reconciled %d orphaned 'running' session(s) at startup", _n)
    if schedule_manager is None and default_path:
        from .scheduler import ScheduleManager as _ScheduleManager
        schedule_manager = _ScheduleManager(
            db_path=PanelHandler.db_path, run_manager=run_manager)
    if schedule_manager is not None:
        schedule_manager.start()
    # v14 Phase 4: the distributed-jobs reclaim daemon. Auto-started ONLY on the default
    # production path (or when explicitly injected) — so server test fixtures that inject
    # a run_manager but no reclaim_manager never spawn a DB-touching sweep thread.
    if reclaim_manager is None and default_path:
        from .reclaim_manager import ReclaimManager as _ReclaimManager
        reclaim_manager = _ReclaimManager(db_path=PanelHandler.db_path)
    if reclaim_manager is not None:
        reclaim_manager.start()
    # Hang-prevention fix #4: the session-liveness watchdog. Same default-path
    # gating as the reclaim daemon above — auto-started only in production (or
    # when explicitly injected), so injected-run_manager test fixtures never
    # spawn a surprise DB-touching thread.
    if session_watchdog is None and default_path:
        from .session_watchdog import SessionWatchdog as _SessionWatchdog
        session_watchdog = _SessionWatchdog(
            db_path=PanelHandler.db_path, run_manager=run_manager)
    if session_watchdog is not None:
        session_watchdog.start()
    # staticmethod(): a plain function parked on the handler CLASS would be bound on
    # attribute access and get `self` as its first positional argument. The other
    # injected collaborators are instances, so this trap is unique to these two.
    PanelHandler.readiness_probe = (
        staticmethod(readiness_probe) if readiness_probe is not None else None)
    PanelHandler.login_opener = (
        staticmethod(login_opener) if login_opener is not None else None)
    if PanelHandler.telegram_login is None:
        PanelHandler.telegram_login = TelegramLoginManager()
    # Billing providers: injected (tests) or built from env. A missing/invalid
    # Polar config is NOT fatal — billing routes 503 until it is configured, so the
    # rest of the panel still serves (mirrors the run-control-disabled path).
    if billing_providers is not None:
        PanelHandler.billing_providers = billing_providers
    else:
        try:
            PanelHandler.billing_providers = {"polar": billing.PolarClient.from_env()}
        except billing.BillingConfigError as e:
            log.warning("Billing disabled — Polar not configured: %s", e)
            PanelHandler.billing_providers = {}
