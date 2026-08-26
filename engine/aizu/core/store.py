"""SQLite store — the only contract between engine and admin panel (PRD §3, §7, §8).

WAL mode so the panel can read while the engine writes. Two processes meet here
and nowhere else; the panel never calls the engine.

Design rules enforced here:
- Writes are idempotent on `comment_id` — a re-poll never overwrites human
  `status` (PRD §7: "status keyed on comment_id; survives re-scrapes").
- `seen_reels` is a forward-only watermark for dedupe.
- Killed mid-run resumes from persisted state.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import secrets
import sqlite3
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any, Iterator, Optional, Sequence

from ..auth import hash_session_token
from . import accounts as accounts_lib
from . import warmth as warmth_lib
from .logsetup import get_logger

if TYPE_CHECKING:
    from ..secrets import SecretCipher

logger = get_logger(__name__)

SCHEMA_VERSION = 28  # v2: platform dimension; v3: panel ops tables; v4: campaign_briefs; v5: auth; v6: lead Kanban (status set + audit log + notes); v7: multi-tenancy (organizations + memberships role + invites; per-org settings/integrations); v8: encrypted per-(org, platform) integration secrets; v9: security audit log; v10: run_events live activity feed; v11: account warming (accounts + state_changes + campaign_accounts + account_secrets; sessions.engine_mode/account_id, health_flags.account_id, actions.account_id); v12: campaign lifecycle controls (campaign_meta.archived_at/paused_reason + fixed-cadence schedule cols: schedule_enabled/kind/dow/hour/minute/tz, next_run_at, last_scheduled_run_at, schedule_target_leads, schedule_duration_minutes); v13: billing subscriptions (Polar + provider-agnostic, soft run-cap); v14: distributed workers pool (token-based registry + presence; status DERIVED not stored); v15: superadmin plane (platform_admins + platform_admin_sessions with impersonation principal + hash-chained admin_audit_log + DB-backed admin_login_throttle); v16: platform_settings (superadmin execution_backend switch — route runs to in-process RunManager vs distributed worker fleet); v17: model comparison (superadmin-switchable LLM fan-out — matches.found_by_models + model_comparison_log; platform_settings.model_comparison_enabled); v18: Uzbek-only local STT transcript (seen_reels.transcript/transcript_lang/transcript_ms; sessions.transcriptions); v19: video-analysis tier (seen_reels.video_analyzed/video_analysis_summary; sessions.video_analyses); v20: session liveness heartbeat (sessions.last_activity_at/pid) so SessionWatchdog can detect a wedged-but-never-excepting session; v21: self-healing anti-bot cooldown (session_cooldowns: per-(campaign_id, platform) attempt counter + exponential-backoff cooldown_until for a SOFT halt, gap #1); v22: per-worker enrolment tokens (worker_enrolment_tokens: single-use, admin-minted, server-assigned org/pool scope for worker enrolment, closing gap B8 — shared bootstrap token could self-declare pool-wide capability); v23: worker launch preflight (workers.preflight_json — the box's own self-check summary, carried on register/heartbeat and surfaced in the fleet console so a box that is online-but-cannot-work says WHY, ledger F9/F10/F12); v24: Campaign Lab per-source attribution (source_stats: one row per (campaign, platform, seed) carrying navigations/yield/redirect/dead verdicts + park/ban lifecycle; seen_reels.source and matches.source so "which seed produced this lead" is finally answerable — Remedy Sheet #1/D); v25: Campaign Lab seed mining (seen_reels.author_id — the author's STABLE, seed-shaped id (YouTube UC-id, LinkedIn canonical profile URL, Telegram @channel) alongside the display name, so mining our own leads yields actionable seeds rather than strings that break on a rename — Remedy Sheet #2/A); v26: Campaign Lab negative capture (eval_candidates — a sampled, labellable record of comments the match gate REJECTED, which the engine paid a model call for and then threw away; plus matches.confidence/raw. Without it a gold set cannot be built at all: the DB held 2 accepted comments and zero rejects — Remedy Sheet #3/E); v27: lead-intent redaction (matches.intent — the customer-facing one-line summary of what the commenter wants, so the panel can hide username/comment); v28: opaque org-facing lead key (matches.lead_token — a random per-lead token that org payloads ship as `commentId` in place of the platform's own id. The real comment_id is a PERMALINK on four of six platforms — reddit/youtube/telegram compose it as "{reel_id}/{comment_id}" and x uses the reply's tweet id — so shipping it left the whole v27 redaction one hand-built URL from being undone. Every org-scoped lead write now resolves this token server-side)


def _now_iso() -> str:
    """UTC ISO-8601 timestamp — matches runner._now_iso for human-readable trails."""
    return datetime.now(timezone.utc).isoformat()


def _iso_from_epoch(ts: float) -> str:
    """Epoch seconds → the SAME UTC ISO-8601 shape `_now_iso` writes.

    Every other time column in this schema is a REAL epoch; `audit_log.created_at`
    is TEXT because that trail is meant to be readable without a converter. So a
    range query over it needs its bound in that text shape — and it has to come
    from this one place, because a bound built any other way (a naive datetime, a
    `Z` suffix, a different microsecond convention) would compare lexicographically
    against a different alphabet and silently select the wrong window.
    """
    return datetime.fromtimestamp(ts, timezone.utc).isoformat()

# The lead Kanban pipeline (v6). Engine snake_case keys; the panel maps to labels
# New / In Progress / Interested / Closed / Couldn't Connect / Archived.
# 'needs_review' (gap #4) is a deferred-outcome status distinct from 'new': the
# corroboration gate (Campaign.require_corroboration, off by default) routes a
# match here instead of a hard accept when a comparison model disagreed with or
# was inconclusive about the primary verdict. It behaves like any other status
# for triage (an operator moves it on via the normal set_status path) — it just
# is never assigned by set_status itself, only by upsert_match on first insert.
VALID_STATUS = {"new", "in_progress", "interested", "closed", "couldnt_connect",
                "archived", "needs_review"}

# Moving a lead INTO one of these requires a non-empty reason note (enforced here,
# not just in the UI) — they are the "negative/terminal" outcomes.
FORCED_REASON_STATUS = {"closed", "couldnt_connect", "archived"}

# Statuses that count as a successful lead for CPL / pipeline win-rate. One tunable:
# flip to {"closed"} for "won = closed only". Legacy 'confirmed' migrates to
# 'interested', so keeping it here preserves historical CPL continuity.
WIN_STATUS = {"interested", "closed"}

# v5→v6 value remap, applied once in the migration (a value change, not a schema
# change — a plain UPDATE, not the legacy rename-dance). 'new' stays 'new';
# 'closed'/'couldnt_connect' have no legacy source.
_STATUS_V6_REMAP = {
    "reviewing": "in_progress",
    "confirmed": "interested",
    "discarded": "archived",
}

# Defensive cap on a free-form note / status reason body (chars), on top of the
# server's 64 KB request cap.
MAX_NOTE_LENGTH = 4000

DEFAULT_PLATFORM = "instagram"

# Gap #1 self-healing cooldown: exponential backoff for a SOFT halt (action_block/
# canary — rate-limit-shaped, see engines/base.py's SOFT_HALT_KINDS), mirroring the
# reference design (services/analyzer/src/haunter/browse.ts's flagHardSignal): base
# 15 minutes, doubling per consecutive soft halt on the SAME (campaign_id,
# platform), capped at 6 hours. See Store.record_soft_halt.
COOLDOWN_BASE_SECONDS = 15 * 60
COOLDOWN_MAX_SECONDS = 6 * 60 * 60

# Asia/Tashkent is a fixed UTC+5 — shift epoch timestamps before bucketing so
# day/hour groups line up with the labels panel.py produces (see panel.TASHKENT).
TZ_SQL_SHIFT = "+5 hours"
# Same fixed offset as a tzinfo, for Python-side month bucketing (the Free-tier
# billing-period fallback). Keeps the cap window aligned with the dashboards.
_TASHKENT_TZ = timezone(timedelta(hours=5))

VALID_CAMPAIGN_STATUS = {"live", "paused", "draft", "ended"}

# v12: a campaign is "runnable" (eligible to start a live run — by the scheduler,
# the CLI run-all batch, or the panel) iff it is live AND not archived. This single
# SQL fragment is the source of truth; cli._live_campaigns, the scheduler's due
# query, and the React selectors.isRunnable must all agree with it.
RUNNABLE_SQL_PREDICATE = "status='live' AND archived_at IS NULL"

# v12 paused_reason precedence. A system 'auto' pause (budget/health) outranks a
# 'user' pause — an operator resume (reason='user') must NOT silently clear a
# halt the engine imposed for cause. Higher rank = harder to clear.
_PAUSED_REASON_RANK = {"user": 0, "auto": 1}

# Substrings that flag a credential mistakenly routed to the plaintext
# `integrations.detail` column instead of the encrypted secret store. Real
# secrets must go through set_integration_secret (Fernet-encrypted).
_SECRET_DETAIL_MARKERS = ("api_key", "api_hash", "session", "token")


def _time_filter(col: str, since_ts: Optional[float], until_ts: Optional[float],
                 args: list[Any]) -> str:
    """Append inclusive-since / exclusive-until predicates to `args`; return SQL."""
    sql = ""
    if since_ts is not None:
        sql += f" AND {col}>=?"
        args.append(since_ts)
    if until_ts is not None:
        sql += f" AND {col}<?"
        args.append(until_ts)
    return sql

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT
);

-- Match record (PRD §8). One row per comment per (campaign, platform).
-- `platform` is part of the key: one campaign brief can be fanned across
-- platforms and pooled in one dashboard (multi-platform plan Part B), and a
-- comment_id is only unique within its own platform's id namespace.
CREATE TABLE IF NOT EXISTS matches (
    campaign_id TEXT NOT NULL,
    org_id      INTEGER,                    -- v7: owning org (stamped from the campaign)
    platform    TEXT NOT NULL DEFAULT 'instagram',
    reel_id     TEXT NOT NULL,
    comment_id  TEXT NOT NULL,
    session_id  TEXT,                       -- session that captured it (provenance)
    username    TEXT,
    text        TEXT,
    lang        TEXT,
    score       REAL,
    reason      TEXT,
    extracted   TEXT,                      -- brief-defined JSON blob
    status      TEXT NOT NULL DEFAULT 'new',
    tier        TEXT,                       -- which model tier decided (local/cloud)
    captured_at REAL NOT NULL,
    updated_at  REAL NOT NULL,
    source      TEXT,                       -- v24: seed term whose page produced this lead
    intent      TEXT,                       -- v27: customer-facing intent summary
    -- v28: the OPAQUE org-facing lead key. Random, carries no platform data, and it
    -- is what an org-facing payload ships as `commentId` in place of the real one.
    -- The real `comment_id` is a permalink on four of six platforms (reddit/youtube/
    -- telegram compose it as "{reel_id}/{comment_id}"; x uses the reply's own tweet
    -- id), so shipping it handed an org the very comment the v27 redaction withholds.
    lead_token  TEXT,
    PRIMARY KEY (campaign_id, platform, comment_id)
);
CREATE INDEX IF NOT EXISTS idx_matches_reel   ON matches(campaign_id, platform, reel_id);
CREATE INDEX IF NOT EXISTS idx_matches_status ON matches(campaign_id, platform, status);

-- Dedupe watermark. Forward-only.
CREATE TABLE IF NOT EXISTS seen_reels (
    campaign_id TEXT NOT NULL,
    platform    TEXT NOT NULL DEFAULT 'instagram',
    reel_id     TEXT NOT NULL,
    first_seen  REAL NOT NULL,
    last_seen   REAL NOT NULL,
    relevant    INTEGER,                    -- 1/0/NULL (NULL = not yet gated)
    author      TEXT,
    caption     TEXT,
    ocr_text    TEXT,                       -- on-screen text read by vision
    transcript      TEXT,                   -- v18: STT transcript (Uzbek-only, gated)
    transcript_lang TEXT,                   -- v18: 'uz' when transcript is set, else NULL
    transcript_ms   INTEGER,                -- v18: reserved for future audio-duration tracking
    video_analyzed  INTEGER,                -- v19: 1 when the video-analysis tier ran, else NULL
    video_analysis_summary TEXT,            -- v19: compact JSON of the fusion verdict extras
    source          TEXT,                   -- v24: seed term this item was intercepted on ('' / NULL = unattributed)
    author_id       TEXT,                   -- v25: author's stable, seed-shaped id (NULL = platform exposes none)
    PRIMARY KEY (campaign_id, platform, reel_id)
);

-- Per-reel comment cursor: "new comments since last poll."
CREATE TABLE IF NOT EXISTS comment_cursors (
    campaign_id  TEXT NOT NULL,
    platform     TEXT NOT NULL DEFAULT 'instagram',
    reel_id      TEXT NOT NULL,
    last_cursor  TEXT,                      -- opaque cursor from interception
    last_polled  REAL,
    PRIMARY KEY (campaign_id, platform, reel_id)
);

-- Match-rich reels re-polled until aged out (~7-14 days).
CREATE TABLE IF NOT EXISTS watchlist (
    campaign_id TEXT NOT NULL,
    platform    TEXT NOT NULL DEFAULT 'instagram',
    reel_id     TEXT NOT NULL,
    added_at    REAL NOT NULL,
    expires_at  REAL NOT NULL,
    match_count INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (campaign_id, platform, reel_id)
);

-- One row per session run. Counters per PRD §7.
CREATE TABLE IF NOT EXISTS sessions (
    session_id      TEXT PRIMARY KEY,
    campaign_id     TEXT NOT NULL,
    platform        TEXT NOT NULL DEFAULT 'instagram',
    started_at      REAL NOT NULL,
    ended_at        REAL,
    status          TEXT NOT NULL DEFAULT 'running',  -- running/completed/halted
    halt_reason     TEXT,
    reels_seen      INTEGER NOT NULL DEFAULT 0,
    already_seen_skips INTEGER NOT NULL DEFAULT 0,
    relevance_passes   INTEGER NOT NULL DEFAULT 0,
    comments_scored INTEGER NOT NULL DEFAULT 0,
    matches         INTEGER NOT NULL DEFAULT 0,
    escalations     INTEGER NOT NULL DEFAULT 0,
    transcriptions  INTEGER NOT NULL DEFAULT 0,  -- v18: reels sent through Uzbek STT
    video_analyses  INTEGER NOT NULL DEFAULT 0,  -- v19: reels sent through video-analysis
    spend_usd       REAL NOT NULL DEFAULT 0.0,
    feed_health_flag INTEGER NOT NULL DEFAULT 0,
    run_id          TEXT,                            -- v10: correlates to the RunManager run that spawned this session
    org_id          INTEGER,                         -- v10: owning org (run-activity scoping)
    last_activity_at REAL,                           -- v20: heartbeat, bumped by update_counters; seeded from started_at
    pid             INTEGER                          -- v20: os.getpid() of the process that started this session
);

-- Account health / canary flags (PRD §7, §9).
CREATE TABLE IF NOT EXISTS health_flags (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    campaign_id TEXT,
    org_id      INTEGER,                    -- v7: owning org (NULL for system-wide flags)
    session_id  TEXT,
    kind        TEXT NOT NULL,              -- feed_health / checkpoint / empty_interception / spend_cap / cloud_degraded
    severity    TEXT NOT NULL,              -- soft / halt
    detail      TEXT,
    created_at  REAL NOT NULL,
    resolved_at REAL
);

-- v21: self-healing anti-bot cooldown (gap #1). A SOFT halt (rate-limit-shaped —
-- action_block / canary, see engines/base.py's SOFT_HALT_KINDS) escalates this row
-- instead of only raising a health_flag a human must resolve: `attempt` counts
-- consecutive soft halts and `cooldown_until` is an exponential-backoff deadline
-- (Store.record_soft_halt: COOLDOWN_BASE_SECONDS * 2**(attempt-1), capped at
-- COOLDOWN_MAX_SECONDS). cli._run_one checks this BEFORE touching the browser/
-- account again, so a run attempt made while still cooling down short-circuits
-- cleanly and the very next attempt after cooldown_until proceeds normally — no
-- resolve_flag, no human step. One row per (campaign_id, platform); account_id is
-- denormalised (nullable) for warming-pool joins, mirroring health_flags. Purely
-- SQLite-backed, so a process restart rehydrates by simply reading this row.
-- HARD halts (checkpoint/login) never touch this table — they stay human-gated
-- via raise_flag/resolve_flag exactly as before.
CREATE TABLE IF NOT EXISTS session_cooldowns (
    campaign_id    TEXT NOT NULL,
    platform       TEXT NOT NULL,
    account_id     INTEGER,
    attempt        INTEGER NOT NULL DEFAULT 0,
    cooldown_until REAL,
    last_kind      TEXT,
    updated_at     REAL NOT NULL,
    PRIMARY KEY (campaign_id, platform)
);

-- Per-call cloud spend log (PRD §6).
CREATE TABLE IF NOT EXISTS spend_log (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    campaign_id TEXT NOT NULL,
    session_id  TEXT,
    stage       TEXT NOT NULL,              -- relevance / match / vision / transcribe
    model       TEXT,
    usd         REAL NOT NULL,
    created_at  REAL NOT NULL
);

-- Engagement actions (like/follow). Opt-in; read-only campaigns write nothing.
CREATE TABLE IF NOT EXISTS actions (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    campaign_id TEXT NOT NULL,
    session_id  TEXT,
    reel_id     TEXT,
    action_type TEXT NOT NULL,              -- like / follow
    target      TEXT,                       -- author handle (follow) or reel id (like)
    succeeded   INTEGER NOT NULL DEFAULT 0,
    created_at  REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_actions_session ON actions(session_id);

-- ----- v3: admin-panel ops surfaces -----
-- Editable metadata overlaid on the markdown campaign brief. The brief stays the
-- source of truth for matching logic; this only carries display/ops fields the
-- panel edits (status, budget, goal). Rows are sparse — a campaign with no row
-- falls back to brief-derived defaults in panel.py.
CREATE TABLE IF NOT EXISTS campaign_meta (
    campaign_id  TEXT PRIMARY KEY,
    org_id       INTEGER,                       -- v7: owning org (campaign→org registry)
    display_name TEXT,
    status       TEXT NOT NULL DEFAULT 'live',   -- live / paused / draft / ended
    budget_cap   REAL,                            -- USD cap for the budget bar (nullable)
    goal_target  INTEGER,                         -- monthly lead target for the gauge (nullable)
    -- v12 lifecycle controls. archive is a reversible timestamp dimension (NOT a
    -- status); a non-null archived_at hides the campaign and bars it from any run.
    archived_at  REAL,                            -- v12: non-null = archived (reversible)
    paused_reason TEXT,                           -- v12: 'user' (operator) | 'auto' (system); precedence guards resume
    -- v12 fixed-cadence schedule (no cron string — daily/weekdays/weekly only).
    schedule_enabled  INTEGER NOT NULL DEFAULT 0, -- 1 = recurring schedule armed
    schedule_kind     TEXT NOT NULL DEFAULT '',   -- 'daily' | 'weekdays' | 'weekly'
    schedule_dow      INTEGER,                     -- 0-6 Mon-Sun (weekly only)
    schedule_hour     INTEGER,                     -- 0-23 local (Asia/Tashkent)
    schedule_minute   INTEGER,                     -- 0-59
    schedule_tz       TEXT NOT NULL DEFAULT 'Asia/Tashkent',
    next_run_at       REAL,                        -- epoch of the next scheduled fire
    last_scheduled_run_at REAL,                    -- epoch of the last fire the scheduler launched
    schedule_target_leads INTEGER,                 -- per-schedule lead cap (defaults to goal_target)
    schedule_duration_minutes INTEGER,             -- per-schedule safety time cap
    created_at   REAL NOT NULL,
    updated_at   REAL NOT NULL
);

-- Workspace team members. Panel-only CRUD; the engine never writes these.
CREATE TABLE IF NOT EXISTS team_members (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    name       TEXT NOT NULL,
    email      TEXT NOT NULL UNIQUE,
    role       TEXT NOT NULL DEFAULT 'member',    -- owner / admin / member / viewer
    initials   TEXT,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);

-- Editable workspace settings overlay (same key/value idiom as `meta`, but
-- panel-writable and JSON-encoded). Overrides the hardcoded CONFIG defaults.
CREATE TABLE IF NOT EXISTS settings (
    org_id     INTEGER NOT NULL,                  -- v7: settings are per-org
    key        TEXT NOT NULL,
    value      TEXT,                              -- JSON-encoded scalar/object
    updated_at REAL NOT NULL,
    PRIMARY KEY (org_id, key)
);

-- Per-platform integration connection state (Settings > Integrations). Overlays
-- the health-derived baseline in panel.py.
CREATE TABLE IF NOT EXISTS integrations (
    org_id     INTEGER NOT NULL,                  -- v7: integrations are per-org
    platform   TEXT NOT NULL,                     -- instagram / youtube / telegram
    connected  INTEGER NOT NULL DEFAULT 0,
    detail     TEXT,
    updated_at REAL NOT NULL,
    PRIMARY KEY (org_id, platform)
);

-- ----- v8: encrypted per-(org, platform) integration credentials -----
-- The connection STATE lives in `integrations` (plaintext, panel-readable). The
-- SECRET (YT api_key; TG api_id/api_hash/session) lives here as a Fernet blob —
-- the only place a credential is persisted. JSON shape evolves without migration;
-- decryption is keyed by AIZU_SECRET_KEY (see aizu.secrets).
CREATE TABLE IF NOT EXISTS integration_secrets (
    org_id      INTEGER NOT NULL,
    platform    TEXT NOT NULL,
    secret_blob TEXT NOT NULL,                   -- Fernet token of a JSON dict
    updated_at  REAL NOT NULL,
    PRIMARY KEY (org_id, platform)
);

-- ----- v4: panel-authored runnable briefs -----
-- The full matching brief authored from the panel, stored as one JSON blob so its
-- shape can evolve without a migration. `config.campaign_from_brief` builds a
-- Campaign from it and `aizu run --campaign <id>` executes it instead of the
-- file-based config/campaign.md. campaign_meta holds lifecycle/ops; this holds the
-- logic. A campaign with a row here is runnable; meta-only rows are drafts.
CREATE TABLE IF NOT EXISTS campaign_briefs (
    campaign_id TEXT PRIMARY KEY,
    org_id      INTEGER,                        -- v7: owning org (campaign→org registry)
    brief       TEXT NOT NULL,                  -- JSON: platform, goal, threshold, language_mix, relevance/match/extract, seeds
    updated_at  REAL NOT NULL
);

-- ----- v5: panel auth (email + password) -----
-- One row per panel user. Email is the unique login identity (stored lowercased).
-- password_hash is a self-describing PBKDF2 string (see auth.hash_password); the
-- engine never reads these — auth lives entirely in the bridge server.
CREATE TABLE IF NOT EXISTS users (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    email         TEXT NOT NULL UNIQUE,         -- globally unique: one account = one company
    password_hash TEXT NOT NULL,
    org_id        INTEGER,                       -- v7: the company this account belongs to
    role          TEXT NOT NULL DEFAULT 'owner', -- v7: owner / admin / member / viewer
    created_at    REAL NOT NULL,
    updated_at    REAL NOT NULL
);

-- Server-side session store. The client holds only the opaque `token` (in an
-- HttpOnly cookie); the row is the source of truth and is deletable on logout.
-- ON DELETE CASCADE so removing a user drops their sessions (foreign_keys=ON).
CREATE TABLE IF NOT EXISTS auth_sessions (
    token      TEXT PRIMARY KEY,
    user_id    INTEGER NOT NULL,
    created_at REAL NOT NULL,
    expires_at REAL NOT NULL,
    FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_auth_sessions_user ON auth_sessions(user_id);

-- ----- v6: lead Kanban audit log + free-form notes -----
-- One row per status transition. IMMUTABLE: never updated or deleted, even when a
-- linked note is removed — the audit trail must survive. user_id/user_email are
-- denormalised (no FK to users) so the log reads standalone after an account is
-- deleted, matching the health_flags/spend_log convention.
CREATE TABLE IF NOT EXISTS lead_status_changes (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    campaign_id TEXT NOT NULL,
    platform    TEXT NOT NULL DEFAULT 'instagram',
    comment_id  TEXT NOT NULL,
    from_status TEXT,                          -- NULL only if the row had no prior status
    to_status   TEXT NOT NULL,
    user_id     INTEGER,                       -- actor (denormalised, no FK)
    user_email  TEXT,
    reason      TEXT,                          -- optional; required into FORCED_REASON_STATUS
    created_at  REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_lsc_lead
    ON lead_status_changes(campaign_id, platform, comment_id, created_at);
CREATE INDEX IF NOT EXISTS idx_lsc_user_time
    ON lead_status_changes(campaign_id, user_id, created_at);

-- Free-form notes per lead. Any authed user ADDs; only the AUTHOR DELETEs. Hard
-- delete (no soft-delete needed for v1). author_id is a soft link (no FK); display
-- survives user deletion via author_email.
CREATE TABLE IF NOT EXISTS lead_notes (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    campaign_id  TEXT NOT NULL,
    platform     TEXT NOT NULL DEFAULT 'instagram',
    comment_id   TEXT NOT NULL,
    author_id    INTEGER,
    author_email TEXT,
    body         TEXT NOT NULL,
    created_at   REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_lead_notes_lead
    ON lead_notes(campaign_id, platform, comment_id, created_at);

-- ----- v7: multi-tenancy -----
-- One row per company/tenant. Created at signup (company name required; logo +
-- description optional). Every campaign, lead, setting and user belongs to exactly
-- one org. `created_by_user_id` is a soft link (no FK) to the signing-up owner.
CREATE TABLE IF NOT EXISTS organizations (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    name               TEXT NOT NULL,             -- company name (required)
    logo               TEXT,                      -- optional URL / data-uri
    description         TEXT,                      -- optional short blurb
    created_by_user_id INTEGER,
    created_at         REAL NOT NULL,
    updated_at         REAL NOT NULL
);

-- Pending team invites (the copy-link path). The raw token lives only in the link
-- the inviter shares; we store its SHA-256 (same idiom as auth_sessions). A row is
-- pending until `accepted_at` is set or it expires. Direct-add (email+password)
-- creates a user immediately and writes NO invite row.
CREATE TABLE IF NOT EXISTS invites (
    token_hash         TEXT PRIMARY KEY,          -- SHA-256 of the raw invite token
    org_id             INTEGER NOT NULL,
    email              TEXT,                      -- optional: pre-fill / restrict the invite
    role               TEXT NOT NULL,             -- admin / member / viewer (never owner)
    invited_by_user_id INTEGER,
    created_at         REAL NOT NULL,
    expires_at         REAL NOT NULL,
    accepted_at        REAL,                      -- NULL while pending
    FOREIGN KEY (org_id) REFERENCES organizations(id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_invites_org ON invites(org_id, accepted_at);

-- ----- v9: security audit log -----
-- One row per security-relevant action (role change, member add/remove, invite
-- create/accept, integration connect/disconnect). IMMUTABLE: insert-only, never
-- updated or deleted. org_id scopes reads; actor_user_id/target/detail are
-- denormalised (no FK) so the trail reads standalone after an account is deleted,
-- matching the lead_status_changes / health_flags / spend_log convention. Purely
-- additive (CREATE TABLE IF NOT EXISTS) — existing DBs gain it on open, no migration.
CREATE TABLE IF NOT EXISTS audit_log (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    org_id        INTEGER NOT NULL,
    actor_user_id INTEGER,                       -- actor (denormalised, no FK)
    action        TEXT NOT NULL,                 -- role_changed / member_added / member_removed / invite_created / invite_accepted / integration_connected / integration_disconnected
    target        TEXT,                          -- the object acted on (e.g. platform, user id)
    detail        TEXT,                          -- optional JSON blob (e.g. {"from","to"})
    created_at    TEXT NOT NULL                  -- ISO-8601 UTC (human-readable trail)
);
CREATE INDEX IF NOT EXISTS idx_audit_log_org ON audit_log(org_id, id);
-- v27: the reveal METER reads this table on every /api/lead/reveal call
-- (COUNT(DISTINCT target) for one org's `reveal_lead` rows inside the billing
-- period), so give that predicate its own index instead of walking the org's whole
-- history. `target` rides along to keep the count covering. No schema bump needed:
-- CREATE INDEX IF NOT EXISTS runs on every open, so an existing DB gains it there.
CREATE INDEX IF NOT EXISTS idx_audit_log_action
    ON audit_log(org_id, action, created_at, target);
-- NOTE: org_id indexes on users/matches/campaign_meta are created in _init_schema
-- AFTER the v7 migration, because on an upgrading DB those columns are added by
-- ADD COLUMN (which runs after this executescript) — see _init_schema tail.

-- ----- v10: live run activity feed -----
-- Append-only narrative of what a run did, step by step (the panel "live activity"
-- drawer). Correlated to an in-flight RunManager run via run_id (passed to the
-- engine subprocess by env) and to a Session via session_id. `seq` is a per-run
-- monotonic cursor the panel pages on (after=<seq>); `id` gives global insertion
-- order. `detail` is an optional JSON blob whose shape evolves without migration.
-- IMMUTABLE: insert-only, never updated/deleted (pruned wholesale by retention).
-- Purely additive (CREATE TABLE IF NOT EXISTS) — existing DBs gain it on open. It
-- carries org_id/campaign_id inline, so it is NOT in _ORG_ID_TABLES/_PLATFORM_TABLES.
CREATE TABLE IF NOT EXISTS run_events (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id      TEXT NOT NULL,                  -- correlates to RunManager.ActiveRun.run_id
    org_id      INTEGER,                        -- owning org (boundary scoping); NULL if unregistered
    campaign_id TEXT,                            -- which campaign this step belongs to (a batch run spans many)
    session_id  TEXT,                            -- the Session that emitted it (provenance)
    seq         INTEGER NOT NULL,                -- per-(run, session) monotonic cursor (1-based)
    phase       TEXT NOT NULL,                   -- lifecycle / relevance / comments / engage / feed_walk / halt
    level       TEXT NOT NULL,                   -- info / success / warn / error
    message     TEXT NOT NULL,                   -- human narrative line
    detail      TEXT,                            -- optional JSON blob (score, counts, target)
    created_at  REAL NOT NULL                    -- epoch seconds (matches sessions/actions)
);
CREATE INDEX IF NOT EXISTS idx_run_events_run ON run_events(run_id, id);
CREATE INDEX IF NOT EXISTS idx_run_events_org ON run_events(org_id, id);

-- ----- v11: account warming -----
-- First-class managed account, per-(org, platform). Identity columns
-- (username/profile_dir/cdp_port/fingerprint) are written once at provision and
-- never mutated; state/ramp/detail mutate in place. The durable warming entity;
-- a campaign consumes one from the pool (campaign_accounts). Purely additive
-- (CREATE TABLE IF NOT EXISTS) — existing DBs gain it on open, no migration.
CREATE TABLE IF NOT EXISTS accounts (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    org_id       INTEGER NOT NULL,
    platform     TEXT NOT NULL,                  -- x | linkedin | instagram (WARMABLE_PLATFORMS)
    username     TEXT NOT NULL,
    state        TEXT NOT NULL DEFAULT 'provisioned',
    profile_dir  TEXT,                           -- CDP: Chrome --user-data-dir
    cdp_port     INTEGER,                         -- per-account debug port (BASE_PORT + ordinal)
    fingerprint  TEXT,                            -- JSON, written once at provision
    ramp_day     INTEGER NOT NULL DEFAULT 0,
    warmth_floor REAL NOT NULL DEFAULT 0,         -- ramp curve target for ramp_day
    consecutive_flag_count INTEGER NOT NULL DEFAULT 0,
    last_warmed_at REAL,
    last_active_at REAL,
    cooling_until  REAL,
    detail       TEXT,                            -- JSON {login_status, checkpoint, ...}
    added_at     REAL NOT NULL,                   -- subsystem onboarding time (NOT account age, §3.5)
    updated_at   REAL NOT NULL,
    UNIQUE(org_id, platform, username),
    UNIQUE(cdp_port)
);
CREATE INDEX IF NOT EXISTS idx_accounts_org ON accounts(org_id, platform);

-- Append-only audit of lifecycle transitions (lead_status_changes idiom).
CREATE TABLE IF NOT EXISTS account_state_changes (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    account_id INTEGER NOT NULL,
    org_id     INTEGER NOT NULL,
    from_state TEXT,
    to_state   TEXT NOT NULL,
    reason     TEXT,                              -- 'warmth_gate_passed' | 'checkpoint_detected' | ...
    session_id TEXT,
    created_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_account_changes_acct ON account_state_changes(account_id, id);

-- campaign -> account assignment (pool model). No row => default pool pick;
-- row => pin. One backing account per (campaign, platform).
CREATE TABLE IF NOT EXISTS campaign_accounts (
    campaign_id TEXT NOT NULL,
    org_id      INTEGER NOT NULL,
    platform    TEXT NOT NULL,
    account_id  INTEGER NOT NULL,
    pinned      INTEGER NOT NULL DEFAULT 0,
    assigned_at REAL NOT NULL,
    PRIMARY KEY (campaign_id, platform)
);

-- Per-account encrypted secrets (Fernet via SecretCipher). A NEW table (not an
-- account_id added to integration_secrets, whose PK is (org_id, platform) — that
-- would be a reshape). Holds proxy creds / cookie-backup / MTProto session.
CREATE TABLE IF NOT EXISTS account_secrets (
    org_id     INTEGER NOT NULL,
    platform   TEXT NOT NULL,
    account_id INTEGER NOT NULL,
    enc_blob   TEXT NOT NULL,                     -- Fernet(JSON)
    updated_at REAL NOT NULL,
    PRIMARY KEY (org_id, platform, account_id)
);

-- v13: per-org subscription state (one active sub per org across ALL billing
-- rails — Polar today, PayTechUZ later). The vendor has ONE Polar account; each
-- org is a customer inside it, linked by external_customer_id = str(org_id). We
-- store only the resulting state here; Polar credentials are env vars, not rows.
-- Net-new table → fresh AND upgrading DBs get it from this CREATE IF NOT EXISTS
-- (executescript runs on every open); no ADD-COLUMN migration needed.
CREATE TABLE IF NOT EXISTS subscriptions (
    org_id                   INTEGER PRIMARY KEY,            -- one active sub per org
    provider                 TEXT NOT NULL DEFAULT 'polar',
    tier                     TEXT NOT NULL DEFAULT 'free',   -- free|lite|starter|pro|scale
    interval                 TEXT,                           -- month|year (NULL for free)
    lead_cap_override        INTEGER,                        -- NULL = TIERS[tier].lead_cap; set per-deal for scale
    status                   TEXT NOT NULL DEFAULT 'active', -- active|trialing|past_due|canceled|...
    provider_subscription_id TEXT,                           -- provider-neutral id
    provider_customer_id     TEXT,                           -- provider-neutral id
    current_period_start     REAL,                           -- anchors the cap window
    current_period_end       REAL,
    cancel_at_period_end     INTEGER NOT NULL DEFAULT 0,
    last_event_ts            REAL NOT NULL DEFAULT 0,         -- monotonic ordering (modified_at)
    updated_at               REAL NOT NULL
);

-- ----- v14: distributed workers pool -----
-- Off-cloud sidecar boxes that lease + run engine jobs against a LOCAL Chrome
-- (distributed-workers PRD §6, BUILD-PLAN §5). Net-new table → fresh AND upgrading
-- DBs get it from this CREATE IF NOT EXISTS (executescript runs on every open); no
-- ADD-COLUMN migration. Token hashed at rest (auth.hash_session_token), like
-- auth_sessions/invites. `status` is NOT a column — it is DERIVED at read time from
-- (now - last_heartbeat_at), see Store.list_workers / WORKER_* constants.
CREATE TABLE IF NOT EXISTS workers (
    id                TEXT PRIMARY KEY,         -- stable machine fingerprint (cfg.machine_id)
    org_id            INTEGER,                  -- NULL = pool-wide / not yet org-pinned
    display_name      TEXT,
    host              TEXT,
    os                TEXT,
    agent_version     TEXT,
    last_heartbeat_at REAL,
    registered_at     REAL NOT NULL,
    max_sessions      INTEGER NOT NULL DEFAULT 1,
    current_sessions  INTEGER NOT NULL DEFAULT 0,
    capabilities      TEXT,                     -- JSON array of [org_id, platform, account_handle]
    worker_token_hash TEXT NOT NULL,            -- SHA-256 at rest (never plaintext)
    token_expires_at  REAL,                     -- NULL = no expiry (long-lived; rotation is Phase 4)
    revoked_at        REAL,                     -- NULL = active; set = revoked
    enrolment_scope_kind TEXT,                  -- v22: 'org'|'pool' if enrolled via a token, else
                                                 -- NULL (legacy/self-declared). Sticky across
                                                 -- re-register — see register_worker.
    preflight_json    TEXT                      -- v23: the box's own launch self-check summary
                                                 -- {ok, blocking, enforced, ranAt, failed[]}, carried
                                                 -- on register/heartbeat. DIAGNOSTIC ONLY — never an
                                                 -- auth/dispatch input; a blocking box withholds its
                                                 -- capabilities instead. NULL = a pre-v23 sidecar
                                                 -- that has never reported one (F9/F10/F12).
);
CREATE INDEX IF NOT EXISTS idx_workers_org   ON workers(org_id);
CREATE INDEX IF NOT EXISTS idx_workers_token ON workers(worker_token_hash);

-- v22: per-worker enrolment tokens (BUILD-PLAN B8 fix). A worker's org scope is
-- SERVER-ASSIGNED at enrolment (this table), not self-declared by the box. Single-use:
-- redeemed_at is set exactly once, atomically, by Store.redeem_worker_enrolment_token
-- under _tx_immediate (mirrors lease_one_job's single-winner claim). scope_kind is an
-- explicit ADMIN decision: 'org' (org_id required) pins the worker+every capability's
-- cap_org to that org at redemption; 'pool' (org_id NULL) is the deliberate multi-org
-- grant (PRD: one managed box serving ~10 companies) and leaves capabilities unclamped.
-- id is an opaque, NON-secret admin-facing identifier (like workers.id); token_hash is
-- the ONLY persisted form of the plaintext (SHA-256 via hash_session_token, same as
-- worker bearer tokens) -- the plaintext is minted at the HTTP boundary and returned
-- to the admin exactly once, never stored, never logged.
CREATE TABLE IF NOT EXISTS worker_enrolment_tokens (
    id                    TEXT PRIMARY KEY,
    token_hash            TEXT NOT NULL UNIQUE,
    scope_kind            TEXT NOT NULL CHECK (scope_kind IN ('org','pool')),
    org_id                INTEGER REFERENCES organizations(id) ON DELETE CASCADE,
    label                 TEXT,
    created_at            REAL NOT NULL,
    created_by_admin_id   INTEGER REFERENCES platform_admins(id),
    expires_at            REAL NOT NULL,
    redeemed_at           REAL,
    redeemed_by_worker_id TEXT,
    revoked_at            REAL,
    revoked_by_admin_id   INTEGER REFERENCES platform_admins(id)
);
CREATE INDEX IF NOT EXISTS idx_worker_enrolment_tokens_org ON worker_enrolment_tokens(org_id);

-- Leased engine jobs (BUILD-PLAN §5, Phase 3). Net-new table → CREATE IF NOT EXISTS
-- (no ADD-COLUMN migration). status is a STORED lifecycle column with a CHECK guard;
-- leasing is SQLite-correct via Store.lease_one_job (BEGIN IMMEDIATE + conditional
-- UPDATE + rowcount, BUILD-PLAN C2 — SQLite has no SELECT … FOR UPDATE SKIP LOCKED).
-- Timestamps are REAL epoch, matching sessions/run_events/workers.
CREATE TABLE IF NOT EXISTS jobs (
    id                      TEXT PRIMARY KEY,     -- caller-supplied stable job id
    org_id                  INTEGER,
    campaign_id             TEXT NOT NULL,
    platform                TEXT NOT NULL,
    required_account_handle TEXT,                 -- NULL = no account pin
    spec                    TEXT NOT NULL,        -- JSON: target_leads, duration_minutes, engine_mode, soul_text…
    status                  TEXT NOT NULL DEFAULT 'queued'
                              CHECK (status IN ('queued','leased','running','done','failed','interrupted')),
    leased_by               TEXT,                 -- → workers.id while leased/running
    lease_expires_at        REAL,                 -- wall-clock lease deadline
    retry_after_at          REAL,                 -- do-not-lease-before (daytime/backoff)
    attempts                INTEGER NOT NULL DEFAULT 0,
    max_attempts            INTEGER NOT NULL DEFAULT 5,
    dead_lettered_at        REAL,                 -- set = exhausted/poison, never re-leased
    result                  TEXT,                 -- JSON summary from ack
    session_id              TEXT,                 -- → sessions.session_id (cloud-side mirror)
    pinned_worker_id        TEXT,                 -- Phase 4: reclaimed job pinned back to its original box (one account ↔ one box)
    created_at              REAL NOT NULL,
    updated_at              REAL NOT NULL
);
-- The lease SELECT filters status + account; a partial index keeps the queued-scan
-- tight as done/failed rows accumulate.
CREATE INDEX IF NOT EXISTS idx_jobs_lease  ON jobs(status, required_account_handle);
CREATE INDEX IF NOT EXISTS idx_jobs_queued ON jobs(created_at) WHERE status='queued';

-- ----- v14 (Phase 4): lifecycle control flags -----
-- The SOURCE OF TRUTH for drain/halt/update_required (BUILD-PLAN C6): the pause-file is
-- only the engine's cooperative checkpoint, NOT where an operator's intent lives. One
-- row per (scope, scope_key); a flag resolves for a job/worker by OR-merging every
-- applicable scope (global + its org + its platform + the worker itself). Net-new table
-- → CREATE IF NOT EXISTS self-heals fresh AND existing v14 DBs (executescript runs every
-- open); no ADD-COLUMN migration. Timestamps REAL epoch, matching workers/jobs.
CREATE TABLE IF NOT EXISTS control_flags (
    scope           TEXT NOT NULL,     -- 'global' | 'org' | 'platform' | 'worker'
    scope_key       TEXT NOT NULL,     -- '' for global; org_id / platform name / worker id otherwise
    drain           INTEGER NOT NULL DEFAULT 0,
    halt            INTEGER NOT NULL DEFAULT 0,
    update_required INTEGER NOT NULL DEFAULT 0,
    reason          TEXT,
    set_by          TEXT,              -- acting admin email (audit breadcrumb)
    updated_at      REAL NOT NULL,
    PRIMARY KEY (scope, scope_key)
);

-- ----- v15: superadmin (platform-admin) plane -----
-- A SEPARATE, higher-privilege auth surface from the org users/auth_sessions plane
-- (PRD §10): the one sanctioned, audited BOLA bypass. Parallel to users/auth_sessions
-- so the data layer never grows an `OR role='superadmin'` branch. Net-new tables →
-- CREATE IF NOT EXISTS self-heals fresh AND upgrading DBs (executescript every open);
-- no ADD-COLUMN migration. Timestamps REAL epoch. password_hash is the same
-- self-describing PBKDF2 string as users (auth.hash_password); mfa_secret is a Fernet
-- blob (aizu.secrets), NEVER plaintext.
CREATE TABLE IF NOT EXISTS platform_admins (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    email         TEXT NOT NULL UNIQUE,          -- login identity (stored lowercased)
    password_hash TEXT NOT NULL,                 -- PBKDF2 (auth.hash_password)
    mfa_secret    TEXT NOT NULL,                 -- Fernet blob of {"totp": <base32>}
    created_at    REAL NOT NULL,
    updated_at    REAL NOT NULL,
    disabled_at   REAL                           -- NULL = active; set = disabled (no login)
);

-- Server-side admin session store (parallel to auth_sessions). The client holds only
-- the opaque token in the rr_admin_session HttpOnly cookie; the row is the source of
-- truth. effective_org_id / effective_user_id are the IMPERSONATION principal (Phase
-- 5c): NULL = not impersonating (admin sees only the admin plane). Exactly one place
-- (the impersonate route) ever sets them.
CREATE TABLE IF NOT EXISTS platform_admin_sessions (
    token             TEXT PRIMARY KEY,          -- SHA-256 of the raw token (hash at rest)
    admin_id          INTEGER NOT NULL,
    effective_org_id  INTEGER,                   -- impersonated org (NULL = none)
    effective_user_id INTEGER,                   -- impersonated user (NULL = none)
    impersonation_started_at REAL,               -- when the current impersonation began
    impersonation_reason     TEXT,               -- operator-supplied reason (audited)
    created_at        REAL NOT NULL,
    expires_at        REAL NOT NULL,
    FOREIGN KEY (admin_id) REFERENCES platform_admins(id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_admin_sessions_admin ON platform_admin_sessions(admin_id);

-- Append-only, HASH-CHAINED audit of every admin-plane action (PRD §10, Phase 5c).
-- Distinct from the v9 per-org `audit_log`. row_hash = SHA-256(prev_hash ||
-- canonical-json(row-without-hashes)); a break in the chain is tamper evidence
-- (GET /api/admin/audit/verify walks it). NEVER updated or deleted.
CREATE TABLE IF NOT EXISTS admin_audit_log (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    prev_hash           TEXT NOT NULL,
    row_hash            TEXT NOT NULL,
    acting_admin_id     INTEGER,                 -- denormalised (no FK): survives admin deletion
    action              TEXT NOT NULL,
    target_org_id       INTEGER,
    target_user_id      INTEGER,
    target_resource     TEXT,
    at                  REAL NOT NULL,
    ip                  TEXT,
    user_agent          TEXT,
    reason              TEXT,
    impersonation_start REAL,
    impersonation_end   REAL
);
CREATE INDEX IF NOT EXISTS idx_admin_audit_at ON admin_audit_log(at);

-- DB-backed admin login throttle (survives a process restart, unlike the in-memory
-- org LoginThrottle). One compact row per throttle key (email / client-ip).
CREATE TABLE IF NOT EXISTS admin_login_throttle (
    key          TEXT PRIMARY KEY,
    fail_count   INTEGER NOT NULL DEFAULT 0,
    window_start REAL NOT NULL,
    locked_until REAL
);

-- Consumed TOTP step counters (anti-replay). A valid 6-digit code stays valid for the
-- ±window (~90s); recording the (admin, counter) it authenticated at rejects a replay
-- inside that window. Rows are short-lived — pruned opportunistically past the window.
CREATE TABLE IF NOT EXISTS admin_totp_used (
    admin_id INTEGER NOT NULL,
    counter  INTEGER NOT NULL,
    used_at  REAL NOT NULL,
    PRIMARY KEY (admin_id, counter)
);

-- v16: platform-wide superadmin settings (key→value). One row per setting; the only
-- one today is `execution_backend` (in_process|distributed) — the switch that routes
-- EVERY campaign run to the in-process RunManager or the distributed worker fleet.
CREATE TABLE IF NOT EXISTS platform_settings (
    key        TEXT PRIMARY KEY,
    value      TEXT NOT NULL,
    updated_at REAL NOT NULL,
    updated_by TEXT                                -- acting admin email (provenance)
);

-- v17: per-call log of the model-comparison fan-out (superadmin-switchable — see
-- platform_settings.model_comparison_enabled). One row per model per match-stage
-- call, including the primary model (is_primary=1). Deliberately separate from
-- spend_log so comparison-model $ never counts toward a campaign's spend cap.
CREATE TABLE IF NOT EXISTS model_comparison_log (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    campaign_id  TEXT NOT NULL,
    session_id   TEXT,
    platform     TEXT,
    stage        TEXT NOT NULL,
    model        TEXT NOT NULL,
    is_primary   INTEGER NOT NULL DEFAULT 0,
    label        TEXT,
    score        REAL,
    confidence   REAL,
    agreed       INTEGER,           -- NULL unless a threshold was supplied; else 1/0 vs primary's verdict
    latency_ms   REAL,
    usd          REAL,
    error        TEXT,
    created_at   REAL NOT NULL
);

-- v24: per-source discovery ledger (Campaign Lab, Remedy Sheet #1 / Remedy D).
-- One row per (campaign, platform, seed term). `walk()` has computed per-source
-- yield every run since the source stamp landed and dropped it at a debug line;
-- this is where it goes instead. Counters are cumulative across sessions, which
-- is what the park rule and the generator feedback both need.
--
-- `kind` is 'home' | 'hashtag' | 'account' | 'unknown'. `navigations` counts how
-- many times the walk visited this seed at all (NOT how many sessions ran, so a
-- multi-session run credits each visit). `yielded` counts items intercepted ON
-- this seed; `carried_over` counts items it drained that an earlier seed queued
-- (the 2026-08-19 mis-attribution, now recorded rather than hidden).
--
-- Lifecycle columns are SOFT and reversible: `parked_at` means the park rule
-- (see park_dry_sources) fired; `banned_at` means the platform said the page does
-- not exist. Neither deletes anything and both clear the moment the seed produces
-- again, so a transient outage cannot permanently kill a good seed.
CREATE TABLE IF NOT EXISTS source_stats (
    campaign_id   TEXT NOT NULL,
    platform      TEXT NOT NULL,
    source        TEXT NOT NULL,        -- the seed term ('home' for the algorithmic feed)
    kind          TEXT NOT NULL DEFAULT 'unknown',
    navigations   INTEGER NOT NULL DEFAULT 0,
    yielded       INTEGER NOT NULL DEFAULT 0,
    carried_over  INTEGER NOT NULL DEFAULT 0,
    redirects     INTEGER NOT NULL DEFAULT 0,
    dead_hits     INTEGER NOT NULL DEFAULT 0,   -- times the page reported "doesn't exist"
    seconds       REAL    NOT NULL DEFAULT 0,   -- cumulative walk time spent here
    first_seen    REAL NOT NULL,
    last_seen     REAL NOT NULL,
    last_yield_at REAL,                          -- last time this seed produced anything
    banned_at     REAL,                          -- platform says the page does not exist
    parked_at     REAL,                          -- park rule fired (skip on the next build_feed)
    park_reason   TEXT,
    PRIMARY KEY (campaign_id, platform, source)
);

-- v26: the flip-list substrate (Campaign Lab, Remedy Sheet #3 / Remedy E).
--
-- Every comment the match gate REJECTS is scored, costs a model call, and is then
-- discarded — `session.py`'s `if res.is_match:` is the only path to `matches`. So
-- the engine has been paying for exactly the data a gold set needs and keeping
-- none of it: measured 2026-08-21 on the live DB, `matches` held 2 rows (both
-- accepted, both Russian price questions) and there was no table anywhere holding
-- a rejected comment.
--
-- The negatives are the expensive half. Easy positives do not discriminate
-- between two prompts; the HARD negatives do — price complaints, past-purchase
-- reviews, competing vendors quoting their own prices. None of that was
-- recoverable after the fact.
--
-- `band` records WHY a row was captured, because the sampling is not uniform:
--   'accepted' — passed the gate (rare, and every one is a positive worth having)
--   'near'     — rejected within NEAR_BAND of the threshold; ALWAYS captured,
--                these are the boundary cases a threshold sweep turns on
--   'clear'    — rejected well below it; deterministically sampled, so the set
--                does not fill up with obvious noise
-- `label` is the HUMAN verdict and is NULL until someone supplies it. The model's
-- own score is stored beside it precisely so the two can disagree.
CREATE TABLE IF NOT EXISTS eval_candidates (
    campaign_id TEXT NOT NULL,
    platform    TEXT NOT NULL,
    comment_id  TEXT NOT NULL,
    session_id  TEXT,
    reel_id     TEXT,
    username    TEXT,
    text        TEXT NOT NULL,
    lang        TEXT,
    score       REAL,
    confidence  REAL,
    threshold   REAL,                    -- the gate this verdict was judged against
    reason      TEXT,
    tier        TEXT,
    raw         TEXT,                    -- sampled: the model's unparsed reply
    band        TEXT NOT NULL,           -- accepted | near | clear
    label       INTEGER,                 -- human ground truth 1/0; NULL = unlabelled
    labeled_at  REAL,
    labeled_by  TEXT,
    created_at  REAL NOT NULL,
    PRIMARY KEY (campaign_id, platform, comment_id)
);

-- Time-bucketed dashboard/report queries filter on these timestamps.
CREATE INDEX IF NOT EXISTS idx_matches_time     ON matches(campaign_id, captured_at);
CREATE INDEX IF NOT EXISTS idx_spend_time       ON spend_log(campaign_id, created_at);
CREATE INDEX IF NOT EXISTS idx_sessions_time    ON sessions(campaign_id, started_at);
CREATE INDEX IF NOT EXISTS idx_model_cmp_campaign ON model_comparison_log(campaign_id, created_at);
CREATE INDEX IF NOT EXISTS idx_model_cmp_model    ON model_comparison_log(model, created_at);
"""

# v7: data tables that gain a nullable `org_id` via ADD COLUMN on an existing DB
# (fresh DBs get it from SCHEMA above). These are the tables read ACROSS campaigns
# (matches → per_campaign_rollup; health_flags → open_flags) plus the campaign→org
# registry. All other data is campaign-scoped and isolated transitively by the
# campaign-ownership check at the API boundary, so it needs no org_id column.
_ORG_ID_TABLES = ("matches", "health_flags", "campaign_meta", "campaign_briefs")
DEFAULT_ORG_NAME = "Default Workspace"

# v10 run-activity retention: prune narrative events older than this, and keep only
# the most recent N runs' events (the panel only surfaces RECENT_LIMIT=10 runs).
RUN_EVENTS_TTL_SECONDS = 14 * 24 * 3600
RUN_EVENTS_KEEP_RUNS = 20
# Max run_events a fleet worker may ship in one heartbeat (bounds the body + the write
# lock window; the worker pages older events on subsequent beats).
MAX_RUN_EVENTS_SYNC = 200

# v14 worker presence (LOCKED #6). status is DERIVED from heartbeat age, never stored.
# Default cadence is 20s (DEFAULT_HEARTBEAT_INTERVAL_SEC, worker/__init__.py). PRD §8
# "offline after ~2min silence" = 6×20s; this resolves the PRD §6-vs-§8 inconsistency
# toward §8. online ≤ 2×interval; stale ≤ 6×interval; offline > 6×interval.
# (core must NOT depend on the worker package, so the 20.0 is intentionally duplicated
# here with this cross-reference comment, not imported.)
WORKER_HEARTBEAT_INTERVAL_SEC = 20.0
WORKER_ONLINE_MULTIPLIER = 2
WORKER_STALE_MULTIPLIER = 6

# v14 jobs leasing (BUILD-PLAN Phase 3). A lease must outlive a few missed heartbeats
# so a brief network blip never reassigns a live account (BUILD-PLAN risk #2): the TTL
# is interval × worst-case-multiplier, floored. A heartbeat (every interval) re-extends
# it, so at the 20s cadence a 60s lease survives two missed beats before it is
# reclaimable. The lease-extension UPDATE never reassigns a job for slowness — only an
# EXPIRED lease (lease_expires_at < now) is reclaimable by another lease call.
WORKER_LEASE_WORST_CASE_MULTIPLIER = 3
WORKER_LEASE_MIN_TTL_SEC = 60.0
# How many failed attempts (nacks) before a job is dead-lettered rather than requeued.
DEFAULT_JOB_MAX_ATTEMPTS = 5

# Max captured leads synced back to the cloud in a single ack (Phase 3 lead sync-back).
# Bounds the ack body; excess is dropped + logged (never silently). At PRD scale a job
# stops at its lead target (10–100), so this is generous headroom, not a real ceiling.
MAX_SYNC_LEADS = 500

# Max spend rollup rows synced back to the cloud in a single ack/nack (B9 fleet spend
# roll-up). The worker GROUPs its delta by (stage, model) before shipping it, so one
# run's whole spend collapses into a handful of rows — 50 is generous headroom for the
# stage×model matrix, not a per-call ceiling. Excess is dropped + logged, never silent.
MAX_SYNC_SPEND_ROWS = 50

# Stable per-database identity (platform_settings key), minted lazily on first read.
# Its ONLY job is letting the ack/nack spend roll-up tell "the worker wrote its spend
# rows into a DIFFERENT database from mine" (roll them up) from "the worker's db_path
# IS my db_path" (skip — the rows are already in this very table). AIZU_DB defaults to
# the same `aizu.db` filename the bridge uses, and the same-box dev/desktop topology
# genuinely shares one file, so this case is common, not theoretical — and spend_log
# has an AUTOINCREMENT PK with no unique key, so a second insert would silently DOUBLE
# a campaign's spend (halving its effective cap) rather than being idempotent.
DATABASE_ID_KEY = "db_id"

# v16 platform-wide execution backend (superadmin switch). `in_process` runs the engine
# on the cloud box via RunManager (the historical default); `distributed` enqueues a job
# for the worker fleet instead. Stored in platform_settings under EXECUTION_BACKEND_KEY.
EXECUTION_IN_PROCESS = "in_process"
EXECUTION_DISTRIBUTED = "distributed"
EXECUTION_BACKENDS = (EXECUTION_IN_PROCESS, EXECUTION_DISTRIBUTED)
EXECUTION_BACKEND_KEY = "execution_backend"

# v17 platform-wide model-comparison switch (superadmin). OFF by default so a fresh
# platform_settings row (or none at all) means today's single-model behaviour,
# unchanged. Which extra models to compare against is env-declared
# (MODEL_COMPARISON_MODELS), never stored here — this key only gates the fan-out.
MODEL_COMPARISON_ENABLED_KEY = "model_comparison_enabled"

# Cap on how many model names a lead's found_by_models can carry (mirrors the
# untrusted-payload caps below) — a compromised/buggy worker's synced lead JSON must
# not be able to smuggle an unbounded array into the matches table.
MAX_FOUND_BY_MODELS = 20

# Per-field caps on an untrusted synced lead (the worker payload is not trusted): a
# single string field and the serialized `extracted` blob are each bounded so a buggy
# or hostile worker can't write a multi-hundred-KB row within the 1 MB body cap.
MAX_LEAD_STR_LEN = 8192
MAX_EXTRACTED_BYTES = 8192


def _lead_str(value: Any) -> Optional[str]:
    """Coerce an untrusted synced-lead field to a clean, length-capped str or None
    (external data — the worker payload is not trusted). Numbers are stringified;
    blank → None; anything over MAX_LEAD_STR_LEN is truncated."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, str):
        s = value.strip()
        return s[:MAX_LEAD_STR_LEN] if s else None
    if isinstance(value, (int, float)):
        return str(value)[:MAX_LEAD_STR_LEN]
    return None


def _lead_float(value: Any, default: Optional[float]) -> Optional[float]:
    """Coerce an untrusted synced-lead field to a float, or `default` on anything odd.

    OverflowError is caught alongside TypeError/ValueError and is NOT theoretical: JSON
    has no integer bound, so a worker (or anything that can reach an ack/nack/sync body)
    can send an integer literal too large for a C double — `float(10**400)` raises
    OverflowError, which would escape this coercion and roll back the whole ack/nack
    transaction it runs inside. Every caller here is best-effort accounting/display
    (`score`, `capturedAt`, spend `usd`/`at`, run-event `seq`/`createdAt`): a junk value
    must degrade to `default`, never to a 500 that strands a leased job."""
    if isinstance(value, bool) or value is None:
        return default
    try:
        return float(value)
    except (TypeError, ValueError, OverflowError):
        return default


def _lead_str_list(value: Any) -> Optional[list[str]]:
    """Coerce an untrusted synced-lead field (`found_by_models`) to a clean list of
    short strings, or None. Non-string elements are dropped, not coerced (a model
    name is always a string); the list is capped at MAX_FOUND_BY_MODELS so a buggy
    or hostile worker can't smuggle an unbounded array into the matches table."""
    if not isinstance(value, list):
        return None
    out = [v.strip()[:MAX_LEAD_STR_LEN] for v in value if isinstance(v, str) and v.strip()]
    return out[:MAX_FOUND_BY_MODELS] if out else None

# v14 Phase 4 lifecycle controls.
# Worker bearer-token lifetime: long-lived / until-revoked (PRD §7 sign-off), NOT the
# 30-day human-session TTL. Revocation is the real off-switch (revoke_worker); expiry is
# only a backstop so a lost token can't live forever.
WORKER_TOKEN_TTL_SEC = 365 * 24 * 3600.0  # 1 year
# Control-flag scopes, broadest → narrowest. resolve_control_flags OR-merges all that
# apply to a job/worker; any scope setting a flag wins (halt/drain/update are "sticky on").
CONTROL_FLAG_SCOPES = ("global", "org", "platform", "worker")
CONTROL_FLAG_NAMES = ("drain", "halt", "update_required")
# A worker whose last heartbeat is older than this is treated as gone for reclaim: its
# in-flight (expired-lease) jobs are interrupted + requeued to it. Matches the derived
# "offline" boundary (6× the 20s cadence = 2 min silence).
WORKER_RECLAIM_OFFLINE_SEC = WORKER_STALE_MULTIPLIER * WORKER_HEARTBEAT_INTERVAL_SEC
# Escalate to a visible alert (health_flag) when a leased worker has been dark this long.
WORKER_RECLAIM_ALERT_SEC = 5 * 60.0
# A job requeued PINNED to its original box (no cross-box failover) waits for that box to
# return. If the box stays dark THIS long it is presumed gone for good, so the pinned
# QUEUED job — which reclaim's leased/running scan never touches and which no other box
# may lease — is dead-lettered (+ alerted) instead of lingering forever. Deliberately far
# longer than the offline/alert thresholds: a brief network blip must not burn the job.
WORKER_PINNED_DEAD_LETTER_SEC = 60 * 60.0
# Deterministic requeue backoff schedule (seconds), capped. Deterministic (not jittered)
# so the requeue time is test-assertable; the worker side already jitters its LEASE-miss
# polling (lease_client) which is where thundering-herd matters.
_NACK_BACKOFF_BASE_SEC = 30.0
_NACK_BACKOFF_CAP_SEC = 300.0

# v15 superadmin plane. DB-backed admin-login throttle (survives restart, unlike the
# in-memory org LoginThrottle): N failures inside the window lock the key for the
# lockout period. Mirrors auth.LoginThrottle tuning.
ADMIN_LOGIN_MAX_FAILURES = 5
ADMIN_LOGIN_WINDOW_SEC = 900.0
ADMIN_LOGIN_LOCKOUT_SEC = 900.0
# TOTP anti-replay: how long a consumed counter is retained before pruning. Must cover
# the ±window acceptance span (a 30s step, ±1 window ≈ 90s); kept generous.
_TOTP_WINDOW_STEPS_TTL_SEC = 120.0
# The genesis link of the admin_audit_log hash chain (row 1's prev_hash).
ADMIN_AUDIT_GENESIS_HASH = "0" * 64
# Domain separator between the prev_hash link and the row JSON in the audit hash input,
# so the concatenation is unambiguous even if the hash format ever changes.
_ADMIN_AUDIT_SEP = "\x1e"

# ----- v27 reveal metering (audit_log doubles as the meter) -----
# The audit trail is not just a record here, it is the ENFORCEMENT SOURCE for the
# per-period reveal allowance: `POST /api/lead/reveal` may not hand out more DISTINCT
# leads per billing period than the plan's lead cap. Counting audit rows rather than
# adding a counter table keeps one truth — you cannot spend allowance without leaving
# the row an operator would go looking for, and you cannot delete the row to get the
# allowance back (audit_log is insert-only by contract).
# ----- v28 opaque org-facing lead key -----
#
# `comment_id` is the platform's own id, and on four of six platforms it is a
# PERMALINK: reddit/youtube/telegram build it as f"{reel_id}/{comment_id}" and x uses
# the reply's own tweet rest_id. So an org-facing payload carrying `comment_id` carried
# the post id as a prefix — and the post prints the handle and the comment in plain
# sight. That made the v27 redaction and the handle-only reveal cosmetic on those four:
# the words were one hand-built URL away from a field we shipped on every lead row.
#
# The fix is a key with no platform data in it at all. Random rather than derived: an
# HMAC over the composite key would be stable without a column, but it is only as
# opaque as the secret, and this repo runs deployments where `AIZU_SECRET_KEY` is
# unset. A random token has no such dependency and nothing to reverse.
LEAD_TOKEN_BYTES = 12          # → 16 urlsafe chars; ~96 bits, unguessable


def new_lead_token() -> str:
    """One lead's opaque org-facing key. Unique by construction, not by check."""
    return secrets.token_urlsafe(LEAD_TOKEN_BYTES)


REVEAL_ACTION = "reveal_lead"
REVEAL_RESULT_REVEALED = "revealed"   # the ONLY outcome that consumes allowance


def default_lease_ttl_sec() -> float:
    """The lease window granted on lease + each heartbeat extension (BUILD-PLAN §Phase
    3 lease-extension math). Floored so a sub-cadence interval can't yield a lease too
    short to survive its own first heartbeat."""
    return max(WORKER_LEASE_MIN_TTL_SEC,
               WORKER_HEARTBEAT_INTERVAL_SEC * WORKER_LEASE_WORST_CASE_MULTIPLIER)


def nack_backoff_sec(attempts: int) -> float:
    """Exponential requeue delay for the Nth failed attempt (1-based), capped."""
    if attempts < 1:
        attempts = 1
    return min(_NACK_BACKOFF_CAP_SEC, _NACK_BACKOFF_BASE_SEC * (2 ** (attempts - 1)))


def _job_capability_covers(capabilities: list, *, org_id, platform,
                           account_handle) -> bool:
    """True iff some declared worker capability can serve this job (BUILD-PLAN §Phase 3
    enqueue/lease matching). A capability is ``[cap_org, cap_platform, cap_handle]``:
      - platform must match exactly;
      - org matches when either side is None (None = pool-wide / unpinned) or they're equal;
      - an account-PINNED job (account_handle set) requires the capability to declare
        that exact handle (the one-account-one-box pin); an unpinned job ignores handle.
    NEVER raises on a malformed capability row — a bad entry is skipped."""
    for entry in capabilities or []:
        if not isinstance(entry, (list, tuple)) or len(entry) != 3:
            continue
        cap_org, cap_platform, cap_handle = entry
        if cap_platform != platform:
            continue
        if cap_org is not None and org_id is not None and cap_org != org_id:
            continue
        if account_handle is not None and cap_handle != account_handle:
            continue
        return True
    return False


def derive_worker_status(last_heartbeat_at: Optional[float],
                         now: Optional[float] = None) -> str:
    """'online' | 'stale' | 'offline' from heartbeat age (LOCKED #6). A never-beat
    worker (None) is 'offline'. Boundaries: age == 40.0 → online; age == 120.0 →
    stale; age == 120.001 → offline."""
    if last_heartbeat_at is None:
        return "offline"
    age = (now if now is not None else time.time()) - float(last_heartbeat_at)
    if age <= WORKER_ONLINE_MULTIPLIER * WORKER_HEARTBEAT_INTERVAL_SEC:
        return "online"
    if age <= WORKER_STALE_MULTIPLIER * WORKER_HEARTBEAT_INTERVAL_SEC:
        return "stale"
    return "offline"


def _decode_capabilities(raw: Optional[str]) -> list:
    """Tolerantly decode the `capabilities` JSON blob to a list. NEVER raises — a
    corrupt or non-list blob yields [] (external-boundary discipline: a bad DB row
    must not crash a fleet read or a token auth)."""
    if not raw:
        return []
    try:
        decoded = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return []
    return decoded if isinstance(decoded, list) else []


def _decode_preflight(raw: Optional[str]) -> Optional[dict]:
    """Tolerantly decode the v23 `preflight_json` blob to a dict, or None. NEVER raises
    — a corrupt or non-dict blob yields None, which every reader renders as "no
    preflight reported" (same external-boundary discipline as _decode_capabilities). A
    diagnostic field must never be able to break a fleet read."""
    if not raw:
        return None
    try:
        decoded = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return None
    return decoded if isinstance(decoded, dict) else None


def _decode_job_spec(raw: Optional[str]) -> dict:
    """Tolerantly decode a job's `spec` JSON to a dict (never raises; a corrupt blob
    yields {} so a single bad row can't crash a lease scan)."""
    if not raw:
        return {}
    try:
        decoded = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return {}
    return decoded if isinstance(decoded, dict) else {}


def _job_row_to_dict(row) -> dict[str, Any]:
    """Full decoded job row (status/leasing/result), camelCase keys for the API."""
    return {
        "id": row["id"],
        "orgId": row["org_id"],
        "campaignId": row["campaign_id"],
        "platform": row["platform"],
        "requiredAccountHandle": row["required_account_handle"],
        "spec": _decode_job_spec(row["spec"]),
        "status": row["status"],
        "leasedBy": row["leased_by"],
        "leaseExpiresAt": row["lease_expires_at"],
        "retryAfterAt": row["retry_after_at"],
        "attempts": int(row["attempts"]),
        "maxAttempts": int(row["max_attempts"]),
        "deadLetteredAt": row["dead_lettered_at"],
        "result": _decode_job_spec(row["result"]) if row["result"] else None,
        "sessionId": row["session_id"],
        "pinnedWorkerId": row["pinned_worker_id"],
        "createdAt": row["created_at"],
        "updatedAt": row["updated_at"],
    }


def _enrolment_token_row_to_dict(row) -> dict[str, Any]:
    """Decoded worker_enrolment_tokens row, camelCase for the API. NEVER includes
    token_hash — the plaintext is minted at the HTTP boundary and returned exactly
    once from the mint response; every other read of this table goes through this
    helper so the hash can never leak into a list/read response."""
    return {
        "id": row["id"],
        "scopeKind": row["scope_kind"],
        "orgId": row["org_id"],
        "label": row["label"],
        "createdAt": row["created_at"],
        "createdByAdminId": row["created_by_admin_id"],
        "expiresAt": row["expires_at"],
        "redeemedAt": row["redeemed_at"],
        "redeemedByWorkerId": row["redeemed_by_worker_id"],
        "revokedAt": row["revoked_at"],
        "revokedByAdminId": row["revoked_by_admin_id"],
    }


def _job_row_to_lease(row, *, lease_expires_at: float,
                      prior_spend_usd: float = 0.0) -> dict[str, Any]:
    """The lease payload the worker plane returns — the JobSpec fields the sidecar's
    ``JobSpec.from_payload`` consumes, flattened from the row + its decoded spec. The
    `spec` blob holds the run knobs (target_leads/duration/engine_mode/soul_text/
    campaign_brief) so the job table stays narrow; lease unpacks them into the
    camelCase wire shape. campaignBrief MUST be unpacked here too (not just readable
    off the raw spec column via get_job) — this is the actual HTTP response body a
    remote worker's POST /api/worker/lease receives and JobSpec.from_payload consumes;
    anything baked into the spec but missing from this dict never reaches a genuinely
    remote worker (BUILD-PLAN B4/C5).

    `platformCredentials` is DELIBERATELY absent (SECURITY REVIEW CRITICAL/HIGH): a
    decrypted per-org secret must never ride the lease response OR the `spec` column it
    is read from (server._dispatch_run_to_fleet no longer bakes one there either) — a
    worker instead pulls its job's credential fresh, per request, via the lease-holder-
    gated POST /api/worker/jobs/{id}/credential (Handler._handle_job_credential).

    `priorSpendUsd` is NOT read from the spec — it is resolved LIVE at lease time from
    the cloud spend_log (see `lease_one_job`), because a value baked at enqueue would be
    stale by the time a queued job is picked up. It closes B9: without it a box's spend
    cap silently resets per machine, since the cap is checked against whichever DB the
    process opened. It MUST be whitelisted here, not merely computed — this dict is the
    literal HTTP body a remote worker's POST /api/worker/lease receives (B4)."""
    spec = _decode_job_spec(row["spec"])
    return {
        "id": row["id"],
        "orgId": row["org_id"],
        "campaignId": row["campaign_id"],
        "platform": row["platform"],
        "requiredAccountHandle": row["required_account_handle"],
        "targetLeads": spec.get("target_leads"),
        "durationMinutes": spec.get("duration_minutes"),
        "engineMode": spec.get("engine_mode", "harvest"),
        "soulText": spec.get("soul_text"),
        "campaignBrief": spec.get("campaign_brief"),
        "runId": spec.get("run_id"),
        "priorSpendUsd": prior_spend_usd,
        "leaseExpiresAt": lease_expires_at,
    }

# Tables that gained the `platform` column in schema v2, with their pre-v2
# column list (used to copy legacy rows forward, stamping platform='instagram').
_PLATFORM_TABLES = {
    "matches": ("campaign_id, reel_id, comment_id, session_id, username, text, "
                "lang, score, reason, extracted, status, tier, captured_at, updated_at"),
    "seen_reels": "campaign_id, reel_id, first_seen, last_seen, relevant, author, caption, ocr_text",
    "comment_cursors": "campaign_id, reel_id, last_cursor, last_polled",
    "watchlist": "campaign_id, reel_id, added_at, expires_at, match_count",
    "sessions": ("session_id, campaign_id, started_at, ended_at, status, halt_reason, "
                 "reels_seen, already_seen_skips, relevance_passes, comments_scored, "
                 "matches, escalations, spend_usd, feed_health_flag"),
}


@dataclass
class SessionCounters:
    reels_seen: int = 0
    already_seen_skips: int = 0
    relevance_passes: int = 0
    comments_scored: int = 0
    matches: int = 0
    escalations: int = 0
    transcriptions: int = 0  # reels sent through Uzbek STT (Instagram-only, gated)
    video_analyses: int = 0  # reels sent through the video-analysis tier (gated)
    spend_usd: float = 0.0
    feed_health_flag: bool = False
    likes: int = 0      # engagement actions this session (logged to `actions`)
    follows: int = 0


class Store:
    def __init__(self, db_path: str | Path, secret_cipher: Optional["SecretCipher"] = None):
        self.db_path = str(db_path)
        self._org_cache: dict[str, int] = {}  # campaign_id -> org_id (immutable mapping)
        # Lazily built from AIZU_SECRET_KEY on first secret use unless injected
        # (tests pass a throwaway key). None until resolved; never logged.
        self._secret_cipher = secret_cipher
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(self.db_path, timeout=30.0)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL;")
        self._conn.execute("PRAGMA foreign_keys=ON;")
        self._conn.execute("PRAGMA busy_timeout=30000;")
        self._init_schema()

    def _init_schema(self) -> None:
        with self._tx() as c:
            legacy = self._needs_platform_migration(c)
            if legacy:
                # The new tables differ only by the added `platform` column +
                # widened primary key, which SQLite can't ALTER in place — so
                # rebuild: rename the v1 tables aside, create v2, copy rows
                # forward stamping platform='instagram', drop the originals.
                c.execute("DROP INDEX IF EXISTS idx_matches_reel")
                c.execute("DROP INDEX IF EXISTS idx_matches_status")
                for table in _PLATFORM_TABLES:
                    c.execute(f"ALTER TABLE {table} RENAME TO {table}__legacy_v1")
            # v7: settings/integrations change PRIMARY KEY (gain org_id), which SQLite
            # cannot ALTER in place — rename them aside so executescript rebuilds the
            # new (org_id, key)/(org_id, platform) shape; rows are copied forward below.
            settings_pre_v7 = (self._table_exists(c, "settings")
                               and not self._has_column(c, "settings", "org_id"))
            integrations_pre_v7 = (self._table_exists(c, "integrations")
                                   and not self._has_column(c, "integrations", "org_id"))
            users_pre_v7 = (self._table_exists(c, "users")
                            and not self._has_column(c, "users", "org_id"))
            if settings_pre_v7:
                c.execute("ALTER TABLE settings RENAME TO settings__legacy_v6")
            if integrations_pre_v7:
                c.execute("ALTER TABLE integrations RENAME TO integrations__legacy_v6")
            c.executescript(SCHEMA)
            if legacy:
                for table, cols in _PLATFORM_TABLES.items():
                    c.execute(
                        f"INSERT INTO {table} ({cols}, platform) "
                        f"SELECT {cols}, 'instagram' FROM {table}__legacy_v1")
                    c.execute(f"DROP TABLE {table}__legacy_v1")
            # Read the version on disk BEFORE we stamp the new one, so a one-shot
            # value migration can run exactly once. None = fresh DB (no remap).
            row = c.execute(
                "SELECT value FROM meta WHERE key='schema_version'").fetchone()
            prior = int(row[0]) if row and row[0] is not None else None
            # v6: remap the old 4-status vocabulary to the new Kanban set. A value
            # change (not a PK/column change), so a plain UPDATE — runs after the
            # v1→v2 rename-dance above, which preserved the old status strings.
            if prior is not None and prior < 6:
                for old, new in _STATUS_V6_REMAP.items():
                    c.execute("UPDATE matches SET status=? WHERE status=?", (new, old))
            # v7: adopt all pre-existing single-tenant data into one Default org and
            # make existing account(s) its owner. Runs when a pre-v7 table OR a
            # leftover *__legacy_v6 table (a prior interrupted upgrade) is present —
            # the latter makes the migration self-healing. `executescript` above
            # implicitly COMMITs the rename-aside, so the copy-back below is keyed on
            # the legacy table's existence (not the pre_v7 flag) to survive a crash
            # between the rename and the copy.
            needs_v7 = (settings_pre_v7 or integrations_pre_v7 or users_pre_v7
                        or self._table_exists(c, "settings__legacy_v6")
                        or self._table_exists(c, "integrations__legacy_v6"))
            if needs_v7:
                self._migrate_to_v7(c)
            # Defence-in-depth: ensure org_id exists on the cross-org tables before
            # indexing them — even on the (unreachable-in-practice) path where the
            # migration above was skipped but the tables predate org_id.
            for _t in _ORG_ID_TABLES:
                self._add_column_if_missing(c, _t, "org_id INTEGER")
            # v10: self-heal the sessions run-correlation columns on an upgrading DB
            # (fresh DBs get them from SCHEMA). run_events is purely additive above.
            self._add_column_if_missing(c, "sessions", "run_id TEXT")
            self._add_column_if_missing(c, "sessions", "org_id INTEGER")
            c.execute("CREATE INDEX IF NOT EXISTS idx_sessions_run ON sessions(run_id)")
            # v11: warming join columns on existing tables (fresh DBs get them from
            # SCHEMA). `engine_mode` (NOT `mode` — that means dry/live elsewhere)
            # splits harvest from warming; `account_id` is the canonical warmth join
            # key (PRD C2, §3.2). Additive only — no NOT NULL/PK reshape.
            self._add_column_if_missing(
                c, "sessions", "engine_mode TEXT NOT NULL DEFAULT 'harvest'")
            self._add_column_if_missing(c, "sessions", "account_id INTEGER")
            self._add_column_if_missing(c, "health_flags", "account_id INTEGER")
            self._add_column_if_missing(c, "actions", "account_id INTEGER")
            c.execute("CREATE INDEX IF NOT EXISTS idx_actions_account ON actions(account_id)")
            c.execute(
                "CREATE INDEX IF NOT EXISTS idx_health_flags_account ON health_flags(account_id)")
            # v12: campaign lifecycle columns on an upgrading DB (fresh DBs get them
            # from SCHEMA). Purely additive — existing rows take the DEFAULTs
            # (schedule_enabled=0, archived_at NULL) = unscheduled, unarchived. No
            # rename-dance, no backfill.
            for _coldef in (
                "archived_at REAL", "paused_reason TEXT",
                "schedule_enabled INTEGER NOT NULL DEFAULT 0",
                "schedule_kind TEXT NOT NULL DEFAULT ''",
                "schedule_dow INTEGER", "schedule_hour INTEGER",
                "schedule_minute INTEGER",
                "schedule_tz TEXT NOT NULL DEFAULT 'Asia/Tashkent'",
                "next_run_at REAL", "last_scheduled_run_at REAL",
                "schedule_target_leads INTEGER", "schedule_duration_minutes INTEGER",
            ):
                self._add_column_if_missing(c, "campaign_meta", _coldef)
            c.execute("CREATE INDEX IF NOT EXISTS idx_campaign_meta_next_run "
                      "ON campaign_meta(next_run_at) WHERE next_run_at IS NOT NULL")
            c.execute("CREATE INDEX IF NOT EXISTS idx_campaign_meta_archived "
                      "ON campaign_meta(archived_at)")
            # v14 Phase 4: pinned_worker_id on a jobs table created by an earlier v14
            # (fresh DBs get it from SCHEMA). Purely additive — existing rows take NULL
            # (unpinned). The control_flags table is net-new so it self-heals via the
            # SCHEMA CREATE IF NOT EXISTS above; only the ADD COLUMN needs this.
            if self._table_exists(c, "jobs"):
                self._add_column_if_missing(c, "jobs", "pinned_worker_id TEXT")
            # v17: found_by_models on an existing pre-v17 `matches` table (fresh DBs
            # get it from SCHEMA already). Purely additive — existing rows take NULL.
            self._add_column_if_missing(c, "matches", "found_by_models TEXT")
            # v18: Uzbek-only local STT columns on an upgrading DB (fresh DBs get them
            # from SCHEMA already). Purely additive — existing rows take NULL/0.
            self._add_column_if_missing(c, "seen_reels", "transcript TEXT")
            self._add_column_if_missing(c, "seen_reels", "transcript_lang TEXT")
            self._add_column_if_missing(c, "seen_reels", "transcript_ms INTEGER")
            self._add_column_if_missing(
                c, "sessions", "transcriptions INTEGER NOT NULL DEFAULT 0")
            # v19: video-analysis columns on an upgrading DB (fresh DBs get them from
            # SCHEMA already). Purely additive — existing rows take NULL/0.
            self._add_column_if_missing(c, "seen_reels", "video_analyzed INTEGER")
            self._add_column_if_missing(c, "seen_reels", "video_analysis_summary TEXT")
            self._add_column_if_missing(
                c, "sessions", "video_analyses INTEGER NOT NULL DEFAULT 0")
            # v20: session liveness heartbeat on an upgrading DB (fresh DBs get them
            # from SCHEMA already). Purely additive — existing rows take NULL, which
            # find_stalled_sessions/reconcile_orphan_sessions already treat as "no
            # heartbeat yet" (COALESCE to started_at) rather than crashing.
            self._add_column_if_missing(c, "sessions", "last_activity_at REAL")
            self._add_column_if_missing(c, "sessions", "pid INTEGER")
            # v22: enrolment_scope_kind on an upgrading `workers` table (fresh DBs get
            # it from SCHEMA already). Purely additive — existing rows take NULL, which
            # register_worker/the register handler treat as "legacy/self-declared"
            # (BUILD-PLAN B8 fix — see server.py._handle_worker_register).
            self._add_column_if_missing(c, "workers", "enrolment_scope_kind TEXT")
            # v23: preflight_json on an upgrading `workers` table (fresh DBs get it from
            # SCHEMA already). Purely additive — existing rows take NULL, which every
            # reader treats as "this box has never reported a preflight" (a pre-v23
            # sidecar), NOT as a failure. Self-healing per ledger D4: the ADD COLUMN
            # runs on every open, so a DB restored from an older backup repairs itself.
            self._add_column_if_missing(c, "workers", "preflight_json TEXT")
            # v24: per-source attribution columns (Campaign Lab, Remedy Sheet #1/D).
            # Purely additive — existing rows take NULL, which every reader treats as
            # "captured before attribution existed", NOT as a source named "". The
            # source_stats table itself is net-new so SCHEMA's CREATE IF NOT EXISTS
            # self-heals it; only these two need the ALTER.
            self._add_column_if_missing(c, "seen_reels", "source TEXT")
            self._add_column_if_missing(c, "matches", "source TEXT")
            # v25: the author's stable id (Campaign Lab, Remedy Sheet #2/A). Additive
            # — existing rows take NULL, which every reader treats as "this platform
            # exposed no stable id", falling back to the display name.
            self._add_column_if_missing(c, "seen_reels", "author_id TEXT")
            # v26: keep the two fields that make a stored verdict re-examinable.
            # `confidence` drives the escalate band and was never persisted, so a
            # match could not be re-judged after the fact; `raw` is the model's
            # unparsed reply, sampled.
            self._add_column_if_missing(c, "matches", "confidence REAL")
            self._add_column_if_missing(c, "matches", "raw TEXT")
            # v27: the customer-facing intent line (lead-identity redaction). Additive
            # — existing rows take NULL, which every org-facing reader renders as a
            # neutral placeholder, NOT as a guess derived from the raw comment. Rows
            # captured before v27 keep username/text in the DB for the superadmin
            # plane; they simply have no intent until they are re-polled.
            self._add_column_if_missing(c, "matches", "intent TEXT")
            # v28: the opaque org-facing lead key. Additive, but UNLIKE every other
            # column here it cannot be left NULL on existing rows: it is the only key
            # an org-facing write accepts, so a row without one is a lead the customer
            # can see and can no longer act on (no status change, no note, no reveal).
            # So the ALTER is followed by a BACKFILL, one fresh token per existing row.
            #
            # Backfilled row-by-row rather than with a single UPDATE ... = random():
            # SQLite has no per-row random string function, and `randomblob` in one
            # statement is fine until you need the same value shape the writer mints.
            # One writer for the fact (`new_lead_token`) is worth the loop — this runs
            # once per database, over a table with at most a few hundred thousand rows.
            self._add_column_if_missing(c, "matches", "lead_token TEXT")
            self._backfill_lead_tokens(c)
            # The UNIQUE index is created HERE and deliberately NOT in SCHEMA above.
            # SCHEMA runs through `executescript` BEFORE this migration block, and on an
            # UPGRADING database `CREATE TABLE IF NOT EXISTS matches` does not widen the
            # existing table — so the column does not exist yet at that point and
            # `CREATE UNIQUE INDEX ... ON matches(lead_token)` aborts the whole open with
            # "no such column". That would brick every deployment that has ever stored a
            # lead, over an index. (Verified against a v27-shaped DB, not assumed.)
            #
            # After the backfill, never before: the backfill is what guarantees no two
            # rows share a value. SQLite treats NULLs as DISTINCT under a UNIQUE index, so
            # even a row a pre-v28 worker later inserts with NULL cannot collide — but a
            # blank string is NOT distinct, which is why `_backfill_lead_tokens` and
            # `ensure_lead_token` both treat '' as missing rather than as a value.
            self._create_unique_index_if_columns(
                c, "idx_matches_lead_token", "matches", ["lead_token"])
            # org_id indexes — created now (not in SCHEMA) because the columns may be
            # added by the v7 migration above, after executescript ran. The v24
            # source indexes are here for the same reason.
            c.execute("CREATE INDEX IF NOT EXISTS idx_users_org ON users(org_id)")
            c.execute("CREATE INDEX IF NOT EXISTS idx_matches_org ON matches(org_id)")
            c.execute("CREATE INDEX IF NOT EXISTS idx_campaign_meta_org ON campaign_meta(org_id)")
            # v24 reads: the seen_reels index carries `relevant` so the per-source
            # relevance rollup is index-only; matches.username is indexed for the
            # commenter x author overlap query (Remedy Sheet #2/D3).
            #
            # Each is guarded on its columns actually existing. CREATE TABLE IF NOT
            # EXISTS never widens a table that is already there, so a DB carrying a
            # hand-rolled or truncated `matches` reaches here without `username` —
            # and an unguarded CREATE INDEX would then abort the whole open,
            # bricking a database over a diagnostic index.
            self._create_index_if_columns(
                c, "idx_seen_reels_source", "seen_reels",
                ("campaign_id", "platform", "source", "relevant"))
            self._create_index_if_columns(
                c, "idx_matches_source", "matches",
                ("campaign_id", "platform", "source"))
            self._create_index_if_columns(
                c, "idx_matches_username", "matches", ("username",))
            # v26: the labelling queue reads "everything not yet labelled".
            self._create_index_if_columns(
                c, "idx_eval_candidates_label", "eval_candidates",
                ("campaign_id", "platform", "label"))
            self._create_index_if_columns(
                c, "idx_seen_reels_author", "seen_reels",
                ("campaign_id", "platform", "relevant"))
            c.execute(
                "INSERT OR IGNORE INTO meta(key, value) VALUES('schema_version', ?)",
                (str(SCHEMA_VERSION),),
            )
            c.execute("UPDATE meta SET value=? WHERE key='schema_version'",
                      (str(SCHEMA_VERSION),))

    @staticmethod
    def _needs_platform_migration(c: sqlite3.Cursor) -> bool:
        """True when a pre-v2 `matches` table exists without the platform column."""
        row = c.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='matches'"
        ).fetchone()
        if not row:
            return False  # fresh DB — SCHEMA creates the v2 shape directly
        cols = [r[1] for r in c.execute("PRAGMA table_info(matches)").fetchall()]
        return "platform" not in cols

    @staticmethod
    def _table_exists(c: sqlite3.Cursor, name: str) -> bool:
        return c.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
        ).fetchone() is not None

    @staticmethod
    def _has_column(c: sqlite3.Cursor, table: str, col: str) -> bool:
        return any(r[1] == col for r in c.execute(f"PRAGMA table_info({table})").fetchall())

    @staticmethod
    def _create_index_if_columns(c: sqlite3.Cursor, name: str, table: str,
                                 cols: tuple[str, ...]) -> None:
        """CREATE INDEX IF NOT EXISTS, but only when every named column exists.

        An index is a read optimisation; failing to create one must never abort
        `_init_schema` and take the whole database offline with it."""
        if not all(Store._has_column(c, table, col) for col in cols):
            logger.debug("DB skipping index %s — %s is missing one of %s",
                         name, table, ", ".join(cols))
            return
        c.execute(f"CREATE INDEX IF NOT EXISTS {name} ON {table}({', '.join(cols)})")

    @staticmethod
    def _create_unique_index_if_columns(c: sqlite3.Cursor, name: str, table: str,
                                        cols: list[str]) -> None:
        """`_create_index_if_columns`, but UNIQUE. Same guard and same reason: a
        migrating DB reaches the index statements before its ALTERs have necessarily
        run, and an unguarded CREATE INDEX on a missing column aborts the open — which
        turns a missing index into an unopenable database."""
        if not all(Store._has_column(c, table, col) for col in cols):
            logger.debug("DB skipping unique index %s — %s is missing one of %s",
                         name, table, ", ".join(cols))
            return
        c.execute(
            f"CREATE UNIQUE INDEX IF NOT EXISTS {name} ON {table}({', '.join(cols)})")

    @staticmethod
    def _add_column_if_missing(c: sqlite3.Cursor, table: str, coldef: str) -> None:
        """ALTER ... ADD COLUMN <coldef> unless the column already exists (idempotent)."""
        col = coldef.split()[0]
        if not Store._has_column(c, table, col):
            c.execute(f"ALTER TABLE {table} ADD COLUMN {coldef}")

    @staticmethod
    def _backfill_lead_tokens(c: sqlite3.Cursor) -> None:
        """Give every pre-v28 `matches` row an opaque org-facing key (v28).

        Idempotent by its WHERE clause, so a re-open is a no-op and an upgrade that
        died halfway resumes where it stopped rather than re-minting tokens for rows
        that already have one — re-minting would be silently destructive: the panel
        holds the old token in a URL and in its query cache, and rotating it turns
        every open drawer into a 404.

        Empty string is treated as missing alongside NULL. A blank token is not a
        usable key (it would collide with the next blank one under the UNIQUE index),
        and a hand-edited or partially-migrated DB is exactly where one shows up.
        """
        rows = c.execute(
            "SELECT rowid FROM matches WHERE lead_token IS NULL OR lead_token = ''"
        ).fetchall()
        for row in rows:
            c.execute("UPDATE matches SET lead_token=? WHERE rowid=?",
                      (new_lead_token(), row["rowid"]))
        if rows:
            logger.info("DB v28 — minted %d opaque lead token(s) for existing leads",
                        len(rows))

    def _migrate_to_v7(self, c: sqlite3.Cursor) -> None:
        """Fold existing single-tenant data into one Default org (v6→v7). Idempotent
        and self-healing: the copy-back of settings/integrations is keyed on the
        *__legacy_v6 tables actually being present, so an upgrade interrupted between
        the (committed) rename-aside and the copy completes on the next open."""
        settings_legacy = self._table_exists(c, "settings__legacy_v6")
        integrations_legacy = self._table_exists(c, "integrations__legacy_v6")
        # 1. Ensure a Default org exists (named from the legacy productName if any).
        row = c.execute("SELECT id FROM organizations ORDER BY id LIMIT 1").fetchone()
        if row is None:
            name = DEFAULT_ORG_NAME
            if settings_legacy:
                pr = c.execute(
                    "SELECT value FROM settings__legacy_v6 WHERE key='productName'"
                ).fetchone()
                if pr and pr["value"]:
                    try:
                        name = json.loads(pr["value"]) or DEFAULT_ORG_NAME
                    except (json.JSONDecodeError, TypeError):
                        pass
            now = time.time()
            cur = c.execute(
                """INSERT INTO organizations(name, logo, description, created_by_user_id,
                                             created_at, updated_at)
                   VALUES(?,?,?,?,?,?)""",
                (str(name), None, None, None, now, now))
            org_id = int(cur.lastrowid)
        else:
            org_id = int(row["id"])

        # 2. users gain org_id + role; existing accounts become the Default org owner.
        self._add_column_if_missing(c, "users", "org_id INTEGER")
        self._add_column_if_missing(c, "users", "role TEXT NOT NULL DEFAULT 'owner'")
        c.execute("UPDATE users SET org_id=? WHERE org_id IS NULL", (org_id,))
        c.execute("UPDATE users SET role='owner' WHERE role IS NULL OR role=''")
        c.execute(
            "UPDATE organizations SET created_by_user_id=(SELECT MIN(id) FROM users) "
            "WHERE id=? AND created_by_user_id IS NULL", (org_id,))

        # 3. campaign registry + cross-campaign data tables: add org_id, backfill.
        for table in _ORG_ID_TABLES:
            self._add_column_if_missing(c, table, "org_id INTEGER")
            c.execute(f"UPDATE {table} SET org_id=? WHERE org_id IS NULL", (org_id,))

        # 4. settings / integrations were rebuilt with the new PK — copy rows forward.
        # OR IGNORE so a retried/partial copy can't trip the (org_id, key) PK.
        if settings_legacy:
            c.execute(
                "INSERT OR IGNORE INTO settings(org_id, key, value, updated_at) "
                "SELECT ?, key, value, updated_at FROM settings__legacy_v6", (org_id,))
            c.execute("DROP TABLE settings__legacy_v6")
        if integrations_legacy:
            c.execute(
                "INSERT OR IGNORE INTO integrations(org_id, platform, connected, detail, updated_at) "
                "SELECT ?, platform, connected, detail, updated_at FROM integrations__legacy_v6",
                (org_id,))
            c.execute("DROP TABLE integrations__legacy_v6")

    @contextmanager
    def _tx(self) -> Iterator[sqlite3.Cursor]:
        cur = self._conn.cursor()
        try:
            yield cur
            self._conn.commit()
        except Exception:
            self._conn.rollback()
            raise
        finally:
            cur.close()

    @contextmanager
    def _tx_immediate(self) -> Iterator[sqlite3.Cursor]:
        """Like ``_tx`` but opens the transaction with ``BEGIN IMMEDIATE`` so a WRITE
        lock is taken at statement one — NOT the deferred read-lock ``_tx`` uses
        (BUILD-PLAN C2). This closes the SQLite double-lease window: a plain deferred
        tx lets two workers both ``SELECT … LIMIT 1`` the same queued row before either
        upgrades to a write lock, and the ``rowcount`` guard only catches the loser
        AFTER the fact. ``BEGIN IMMEDIATE`` serialises the SELECT→UPDATE as one writer.

        Used ONLY by the lease/lease-extension paths; every other write keeps the
        deferred ``_tx`` (cheaper, and correctness there does not depend on the lock
        timing). SQLite has no ``SELECT … FOR UPDATE SKIP LOCKED`` — this is the
        equivalent, and is a documented correctness trap, not a micro-optimisation."""
        cur = self._conn.cursor()
        try:
            cur.execute("BEGIN IMMEDIATE")
            yield cur
            self._conn.commit()
        except Exception:
            self._conn.rollback()
            raise
        finally:
            cur.close()

    def close(self) -> None:
        self._conn.close()

    # ----- v7 campaign -> org registry -----
    def org_for_campaign(self, campaign_id: Optional[str]) -> Optional[int]:
        """Resolve the owning org for a campaign (cached; the mapping is immutable).

        Reads the campaign→org registry (campaign_meta / campaign_briefs). Returns
        None for an unknown/unregistered campaign: writes then stamp NULL (never a
        cross-org default), and the API boundary treats None as "not in your org".
        """
        if not campaign_id:
            return None
        cached = self._org_cache.get(campaign_id)
        if cached is not None:
            return cached
        row = self._conn.execute(
            "SELECT org_id FROM campaign_meta WHERE campaign_id=? AND org_id IS NOT NULL",
            (campaign_id,)).fetchone()
        if row is None:
            row = self._conn.execute(
                "SELECT org_id FROM campaign_briefs WHERE campaign_id=? AND org_id IS NOT NULL",
                (campaign_id,)).fetchone()
        org_id = int(row["org_id"]) if row and row["org_id"] is not None else None
        if org_id is not None:
            self._org_cache[campaign_id] = org_id
        return org_id

    def campaign_in_org(self, campaign_id: Optional[str],
                        effective_org_id: Optional[int]) -> bool:
        """The composite tenant filter, in ONE repository helper handlers can't forget
        (BUILD-PLAN Phase 5 / PRD §10 BOLA rule). True iff `campaign_id` is registered to
        `effective_org_id`.

        This is the campaign-ownership gate for every request-boundary campaign-scoped
        read/write. `effective_org_id` is the EFFECTIVE org: a normal user passes their
        own org; an impersonating superadmin passes the impersonated org — there is no
        `OR role='superadmin'` bypass in the data layer, only this one filtered lookup.

        Fail closed: a None `effective_org_id` (a session with no org, or an admin who is
        not impersonating) never matches a real campaign, and an unknown campaign
        (org_for_campaign → None) never matches a real org.
        """
        if effective_org_id is None:
            return False
        return self.org_for_campaign(campaign_id) == effective_org_id

    # ----- seen_reels (dedupe watermark) -----
    def is_seen(self, campaign_id: str, reel_id: str,
                platform: str = DEFAULT_PLATFORM) -> bool:
        row = self._conn.execute(
            "SELECT 1 FROM seen_reels WHERE campaign_id=? AND platform=? AND reel_id=?",
            (campaign_id, platform, reel_id),
        ).fetchone()
        return row is not None

    def mark_seen(self, campaign_id: str, reel_id: str,
                  relevant: Optional[bool] = None, author: Optional[str] = None,
                  caption: Optional[str] = None, ocr_text: Optional[str] = None,
                  transcript: Optional[str] = None,
                  transcript_lang: Optional[str] = None,
                  video_analyzed: Optional[bool] = None,
                  video_analysis_summary: Optional[str] = None,
                  source: Optional[str] = None,
                  author_id: Optional[str] = None,
                  platform: str = DEFAULT_PLATFORM) -> None:
        """`source` is the seed term this item was intercepted on (v24). Like every
        other field here it is COALESCEd, so a re-poll never blanks the original
        attribution — first sighting owns provenance, which is the whole point."""
        now = time.time()
        rel = None if relevant is None else int(relevant)
        va = None if video_analyzed is None else int(video_analyzed)
        with self._tx() as c:
            c.execute(
                """INSERT INTO seen_reels(campaign_id, platform, reel_id, first_seen,
                                          last_seen, relevant, author, caption, ocr_text,
                                          transcript, transcript_lang,
                                          video_analyzed, video_analysis_summary,
                                          source, author_id)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(campaign_id, platform, reel_id) DO UPDATE SET
                     last_seen=excluded.last_seen,
                     relevant=COALESCE(excluded.relevant, seen_reels.relevant),
                     author=COALESCE(excluded.author, seen_reels.author),
                     caption=COALESCE(excluded.caption, seen_reels.caption),
                     ocr_text=COALESCE(excluded.ocr_text, seen_reels.ocr_text),
                     transcript=COALESCE(excluded.transcript, seen_reels.transcript),
                     transcript_lang=COALESCE(excluded.transcript_lang, seen_reels.transcript_lang),
                     video_analyzed=COALESCE(excluded.video_analyzed, seen_reels.video_analyzed),
                     video_analysis_summary=COALESCE(excluded.video_analysis_summary, seen_reels.video_analysis_summary),
                     source=COALESCE(seen_reels.source, excluded.source),
                     author_id=COALESCE(excluded.author_id, seen_reels.author_id)""",
                (campaign_id, platform, reel_id, now, now, rel, author, caption, ocr_text,
                 transcript, transcript_lang, va, video_analysis_summary,
                 (source or None), (author_id or None)),
            )

    # ----- per-source discovery ledger (v24, Campaign Lab Remedy Sheet #1/D) -----
    def record_source_walk(self, campaign_id: str, source: str, *,
                           platform: str = DEFAULT_PLATFORM,
                           kind: str = "unknown",
                           yielded: int = 0, carried_over: int = 0,
                           redirected: bool = False, unavailable: bool = False,
                           seconds: float = 0.0) -> None:
        """Fold one source's walk outcome into the cumulative ledger.

        Called from `CDPFeedBase.walk()`'s per-source epilogue (via the
        `FeedSource.on_source_done` sink), which has computed exactly these
        numbers on every run since the source stamp landed and thrown them away
        at a debug line.

        Self-healing by design: a source that yields anything CLEARS `banned_at`,
        `parked_at` and `park_reason`. A tag that 404s for one render, or a
        profile behind a momentary outage, therefore recovers on its own — the
        lifecycle columns are a running verdict, never a tombstone.
        """
        if not source:
            return
        now = time.time()
        produced = int(yielded) > 0
        with self._tx() as c:
            c.execute(
                """INSERT INTO source_stats(campaign_id, platform, source, kind,
                                            navigations, yielded, carried_over,
                                            redirects, dead_hits, seconds,
                                            first_seen, last_seen, last_yield_at,
                                            banned_at)
                   VALUES(?,?,?,?,1,?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(campaign_id, platform, source) DO UPDATE SET
                     kind=excluded.kind,
                     navigations=source_stats.navigations + 1,
                     yielded=source_stats.yielded + excluded.yielded,
                     carried_over=source_stats.carried_over + excluded.carried_over,
                     redirects=source_stats.redirects + excluded.redirects,
                     dead_hits=CASE WHEN excluded.yielded > 0 THEN 0
                                    ELSE source_stats.dead_hits + excluded.dead_hits END,
                     seconds=source_stats.seconds + excluded.seconds,
                     last_seen=excluded.last_seen,
                     last_yield_at=COALESCE(excluded.last_yield_at,
                                            source_stats.last_yield_at),
                     banned_at=CASE WHEN excluded.yielded > 0 THEN NULL
                                    ELSE COALESCE(source_stats.banned_at,
                                                  excluded.banned_at) END,
                     parked_at=CASE WHEN excluded.yielded > 0 THEN NULL
                                    ELSE source_stats.parked_at END,
                     park_reason=CASE WHEN excluded.yielded > 0 THEN NULL
                                      ELSE source_stats.park_reason END""",
                (campaign_id, platform, source, kind or "unknown",
                 max(0, int(yielded)), max(0, int(carried_over)),
                 1 if redirected else 0, 1 if unavailable else 0,
                 max(0.0, float(seconds)), now, now,
                 now if produced else None,
                 (now if (unavailable and not produced) else None)),
            )

    def source_stats(self, campaign_id: str,
                     platform: Optional[str] = None) -> list[dict[str, Any]]:
        """The ledger, enriched with the two numbers that actually decide a seed's
        fate: how many of its items passed the relevance gate, and how many leads
        it produced. Those live on `seen_reels.source` / `matches.source`, so they
        are DERIVED here rather than double-counted into `source_stats` — one
        writer per fact.

        Ordered best-first (leads, then relevant, then yield) so the caller can
        take the head of the list as "what is working"."""
        q = """
            SELECT ss.*,
                   (SELECT COUNT(*) FROM seen_reels sr
                     WHERE sr.campaign_id=ss.campaign_id AND sr.platform=ss.platform
                       AND sr.source=ss.source AND sr.relevant=1) AS relevant_reels,
                   (SELECT COUNT(*) FROM matches m
                     WHERE m.campaign_id=ss.campaign_id AND m.platform=ss.platform
                       AND m.source=ss.source) AS leads
              FROM source_stats ss
             WHERE ss.campaign_id=?
        """
        args: list[Any] = [campaign_id]
        if platform:
            q += " AND ss.platform=?"
            args.append(platform)
        q += " ORDER BY leads DESC, relevant_reels DESC, ss.yielded DESC, ss.source ASC"
        return [dict(r) for r in self._conn.execute(q, args).fetchall()]

    # Park thresholds. A seed must have been given a REAL chance before it is set
    # aside: three separate visits AND thirty intercepted items AND not one
    # relevance pass. Any one of those alone is noise — a tag can trivially serve
    # 30 items in one unlucky session, and three visits that intercepted 4 items
    # total have proven nothing.
    PARK_MIN_NAVIGATIONS = 3
    PARK_MIN_YIELDED = 30
    # A "does not exist" verdict is much stronger evidence than dryness, so it
    # parks far sooner — but still not on the first sighting, because a single
    # failed render reads identical to a banned page.
    PARK_MIN_DEAD_HITS = 2
    # Never park below this many live sources: a campaign with one working seed
    # left must keep walking it, and a campaign parked down to zero sources
    # silently turns into the home feed (core/config.py:197-210) or into nothing.
    PARK_MIN_ACTIVE = 2

    def park_dry_sources(self, campaign_id: str,
                         platform: str = DEFAULT_PLATFORM) -> list[dict[str, Any]]:
        """Park seeds that have earned it, and return the rows actually parked.

        Two grounds, both reversible (`record_source_walk` clears them the moment
        the seed produces again):
          * dead — the platform said the page does not exist, twice;
          * dry  — enough visits and enough intercepted items to be sure, with
                   zero relevance passes.

        The `home` source is never parked (it is not a seed and cannot be
        re-proposed), and the floor at PARK_MIN_ACTIVE stops the rule from
        disarming a campaign it was supposed to sharpen. Candidates are parked
        worst-first so the floor sacrifices the least-bad seed last."""
        rows = [r for r in self.source_stats(campaign_id, platform)
                if r.get("source") != "home" and not r.get("parked_at")]
        active = len(rows)
        candidates = []
        for r in rows:
            if r.get("dead_hits", 0) >= self.PARK_MIN_DEAD_HITS:
                candidates.append((r, "dead: page does not exist"))
            elif (r.get("navigations", 0) >= self.PARK_MIN_NAVIGATIONS
                  and r.get("yielded", 0) >= self.PARK_MIN_YIELDED
                  and not r.get("relevant_reels")):
                candidates.append((
                    r, f"dry: {r['navigations']} visits, {r['yielded']} items, "
                       f"0 relevant"))
        # Worst first: dead before dry, then fewest relevant, then fewest leads.
        candidates.sort(key=lambda rc: (0 if rc[1].startswith("dead") else 1,
                                        rc[0].get("relevant_reels", 0),
                                        rc[0].get("leads", 0)))
        parked: list[dict[str, Any]] = []
        now = time.time()
        with self._tx() as c:
            for row, reason in candidates:
                if active <= self.PARK_MIN_ACTIVE:
                    logger.info("DB park_dry_sources · floor reached (%d active) · "
                                "leaving %s parked-eligible but live",
                                active, row["source"])
                    break
                c.execute(
                    "UPDATE source_stats SET parked_at=?, park_reason=? "
                    "WHERE campaign_id=? AND platform=? AND source=?",
                    (now, reason, campaign_id, platform, row["source"]))
                active -= 1
                out = dict(row)
                out["parked_at"], out["park_reason"] = now, reason
                parked.append(out)
        if parked:
            logger.info("DB park_dry_sources · campaign=%s platform=%s parked=%s",
                        campaign_id, platform,
                        ", ".join(r["source"] for r in parked))
        return parked

    def parked_sources(self, campaign_id: str,
                       platform: Optional[str] = None) -> set[str]:
        """Seed terms currently parked or banned — what `build_feed` should skip
        and what the campaign generator must stop re-proposing."""
        q = ("SELECT source FROM source_stats WHERE campaign_id=? "
             "AND (parked_at IS NOT NULL OR banned_at IS NOT NULL)")
        args: list[Any] = [campaign_id]
        if platform:
            q += " AND platform=?"
            args.append(platform)
        return {r["source"] for r in self._conn.execute(q, args).fetchall()}

    def live_seeds(self, campaign_id: str, seeds: Sequence[str], *,
                   platform: str = DEFAULT_PLATFORM,
                   kind: str = "hashtag") -> list[str]:
        """Filter a brief's seed list down to the ones still worth walking.

        This is where the ledger stops being a report and starts saving time: a
        parked or banned seed costs a navigation plus four empty-scroll rounds
        (~45s) every single session, forever, and produces nothing.

        Two guardrails, both deliberate:
          * never return an EMPTY list when seeds were supplied — an empty seed
            list flips the home feed back on (`core/config.py` include_home_feed)
            and silently turns a targeted campaign into an untargeted one;
          * keep at least `PARK_MIN_ACTIVE` seeds alive, restoring the
            best-performing parked ones if the filter would go below it.

        When every supplied seed is parked/banned it raises the `seeds_all_dead`
        health flag — the case that used to be a green, zero-lead `completed` run
        with no DB row anywhere.
        """
        wanted = [str(x) for x in seeds if str(x).strip()]
        if not wanted:
            return []
        dead = self.parked_sources(campaign_id, platform)
        live = [x for x in wanted if x not in dead]
        if len(live) == len(wanted):
            return live
        if not live:
            self.raise_flag(
                "seeds_all_dead", "soft",
                f"every {kind} seed on {platform} is parked or banned "
                f"({', '.join(wanted)}) — walking them anyway; re-seed the campaign",
                campaign_id=campaign_id)
            logger.warning("DB live_seeds · campaign=%s platform=%s · ALL %d seed(s) "
                           "dead — refusing to empty the seed list", campaign_id,
                           platform, len(wanted))
            return wanted
        if len(live) < self.PARK_MIN_ACTIVE:
            # Restore best-first from the ledger so the floor keeps the least-bad
            # seeds, not an arbitrary two.
            ranked = [r["source"] for r in self.source_stats(campaign_id, platform)
                      if r["source"] in dead and r["source"] in wanted]
            for src in ranked:
                if len(live) >= self.PARK_MIN_ACTIVE:
                    break
                live.append(src)
            live = [x for x in wanted if x in set(live)]   # keep the brief's order
        skipped = [x for x in wanted if x not in live]
        if skipped:
            logger.info("DB live_seeds · campaign=%s platform=%s · skipping %s",
                        campaign_id, platform, ", ".join(skipped))
        return live

    def seed_history(self, org_id: Optional[int] = None, *,
                     platform: Optional[str] = None,
                     kind: Optional[str] = None,
                     limit: int = 12) -> dict[str, list[str]]:
        """What this org's past runs proved about seed terms, as two flat lists.

        `productive` = seeds that produced at least one lead, best first.
        `dead`       = seeds parked or banned by the ledger.

        Fed to the AI campaign generator (`campaign_gen`), which today invents
        seeds from parametric memory alone and will happily re-propose the exact
        tag that 302'd on every run last month. Scoped to the org, NOT to one
        campaign, because a brand-new campaign has no history of its own — the
        transferable knowledge is "on this platform, for this tenant, these terms
        work and these do not".

        The `home` pseudo-source is excluded: it is not a seed and cannot be
        proposed.

        `kind` ('hashtag' | 'account') splits the two, and the caller should
        almost always pass it. `source_stats.kind` has always carried the
        distinction — `core/cdp.py::_source_seeds` labels account seeds
        correctly — but this function ignored it, so handles and hashtags came
        back in one undifferentiated list and were rendered to the campaign
        generator as one heading. A model told that `@acme_remont` and `remont`
        are the same kind of thing will happily propose a hashtag where a handle
        belongs.
        """
        args: list[Any] = []
        where = ["ss.source <> 'home'"]
        join = ""
        if kind:
            where.append("ss.kind = ?")
            args.append(kind)
        if org_id is not None:
            join = " JOIN campaign_meta cm ON cm.campaign_id = ss.campaign_id"
            where.append("cm.org_id = ?")
            args.append(org_id)
        if platform:
            where.append("ss.platform = ?")
            args.append(platform)
        clause = " AND ".join(where)
        # SUM over the group, NOT a bare correlated subquery. The subquery
        # references ss.campaign_id/ss.platform, which are NOT in the GROUP BY —
        # SQLite then evaluates them against ONE ARBITRARY row of each group, so
        # `leads` was the count for a single campaign rather than the org total.
        # A seed proven on one campaign and parked on another therefore flipped
        # between `productive` and `dead` purely on which campaign the tenant
        # created FIRST (the group representative follows campaign_meta rowid
        # order, not the id's collation), and in the losing case a seed that had
        # produced real leads was rendered to the generator as
        # "Do NOT propose them or close variants".
        productive = [r["source"] for r in self._conn.execute(
            f"""SELECT ss.source,
                       SUM((SELECT COUNT(*) FROM matches m
                             WHERE m.campaign_id=ss.campaign_id
                               AND m.platform=ss.platform
                               AND m.source=ss.source)) AS leads
                  FROM source_stats ss{join}
                 WHERE {clause}
                 GROUP BY ss.source
                HAVING leads > 0
                 ORDER BY leads DESC, ss.source ASC
                 LIMIT ?""", (*args, limit)).fetchall()]
        # No LIMIT in the SQL: the productive-subtraction below happens AFTER the
        # fetch, so limiting here could silently return fewer than `limit` dead
        # terms (or none) whenever the head of the list is also productive.
        dead = [r["source"] for r in self._conn.execute(
            f"""SELECT DISTINCT ss.source FROM source_stats ss{join}
                 WHERE {clause}
                   AND (ss.parked_at IS NOT NULL OR ss.banned_at IS NOT NULL)
                 ORDER BY ss.source ASC""", args).fetchall()]
        # A term can be dead on one campaign and productive on another; proof of
        # leads outranks a park verdict, so it is only reported as dead when it has
        # never produced anywhere.
        proven = set(productive)
        dead = [d for d in dead if d not in proven][:limit]
        return {"productive": productive, "dead": dead}

    def unpark_source(self, campaign_id: str, source: str,
                      platform: str = DEFAULT_PLATFORM) -> None:
        """Operator override — clear a park/ban verdict without touching counters.
        The counters stay so the rule can re-fire on fresh evidence rather than
        re-litigating the old evidence immediately."""
        with self._tx() as c:
            c.execute(
                "UPDATE source_stats SET parked_at=NULL, park_reason=NULL, "
                "banned_at=NULL, dead_hits=0 "
                "WHERE campaign_id=? AND platform=? AND source=?",
                (campaign_id, platform, source))

    # ----- seed mining (v25, Campaign Lab Remedy Sheet #2/A) -----
    def seed_candidates(self, campaign_id: str,
                        platform: Optional[str] = None,
                        exclude: Sequence[str] = (),
                        min_relevant: int = 1,
                        limit: int = 25) -> list[dict[str, Any]]:
        """Accounts whose posts this campaign already judged relevant, best first.

        The best possible seed candidates, and they cost ZERO new requests: every
        engine already writes `seen_reels.author` plus the `relevant` label, and
        `matches` already records which posts produced actual leads. Nothing
        aggregated any of it — "which accounts produce our leads" was unanswerable
        against a database that had always held the answer.

        Ranked leads-first, because a lead is proof and a relevance pass is only a
        signal. `author_id` (v25) is the grouping key when the platform exposes
        one, so a rename reads as the same candidate; the display name is carried
        along for the operator and is the fallback key on platforms that expose no
        stable id.

        `exclude` should carry the campaign's current seeds — an account we are
        already walking is not a discovery.
        """
        drop = {str(x).strip().lower().lstrip("@") for x in exclude if str(x).strip()}
        q = """
            SELECT COALESCE(NULLIF(sr.author_id, ''), sr.author) AS seed_key,
                   MAX(sr.author)    AS author,
                   MAX(sr.author_id) AS author_id,
                   sr.platform       AS platform,
                   COUNT(DISTINCT sr.reel_id) AS relevant_posts,
                   COUNT(DISTINCT m.comment_id) AS leads
              FROM seen_reels sr
              LEFT JOIN matches m
                ON m.campaign_id = sr.campaign_id
               AND m.platform    = sr.platform
               AND m.reel_id     = sr.reel_id
             WHERE sr.campaign_id = ?
               AND sr.relevant = 1
               AND sr.author IS NOT NULL AND TRIM(sr.author) <> ''
        """
        args: list[Any] = [campaign_id]
        if platform:
            q += " AND sr.platform = ?"
            args.append(platform)
        q += """
             GROUP BY seed_key, sr.platform
             HAVING relevant_posts >= ?
             ORDER BY leads DESC, relevant_posts DESC, seed_key ASC
        """
        args.append(max(1, int(min_relevant)))
        out: list[dict[str, Any]] = []
        for r in self._conn.execute(q, args).fetchall():
            row = dict(r)
            key = str(row.get("seed_key") or "")
            if key.strip().lower().lstrip("@") in drop:
                continue
            # `seed` is what an operator would paste back into the brief: the
            # stable id when there is one, else the display name.
            row["seed"] = str(row.get("author_id") or "").strip() or row.get("author") or ""
            out.append(row)
            if len(out) >= limit:
                break
        return out

    def co_commenter_overlap(self, campaign_id: str,
                             platform: Optional[str] = None,
                             min_shared: int = 2,
                             limit: int = 25) -> list[dict[str, Any]]:
        """Authors ranked by how much of their commenting audience we ALSO see
        under other authors we have discovered.

        The bipartite `matches.username` x `seen_reels.author` join the audit
        flagged as computable-today-but-computed-nowhere.

        "Shared" means: a commenter on this author's posts who also comments under
        at least one DIFFERENT author in this campaign. Defining it against every
        lead in the campaign instead — the obvious first cut — makes the set
        include the author's own commenters, so every author trivially overlaps
        100% with "our commenters" and the metric says nothing.

        Reported as `overlap_share` (shared ÷ that author's own commenters), never
        as a raw count. Raw overlap ranks giant generic accounts first purely
        because they are large — the failure mode SubredditStats' published
        formula exists to correct. A niche account whose whole audience we already
        know is a far better lookalike than a huge one where we know 2%.

        Needs `idx_matches_username` (added in v24) to stay cheap.
        """
        where_platform = " AND sr.platform = ?" if platform else ""
        q = f"""
            WITH ac AS (
                SELECT DISTINCT
                       COALESCE(NULLIF(sr.author_id, ''), sr.author) AS seed_key,
                       sr.platform  AS platform,
                       sr.author    AS author,
                       sr.author_id AS author_id,
                       m.username   AS username
                  FROM seen_reels sr
                  JOIN matches m
                    ON m.campaign_id = sr.campaign_id
                   AND m.platform    = sr.platform
                   AND m.reel_id     = sr.reel_id
                 WHERE sr.campaign_id = ?
                   AND sr.author IS NOT NULL AND TRIM(sr.author) <> ''
                   AND m.username IS NOT NULL AND TRIM(m.username) <> ''
                   {where_platform}
            )
            SELECT a.seed_key                     AS seed_key,
                   a.platform                     AS platform,
                   MAX(a.author)                  AS author,
                   MAX(a.author_id)               AS author_id,
                   COUNT(DISTINCT a.username)     AS total_commenters,
                   COUNT(DISTINCT CASE WHEN EXISTS (
                       SELECT 1 FROM ac b
                        WHERE b.username = a.username
                          AND b.seed_key <> a.seed_key
                   ) THEN a.username END)         AS shared_commenters
              FROM ac a
             GROUP BY a.seed_key, a.platform
            HAVING shared_commenters >= ?
             ORDER BY shared_commenters * 1.0 / total_commenters DESC,
                      shared_commenters DESC, seed_key ASC
        """
        args: list[Any] = [campaign_id]
        if platform:
            args.append(platform)
        args.append(max(1, int(min_shared)))
        out: list[dict[str, Any]] = []
        for r in self._conn.execute(q, args).fetchall():
            row = dict(r)
            total = row.get("total_commenters") or 0
            row["overlap_share"] = (row["shared_commenters"] / total) if total else 0.0
            row["seed"] = str(row.get("author_id") or "").strip() or row.get("author") or ""
            out.append(row)
            if len(out) >= limit:
                break
        return out

    # ----- eval candidates: the flip-list substrate (v26, Sheet #3/E) -----
    # A rejected comment is scored, paid for, and thrown away. These capture a
    # sampled record of them so a gold set can exist at all.
    #
    # Sampling is deliberately NOT uniform. Boundary cases decide where a
    # threshold lands and are rare; obvious noise is abundant and worthless.
    NEAR_BAND = 0.15          # |score - threshold| within this = always captured
    CLEAR_SAMPLE_RATE = 8     # keep 1 in N of the obvious rejects
    EVAL_SESSION_CAP = 200    # per session, so a long run cannot flood the table

    @staticmethod
    def eval_band(score: Optional[float], threshold: float,
                  is_match: bool) -> str:
        """Which capture band a verdict falls in: accepted | near | clear."""
        if is_match:
            return "accepted"
        if score is None:
            return "clear"
        # +epsilon: the band is documented as inclusive, and binary floating point
        # puts the exact edge on the wrong side of it — `0.7 - 0.15` is
        # 0.5499999999999999, whose distance from 0.7 is 0.15000000000000002.
        # Without this the boundary case the band exists to capture is the one
        # case it drops.
        return "near" if abs(float(score) - float(threshold)) <= Store.NEAR_BAND + 1e-9 \
            else "clear"

    @staticmethod
    def eval_should_capture(comment_id: str, band: str) -> bool:
        """Deterministic sampling — a hash of the comment id, not `random`.

        Deterministic on purpose: a re-polled comment must land on the same side
        of the sample every time, or the table fills with near-duplicates of
        whatever happened to be re-scored; and a test can assert the decision."""
        if band != "clear":
            return True
        digest = hashlib.sha256(str(comment_id).encode("utf-8")).hexdigest()
        return int(digest[:8], 16) % Store.CLEAR_SAMPLE_RATE == 0

    def record_eval_candidate(self, *, campaign_id: str, comment_id: str,
                              text: str, band: str,
                              platform: str = DEFAULT_PLATFORM,
                              session_id: Optional[str] = None,
                              reel_id: Optional[str] = None,
                              username: Optional[str] = None,
                              lang: Optional[str] = None,
                              score: Optional[float] = None,
                              confidence: Optional[float] = None,
                              threshold: Optional[float] = None,
                              reason: Optional[str] = None,
                              tier: Optional[str] = None,
                              raw: Optional[str] = None) -> bool:
        """Persist one labelling candidate. Returns True if a row was written.

        Idempotent on `comment_id`, and a re-poll REFRESHES the model's fields
        while leaving any human `label` untouched — same rule as `matches.status`.
        A human verdict outranks every later machine opinion about it."""
        if not str(text or "").strip():
            return False
        now = time.time()
        with self._tx() as c:
            c.execute(
                """INSERT INTO eval_candidates
                     (campaign_id, platform, comment_id, session_id, reel_id,
                      username, text, lang, score, confidence, threshold, reason,
                      tier, raw, band, created_at)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(campaign_id, platform, comment_id) DO UPDATE SET
                     score=excluded.score,
                     confidence=excluded.confidence,
                     threshold=excluded.threshold,
                     reason=excluded.reason,
                     tier=excluded.tier,
                     raw=COALESCE(excluded.raw, eval_candidates.raw),
                     band=excluded.band""",
                (campaign_id, platform, str(comment_id), session_id, reel_id,
                 username, text, lang, score, confidence, threshold, reason,
                 tier, raw, band, now))
        return True

    def eval_candidate_count(self, campaign_id: str,
                             session_id: Optional[str] = None) -> int:
        """How many candidates exist, optionally for one session (the cap check)."""
        if session_id:
            row = self._conn.execute(
                "SELECT COUNT(*) FROM eval_candidates WHERE campaign_id=? "
                "AND session_id=?", (campaign_id, session_id)).fetchone()
        else:
            row = self._conn.execute(
                "SELECT COUNT(*) FROM eval_candidates WHERE campaign_id=?",
                (campaign_id,)).fetchone()
        return int(row[0]) if row else 0

    def eval_candidates(self, campaign_id: str, *,
                        platform: Optional[str] = None,
                        band: Optional[str] = None,
                        unlabelled_only: bool = False,
                        limit: int = 500) -> list[dict[str, Any]]:
        """The labelling queue / gold-set export.

        Ordered near-band first, then by how close the score sat to the
        threshold: the most informative items to label are the ones the model was
        least sure about, and a human labelling budget is small."""
        q = ["SELECT * FROM eval_candidates WHERE campaign_id=?"]
        args: list[Any] = [campaign_id]
        if platform:
            q.append("AND platform=?")
            args.append(platform)
        if band:
            q.append("AND band=?")
            args.append(band)
        if unlabelled_only:
            q.append("AND label IS NULL")
        q.append("ORDER BY CASE band WHEN 'near' THEN 0 WHEN 'accepted' THEN 1 "
                 "ELSE 2 END, ABS(COALESCE(score,0) - COALESCE(threshold,0)) ASC, "
                 "created_at ASC LIMIT ?")
        args.append(limit)
        return [dict(r) for r in self._conn.execute(" ".join(q), args).fetchall()]

    def label_eval_candidate(self, campaign_id: str, comment_id: str,
                             label: bool, *, platform: str = DEFAULT_PLATFORM,
                             labeled_by: str = "") -> None:
        """Record the HUMAN verdict. This is the ground truth everything else is
        measured against, so it is never written by the engine."""
        with self._tx() as c:
            c.execute(
                "UPDATE eval_candidates SET label=?, labeled_at=?, labeled_by=? "
                "WHERE campaign_id=? AND platform=? AND comment_id=?",
                (int(bool(label)), time.time(), labeled_by, campaign_id,
                 platform, str(comment_id)))

    # ----- comment cursors -----
    def get_cursor(self, campaign_id: str, reel_id: str,
                   platform: str = DEFAULT_PLATFORM) -> Optional[str]:
        row = self._conn.execute(
            "SELECT last_cursor FROM comment_cursors "
            "WHERE campaign_id=? AND platform=? AND reel_id=?",
            (campaign_id, platform, reel_id),
        ).fetchone()
        return row["last_cursor"] if row else None

    def set_cursor(self, campaign_id: str, reel_id: str, cursor: Optional[str],
                   platform: str = DEFAULT_PLATFORM) -> None:
        with self._tx() as c:
            c.execute(
                """INSERT INTO comment_cursors(campaign_id, platform, reel_id,
                                               last_cursor, last_polled)
                   VALUES(?,?,?,?,?)
                   ON CONFLICT(campaign_id, platform, reel_id) DO UPDATE SET
                     last_cursor=excluded.last_cursor, last_polled=excluded.last_polled""",
                (campaign_id, platform, reel_id, cursor, time.time()),
            )

    # ----- matches (idempotent, status-preserving) -----
    def upsert_match(self, *, campaign_id: str, reel_id: str, comment_id: str,
                     username: Optional[str], text: Optional[str], lang: Optional[str],
                     score: float, reason: str, extracted: Optional[dict[str, Any]],
                     tier: str, session_id: Optional[str] = None,
                     platform: str = DEFAULT_PLATFORM,
                     captured_at: Optional[float] = None,
                     found_by_models: Optional[list[str]] = None,
                     source: Optional[str] = None,
                     intent: Optional[str] = None,
                     initial_status: str = "new") -> None:
        """Insert a match, or refresh its scored fields on re-poll.

        Crucially, `status` is set only on first insert. A re-scrape updates the
        score/reason/extracted but NEVER touches a human-set status (PRD §7).
        `session_id` records first-capture provenance and is not overwritten.
        `captured_at` defaults to now; the distributed-worker lead sync passes the
        ORIGINAL capture time so a lead synced minutes later keeps its real timestamp
        (it is set only on first insert, never overwritten on re-poll).
        `found_by_models` (model-comparison fan-out, empty/None when the feature is
        off) DOES refresh on every re-poll — unlike `status`, it reflects the latest
        verdict, not a human decision.
        `intent` (v27) is the customer-facing one-line summary of what this
        commenter wants — the only lead text an org ever sees, since username/comment
        are superadmin-only now. It refreshes on re-poll but COALESCEs: a re-poll that
        derives nothing (a truncated caption, a model reply without the key) must never
        blank an intent we already had, so a lead cannot silently lose its whole
        customer-visible description.
        `initial_status` (gap #4's corroboration gate; default `"new"`, the
        pre-existing behaviour) is the FIRST-INSERT-ONLY status — same rule as
        `status` above, it is never applied on a re-poll of an existing row. Must
        be a `VALID_STATUS` member.
        """
        if initial_status not in VALID_STATUS:
            raise ValueError(f"invalid initial_status: {initial_status!r}")
        with self._tx() as c:
            self._upsert_match_row(
                c, campaign_id=campaign_id, reel_id=reel_id, comment_id=comment_id,
                username=username, text=text, lang=lang, score=score, reason=reason,
                extracted=extracted, tier=tier, session_id=session_id,
                platform=platform, captured_at=captured_at,
                found_by_models=found_by_models, source=source, intent=intent,
                initial_status=initial_status)
        logger.debug("DB upsert_match · campaign=%s comment=%s score=%.2f tier=%s",
                     campaign_id, comment_id, score, tier)

    def _upsert_match_row(self, c: sqlite3.Cursor, *, campaign_id: str, reel_id: str,
                          comment_id: str, username: Optional[str], text: Optional[str],
                          lang: Optional[str], score: float, reason: str,
                          extracted: Optional[dict[str, Any]], tier: str,
                          session_id: Optional[str], platform: str,
                          captured_at: Optional[float],
                          found_by_models: Optional[list[str]] = None,
                          source: Optional[str] = None,
                          intent: Optional[str] = None,
                          initial_status: str = "new") -> None:
        """Run the match upsert on the caller's cursor `c` — the caller owns the
        transaction. Extracted from `upsert_match` so the distributed-worker ack can
        persist a job's leads in the SAME transaction as the job-done marking, making
        the sync-back atomic (a crash rolls back both, never a done-but-leads-lost
        limbo). `status` is written only on first insert (a re-poll never clobbers a
        human-set status, and re-polling with a different `initial_status` is a
        no-op — the ON CONFLICT clause below never touches the `status` column);
        `session_id`/`captured_at` record first-capture provenance."""
        now = time.time()
        cap = captured_at if captured_at is not None else now
        # v24 provenance: the seed that produced this lead. Derived from the reel
        # rather than passed by every engine — `mark_seen` stamped it on first
        # sighting, and deriving keeps ONE writer for the fact. It also fixes the
        # case an engine could not get right anyway: a watchlist re-poll builds a
        # bare Reel with no source, but the seen_reels row still knows.
        if not source:
            row = c.execute(
                "SELECT source FROM seen_reels "
                "WHERE campaign_id=? AND platform=? AND reel_id=?",
                (campaign_id, platform, reel_id)).fetchone()
            source = row["source"] if row else None
        blob = json.dumps(extracted, ensure_ascii=False) if extracted is not None else None
        found_by_blob = (json.dumps(found_by_models, ensure_ascii=False)
                         if found_by_models else None)
        org_id = self.org_for_campaign(campaign_id)
        c.execute(
            """INSERT INTO matches
                 (campaign_id, org_id, platform, reel_id, comment_id, session_id,
                  username, text, lang, score, reason, extracted, status, tier,
                  found_by_models, source, intent, lead_token, captured_at, updated_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
               -- v28: `lead_token` is DELIBERATELY absent from the DO UPDATE below.
               -- A re-poll or a worker sync-back of a lead we already hold must keep
               -- the token the panel is already holding in a URL and a query cache;
               -- re-minting it on every upsert would 404 an open drawer and orphan
               -- the audit rows keyed to it. Minted once, on first insert, forever.
               ON CONFLICT(campaign_id, platform, comment_id) DO UPDATE SET
                 org_id=COALESCE(matches.org_id, excluded.org_id),
                 reel_id=excluded.reel_id,
                 username=excluded.username,
                 text=excluded.text,
                 lang=excluded.lang,
                 score=excluded.score,
                 reason=excluded.reason,
                 extracted=excluded.extracted,
                 tier=excluded.tier,
                 found_by_models=excluded.found_by_models,
                 source=COALESCE(matches.source, excluded.source),
                 intent=COALESCE(excluded.intent, matches.intent),
                 updated_at=excluded.updated_at""",
            (campaign_id, org_id, platform, reel_id, comment_id, session_id, username,
             text, lang, score, reason, blob, initial_status, tier, found_by_blob,
             (source or None), ((intent or "").strip() or None), new_lead_token(),
             cap, now),
        )

    def matches_for_run(self, run_id: str) -> list[dict[str, Any]]:
        """Every match captured during a run, across ALL its sessions — a target-leads
        run loops many sessions and a multi-channel campaign fans out several, all under
        one run_id stamped on each session row. Joined matches→sessions on session_id.

        Used by the distributed worker to ship a completed job's captured leads to the
        cloud on ack. Ordered earliest-capture first so a truncated sync keeps the
        first-found leads; `extracted` JSON is decoded (mirrors `matches`)."""
        rows = self._conn.execute(
            """SELECT m.* FROM matches m
                 JOIN sessions s ON m.session_id = s.session_id
                WHERE s.run_id = ?
                ORDER BY m.captured_at ASC""",
            (run_id,)).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            if d.get("extracted"):
                try:
                    d["extracted"] = json.loads(d["extracted"])
                except json.JSONDecodeError:
                    pass
            if d.get("found_by_models"):
                try:
                    d["found_by_models"] = json.loads(d["found_by_models"])
                except json.JSONDecodeError:
                    d["found_by_models"] = None
            out.append(d)
        return out

    def set_status(self, campaign_id: str, comment_id: str, status: str,
                   platform: str = DEFAULT_PLATFORM, *,
                   user: Optional[dict[str, Any]] = None,
                   reason: Optional[str] = None, log_noop: bool = False) -> bool:
        """Set a lead's status and log WHO/from/to/when/reason atomically.

        Returns True if the lead exists (and was updated), False for an unknown
        comment_id. `user` is the {id,email} of the actor (None only in direct
        unit tests / defensive paths). `reason` is required (non-empty) when moving
        INTO a FORCED_REASON_STATUS. A no-op transition (from==to) is a success but
        writes no audit row unless `log_noop` is set. Raises ValueError on an
        invalid status or a missing forced reason.
        """
        if status not in VALID_STATUS:
            raise ValueError(f"invalid status: {status!r}")
        reason = reason.strip() if isinstance(reason, str) else None
        if status in FORCED_REASON_STATUS and not reason:
            raise ValueError(f"a reason note is required to move a lead to {status!r}")
        now = time.time()
        with self._tx() as c:
            row = c.execute(
                "SELECT status FROM matches "
                "WHERE campaign_id=? AND platform=? AND comment_id=?",
                (campaign_id, platform, comment_id),
            ).fetchone()
            if row is None:
                return False  # unknown comment — no row, no audit
            from_status = row["status"]
            if from_status == status and not log_noop:
                return True  # already in the requested state — skip the audit row
            c.execute(
                "UPDATE matches SET status=?, updated_at=? "
                "WHERE campaign_id=? AND platform=? AND comment_id=?",
                (status, now, campaign_id, platform, comment_id),
            )
            c.execute(
                """INSERT INTO lead_status_changes
                     (campaign_id, platform, comment_id, from_status, to_status,
                      user_id, user_email, reason, created_at)
                   VALUES (?,?,?,?,?,?,?,?,?)""",
                (campaign_id, platform, comment_id, from_status, status,
                 (user or {}).get("id"), (user or {}).get("email"), reason, now),
            )
            return True

    def matches(self, campaign_id: str, status: Optional[str] = None) -> list[dict[str, Any]]:
        q = "SELECT * FROM matches WHERE campaign_id=?"
        args: list[Any] = [campaign_id]
        if status:
            q += " AND status=?"
            args.append(status)
        q += " ORDER BY score DESC, captured_at DESC"
        rows = self._conn.execute(q, args).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            if d.get("extracted"):
                try:
                    d["extracted"] = json.loads(d["extracted"])
                except json.JSONDecodeError:
                    pass
            if d.get("found_by_models"):
                try:
                    d["found_by_models"] = json.loads(d["found_by_models"])
                except json.JSONDecodeError:
                    d["found_by_models"] = None
            out.append(d)
        return out

    # ----- v6 lead notes + status history -----
    def add_note(self, campaign_id: str, comment_id: str, body: str, *,
                 author: dict[str, Any], platform: str = DEFAULT_PLATFORM) -> dict[str, Any]:
        """Append a free-form note to a lead. Returns the created note (with real id).

        Raises ValueError on an empty or over-long body. Does not require the lead
        row to exist (the note attaches by comment_id and survives re-scrapes).
        """
        body = (body or "").strip()
        if not body:
            raise ValueError("note body must not be empty")
        if len(body) > MAX_NOTE_LENGTH:
            raise ValueError(f"note exceeds {MAX_NOTE_LENGTH} characters")
        now = time.time()
        with self._tx() as c:
            cur = c.execute(
                """INSERT INTO lead_notes
                     (campaign_id, platform, comment_id, author_id, author_email,
                      body, created_at)
                   VALUES (?,?,?,?,?,?,?)""",
                (campaign_id, platform, comment_id,
                 author.get("id"), author.get("email"), body, now),
            )
            note_id = int(cur.lastrowid)
        return {"id": note_id, "campaignId": campaign_id, "platform": platform,
                "commentId": comment_id, "body": body,
                "authorId": author.get("id"), "authorEmail": author.get("email"),
                "createdAt": now}

    def delete_note(self, note_id: int, requester_user_id: Optional[int]) -> str:
        """Delete a note iff the requester is its author.

        Returns 'not_found' / 'forbidden' / 'deleted' so the server maps cleanly to
        404 / 403 / 200. A NULL author never matches (fails safe). Deleting a note
        never touches lead_status_changes.reason — the audit trail is immutable.
        """
        with self._tx() as c:
            row = c.execute(
                "SELECT author_id FROM lead_notes WHERE id=?", (note_id,)).fetchone()
            if row is None:
                return "not_found"
            if row["author_id"] is None or row["author_id"] != requester_user_id:
                return "forbidden"
            c.execute("DELETE FROM lead_notes WHERE id=?", (note_id,))
            return "deleted"

    def notes_for(self, campaign_id: str, comment_id: str,
                  platform: str = DEFAULT_PLATFORM) -> list[dict[str, Any]]:
        rows = self._conn.execute(
            "SELECT id, author_id, author_email, body, created_at FROM lead_notes "
            "WHERE campaign_id=? AND platform=? AND comment_id=? ORDER BY created_at",
            (campaign_id, platform, comment_id)).fetchall()
        return [{"id": r["id"], "body": r["body"], "authorId": r["author_id"],
                 "authorEmail": r["author_email"], "createdAt": r["created_at"]}
                for r in rows]

    def status_history(self, campaign_id: str, comment_id: str,
                       platform: str = DEFAULT_PLATFORM) -> list[dict[str, Any]]:
        rows = self._conn.execute(
            "SELECT from_status, to_status, user_id, user_email, reason, created_at "
            "FROM lead_status_changes "
            "WHERE campaign_id=? AND platform=? AND comment_id=? ORDER BY created_at",
            (campaign_id, platform, comment_id)).fetchall()
        return [{"fromStatus": r["from_status"], "toStatus": r["to_status"],
                 "by": r["user_email"], "byId": r["user_id"],
                 "reason": r["reason"], "at": r["created_at"]} for r in rows]

    def notes_by_lead(self, campaign_id: str) -> dict[tuple[str, str], list[dict[str, Any]]]:
        """All notes for a campaign grouped by (platform, comment_id) — one query so
        panel.py avoids an N+1 across leads."""
        rows = self._conn.execute(
            "SELECT platform, comment_id, id, author_id, author_email, body, created_at "
            "FROM lead_notes WHERE campaign_id=? ORDER BY created_at",
            (campaign_id,)).fetchall()
        out: dict[tuple[str, str], list[dict[str, Any]]] = {}
        for r in rows:
            out.setdefault((r["platform"], r["comment_id"]), []).append(
                {"id": r["id"], "body": r["body"], "authorId": r["author_id"],
                 "authorEmail": r["author_email"], "createdAt": r["created_at"]})
        return out

    def status_history_by_lead(self, campaign_id: str) -> dict[tuple[str, str], list[dict[str, Any]]]:
        rows = self._conn.execute(
            "SELECT platform, comment_id, from_status, to_status, user_email, user_id, "
            "reason, created_at FROM lead_status_changes "
            "WHERE campaign_id=? ORDER BY created_at", (campaign_id,)).fetchall()
        out: dict[tuple[str, str], list[dict[str, Any]]] = {}
        for r in rows:
            out.setdefault((r["platform"], r["comment_id"]), []).append(
                {"fromStatus": r["from_status"], "toStatus": r["to_status"],
                 "by": r["user_email"], "byId": r["user_id"],
                 "reason": r["reason"], "at": r["created_at"]})
        return out

    # ----- watchlist -----
    def add_to_watchlist(self, campaign_id: str, reel_id: str, ttl_days: float = 10.0,
                         platform: str = DEFAULT_PLATFORM) -> None:
        now = time.time()
        with self._tx() as c:
            c.execute(
                """INSERT INTO watchlist(campaign_id, platform, reel_id, added_at,
                                         expires_at, match_count)
                   VALUES(?,?,?,?,?,1)
                   ON CONFLICT(campaign_id, platform, reel_id) DO UPDATE SET
                     match_count=watchlist.match_count+1,
                     expires_at=excluded.expires_at""",
                (campaign_id, platform, reel_id, now, now + ttl_days * 86400),
            )

    def active_watchlist(self, campaign_id: str,
                         platform: str = DEFAULT_PLATFORM) -> list[str]:
        rows = self._conn.execute(
            "SELECT reel_id FROM watchlist "
            "WHERE campaign_id=? AND platform=? AND expires_at > ? ORDER BY added_at",
            (campaign_id, platform, time.time()),
        ).fetchall()
        return [r["reel_id"] for r in rows]

    def prune_watchlist(self, campaign_id: str) -> int:
        with self._tx() as c:
            cur = c.execute(
                "DELETE FROM watchlist WHERE campaign_id=? AND expires_at <= ?",
                (campaign_id, time.time()),
            )
            return cur.rowcount

    # ----- sessions -----
    def start_session(self, session_id: str, campaign_id: str,
                      platform: str = DEFAULT_PLATFORM, *,
                      run_id: Optional[str] = None,
                      org_id: Optional[int] = None,
                      engine_mode: str = "harvest",
                      account_id: Optional[int] = None) -> None:
        # v10: stamp run_id/org_id so the activity endpoint can find a run's
        # session(s) and scope by org. org_id falls back to the campaign's owner
        # (the registry is the source of truth), matching raise_flag/log_spend.
        # v11: warming sessions pass engine_mode='warming' + account_id and an
        # EXPLICIT org_id — their campaign_id is the per-org sentinel (§3.2), which
        # has no campaign_meta row, so org_for_campaign would land NULL.
        if org_id is None:
            org_id = self.org_for_campaign(campaign_id)
        # v20: seed the liveness heartbeat at session start — last_activity_at=now
        # (bumped forward by every update_counters call) and pid=this process, so
        # SessionWatchdog can tell "still actively updating" from "wedged" without
        # any new engine call site (every engine already calls update_counters
        # periodically via Session._flush).
        now = time.time()
        with self._tx() as c:
            c.execute(
                """INSERT OR REPLACE INTO sessions(session_id, campaign_id, platform,
                                                   started_at, status, run_id, org_id,
                                                   engine_mode, account_id,
                                                   last_activity_at, pid)
                   VALUES(?,?,?,?, 'running', ?, ?, ?, ?, ?, ?)""",
                (session_id, campaign_id, platform, now, run_id, org_id,
                 engine_mode, account_id, now, os.getpid()),
            )

    def update_counters(self, session_id: str, counters: SessionCounters) -> None:
        with self._tx() as c:
            c.execute(
                """UPDATE sessions SET
                     reels_seen=?, already_seen_skips=?, relevance_passes=?,
                     comments_scored=?, matches=?, escalations=?, transcriptions=?,
                     video_analyses=?, spend_usd=?, feed_health_flag=?,
                     last_activity_at=?
                   WHERE session_id=?""",
                (counters.reels_seen, counters.already_seen_skips, counters.relevance_passes,
                 counters.comments_scored, counters.matches, counters.escalations,
                 counters.transcriptions, counters.video_analyses, counters.spend_usd,
                 int(counters.feed_health_flag), time.time(),
                 session_id),
            )

    def touch_session(self, session_id: str) -> None:
        """Bump ONLY ``sessions.last_activity_at`` (the v20 liveness heartbeat the
        SessionWatchdog reads) — one single-row UPDATE, no read, no counter rewrite.

        Why this exists beside ``update_counters``, which also bumps the column: an
        engine session's ``_flush()`` first runs ``total_spend()`` (an aggregate
        SELECT over spend_log) and then rewrites all nine counter columns. That is
        the right price ONCE PER REEL, but the 2026-08-20 fleet stall
        (job-2099fb29e88b: 5 attempts, every one killed with "stalled: no activity
        for over 180s") forced heartbeats down to per-model-verdict and per-comment
        granularity — dozens of bumps per reel with no counter changed in between.
        This path is what makes that granularity cost one indexed row write instead
        of an aggregate scan plus a nine-column write.

        Deliberately NOT a timer: callers invoke it only after real progress (a
        model verdict came back, a comment was scored, a permalink opened), so a
        genuinely wedged session still goes quiet and is still halted by the
        watchdog. A blind ticker here would disarm the watchdog entirely."""
        with self._tx() as c:
            c.execute(
                "UPDATE sessions SET last_activity_at=? WHERE session_id=?",
                (time.time(), session_id),
            )

    def end_session(self, session_id: str, status: str = "completed",
                    halt_reason: Optional[str] = None) -> None:
        with self._tx() as c:
            c.execute(
                "UPDATE sessions SET ended_at=?, status=?, halt_reason=? WHERE session_id=?",
                (time.time(), status, halt_reason, session_id),
            )

    def get_session(self, session_id: str) -> Optional[dict[str, Any]]:
        row = self._conn.execute(
            "SELECT * FROM sessions WHERE session_id=?", (session_id,)
        ).fetchone()
        return dict(row) if row else None

    def find_stalled_sessions(self, *, stall_timeout_s: float,
                              now: Optional[float] = None) -> list[dict[str, Any]]:
        """Sessions stuck at status='running' whose heartbeat (last_activity_at,
        falling back to started_at for a pre-v20 row that predates the column)
        has gone quiet for over ``stall_timeout_s`` — the SessionWatchdog
        (session_watchdog.py) signal for "wedged with no exception to trip any
        except-guard." Read-only; the caller decides what to do with each row."""
        now_v = now if now is not None else time.time()
        cutoff = now_v - stall_timeout_s
        rows = self._conn.execute(
            """SELECT * FROM sessions
                WHERE status='running' AND ended_at IS NULL
                  AND COALESCE(last_activity_at, started_at) < ?""",
            (cutoff,)).fetchall()
        return [dict(r) for r in rows]

    # ----- health flags -----
    def raise_flag(self, kind: str, severity: str, detail: str = "",
                   campaign_id: Optional[str] = None,
                   session_id: Optional[str] = None, *,
                   org_id: Optional[int] = None,
                   account_id: Optional[int] = None) -> None:
        # v11: a pool warming session backs an ACCOUNT, not a campaign — its
        # campaign_id is the §3.2 sentinel, so deriving org from it lands NULL and
        # the panel banner would never surface the flag. Warming passes org_id +
        # account_id explicitly; harvest keeps deriving org from the campaign.
        if org_id is None:
            org_id = self.org_for_campaign(campaign_id)
        with self._tx() as c:
            c.execute(
                """INSERT INTO health_flags(campaign_id, org_id, session_id, kind,
                                            severity, detail, created_at, account_id)
                   VALUES(?,?,?,?,?,?,?,?)""",
                (campaign_id, org_id, session_id, kind, severity, detail,
                 time.time(), account_id),
            )
        logger.debug("DB raise_flag · [%s] %s · %s", severity, kind, detail)

    def open_flags(self, org_id: Optional[int] = None,
                   severity: Optional[str] = None) -> list[dict[str, Any]]:
        """Unresolved flags. Scoped to `org_id` when given (the panel halt banner is
        per-org); None returns all (engine/CLI use)."""
        q = "SELECT * FROM health_flags WHERE resolved_at IS NULL"
        args: list[Any] = []
        if org_id is not None:
            q += " AND org_id=?"
            args.append(org_id)
        if severity:
            q += " AND severity=?"
            args.append(severity)
        q += " ORDER BY created_at DESC"
        return [dict(r) for r in self._conn.execute(q, args).fetchall()]

    # ----- self-healing cooldown (gap #1) -----
    def record_soft_halt(self, campaign_id: str, platform: str, kind: str, *,
                         account_id: Optional[int] = None,
                         now: Optional[float] = None) -> dict[str, Any]:
        """Escalate the (campaign_id, platform) cooldown for a SOFT halt — a
        rate-limit-shaped signal (action_block/canary; see engines/base.py's
        SOFT_HALT_KINDS) that should resolve itself with time, NOT wait on a human.

        Bumps `attempt` (consecutive soft halts since the last clean session — see
        `clear_cooldown`) and sets `cooldown_until` to an exponential backoff
        deadline: COOLDOWN_BASE_SECONDS * 2**(attempt-1), capped at
        COOLDOWN_MAX_SECONDS, from `now`. One row per (campaign_id, platform) —
        callers must NEVER call this for a HARD halt (checkpoint/login); those stay
        human-gated via raise_flag/resolve_flag exactly as before. Returns the
        persisted row."""
        now_v = now if now is not None else time.time()
        with self._tx() as c:
            row = c.execute(
                "SELECT attempt FROM session_cooldowns WHERE campaign_id=? AND platform=?",
                (campaign_id, platform)).fetchone()
            attempt = (row["attempt"] if row else 0) + 1
            cooldown_until = now_v + min(
                COOLDOWN_MAX_SECONDS, COOLDOWN_BASE_SECONDS * (2 ** (attempt - 1)))
            c.execute(
                """INSERT INTO session_cooldowns(campaign_id, platform, account_id,
                                                  attempt, cooldown_until, last_kind,
                                                  updated_at)
                   VALUES(?,?,?,?,?,?,?)
                   ON CONFLICT(campaign_id, platform) DO UPDATE SET
                     account_id=excluded.account_id, attempt=excluded.attempt,
                     cooldown_until=excluded.cooldown_until,
                     last_kind=excluded.last_kind, updated_at=excluded.updated_at""",
                (campaign_id, platform, account_id, attempt, cooldown_until, kind,
                 now_v))
        logger.info("DB record_soft_halt · campaign=%s platform=%s kind=%s "
                   "attempt=%d cooldown=+%.0fs", campaign_id, platform, kind,
                   attempt, cooldown_until - now_v)
        return {"campaign_id": campaign_id, "platform": platform,
                "account_id": account_id, "attempt": attempt,
                "cooldown_until": cooldown_until, "last_kind": kind,
                "updated_at": now_v}

    def get_cooldown(self, campaign_id: str, platform: str) -> Optional[dict[str, Any]]:
        """The persisted cooldown row for (campaign_id, platform), or None if it has
        never soft-halted (or was cleared by a subsequent clean session). A process
        restart rehydrates by simply reading this row — the table IS the state, no
        separate warm-up step exists or is needed."""
        row = self._conn.execute(
            "SELECT * FROM session_cooldowns WHERE campaign_id=? AND platform=?",
            (campaign_id, platform)).fetchone()
        return dict(row) if row else None

    def cooldown_remaining(self, campaign_id: str, platform: str, *,
                          now: Optional[float] = None) -> float:
        """Seconds still left on this (campaign_id, platform)'s cooldown; 0.0 once it
        has elapsed (or nothing was ever recorded)."""
        row = self.get_cooldown(campaign_id, platform)
        if row is None or row["cooldown_until"] is None:
            return 0.0
        now_v = now if now is not None else time.time()
        return max(0.0, row["cooldown_until"] - now_v)

    def clear_cooldown(self, campaign_id: str, platform: str) -> None:
        """Reset the escalation after a clean successful session (gap #1) — the next
        soft halt starts back at attempt 1 instead of continuing a stale streak."""
        with self._tx() as c:
            c.execute(
                "DELETE FROM session_cooldowns WHERE campaign_id=? AND platform=?",
                (campaign_id, platform))

    # ----- spend -----
    def log_spend(self, campaign_id: str, stage: str, usd: float,
                  model: Optional[str] = None, session_id: Optional[str] = None) -> None:
        with self._tx() as c:
            c.execute(
                """INSERT INTO spend_log(campaign_id, session_id, stage, model, usd, created_at)
                   VALUES(?,?,?,?,?,?)""",
                (campaign_id, session_id, stage, model, usd, time.time()),
            )
        logger.debug("DB log_spend · campaign=%s stage=%s model=%s usd=$%.4f",
                     campaign_id, stage, model, usd)

    def total_spend(self, campaign_id: str) -> float:
        row = self._conn.execute(
            "SELECT COALESCE(SUM(usd),0) AS t FROM spend_log WHERE campaign_id=?",
            (campaign_id,),
        ).fetchone()
        return float(row["t"])

    def max_spend_id(self, campaign_id: str) -> int:
        """The high-water mark of `spend_log.id` FOR ONE CAMPAIGN — the cursor a worker
        takes BEFORE a run so it can ship exactly that run's delta on ack/nack (B9).

        An id cursor, NOT a run_id join: a requeued attempt REUSES the job's run_id
        (`nack_job` reads it back out of the unchanged spec), so a run-scoped query would
        re-report attempt 1's rows on a same-box retry.

        SCOPED TO THE CAMPAIGN, matching `spend_since`'s own `WHERE campaign_id=?`, so
        an unrelated campaign's write can never move this campaign's mark. NOTE that the
        cursor is NOT what keeps two overlapping attempts on ONE campaign from
        re-reporting each other's rows — no choice of mark can, since the second
        attempt's rows land inside the first's window either way. `spend_since`'s
        `run_id` filter is what does that; see its docstring. The old docstring here
        claimed `JobSpec.lock_key()` made overlap impossible, which is false: that key is
        `f"{org_id}-{platform}-{account or '_default'}"` — per (org, PLATFORM), not per
        campaign."""
        row = self._conn.execute(
            "SELECT COALESCE(MAX(id),0) AS m FROM spend_log WHERE campaign_id=?",
            (campaign_id,)).fetchone()
        return int(row["m"])

    def spend_since(self, campaign_id: str, after_id: int,
                    run_id: Optional[str] = None) -> list[dict[str, Any]]:
        """This campaign's spend rows newer than `after_id`, rolled up per (stage,
        model) — the compact delta a worker ships back on ack/nack (see
        `max_spend_id`). `at` is the EARLIEST created_at in each group so the cloud can
        stamp the rollup row near when the money was actually spent rather than at ack
        time (otherwise `spend_by_day`, which buckets on created_at, puts a whole run's
        spend on the ack day and skews a run that spanned midnight).

        `run_id` EXCLUDES rows that provably belong to a DIFFERENT run, and without it
        the id cursor alone is not enough. Two jobs for the SAME campaign on DIFFERENT
        platforms take different `JobSpec.lock_key()` single-flight locks (the key is
        `org-platform-account`, i.e. per platform, NOT per campaign), so on a box running
        more than one sidecar against one AIZU_DB they overlap — and each attempt's
        window then contains the other's rows. Narrowing the cursor cannot fix that: B's
        rows land INSIDE A's window no matter where A's mark is. Since the cloud
        `spend_log` has an AUTOINCREMENT PK and no unique key, both reports are inserted
        and the campaign's spend is inflated (its effective cap correspondingly halved).

        Rows with NO session (an LLM call made outside a session) are always KEPT: they
        cannot be attributed to another run, and dropping them would lose real money from
        the roll-up. Rows of a PRIOR ATTEMPT of this same run are kept too — a requeue
        reuses the run_id, and with the sidecar's parked cursor those dollars have not
        been banked yet and must still be reported."""
        sql = ["""SELECT stage, model, COALESCE(SUM(usd),0) AS usd, MIN(created_at) AS at
                    FROM spend_log
                   WHERE campaign_id=? AND id>?"""]
        params: list[Any] = [campaign_id, int(after_id)]
        if run_id:
            sql.append("""AND (session_id IS NULL
                               OR session_id NOT IN (
                                   SELECT session_id FROM sessions
                                    WHERE run_id IS NOT NULL AND run_id<>?))""")
            params.append(run_id)
        sql.append("GROUP BY stage, model HAVING SUM(usd) > 0")
        rows = self._conn.execute(" ".join(sql), tuple(params)).fetchall()
        return [{"stage": r["stage"], "model": r["model"],
                 "usd": float(r["usd"]), "at": r["at"]} for r in rows]

    # ----- engagement actions -----
    def log_action(self, campaign_id: str, action_type: str, *,
                   reel_id: Optional[str] = None,
                   target: Optional[str], succeeded: bool,
                   session_id: Optional[str] = None,
                   account_id: Optional[int] = None,
                   now: Optional[float] = None) -> None:
        # v11: warming follows/connects have no reel and no real campaign — they
        # pass the §3.2 sentinel campaign_id, reel_id=None, and account_id (the
        # warmth `network` join key, §5.2). Harvest call sites are unchanged.
        # `now` lets a clock-injected caller (the warming executor) stamp the same
        # clock that action_counts_for_account_day reads back against, so the
        # per-day bucket is consistent under an injected `now` (defaults to the
        # real clock for every existing call site).
        with self._tx() as c:
            c.execute(
                """INSERT INTO actions(campaign_id, session_id, reel_id, action_type,
                                       target, succeeded, created_at, account_id)
                   VALUES(?,?,?,?,?,?,?,?)""",
                (campaign_id, session_id, reel_id, action_type, target,
                 int(succeeded), time.time() if now is None else now, account_id),
            )

    def action_counts(self, campaign_id: str,
                      session_id: Optional[str] = None) -> dict[str, int]:
        """Successful action counts by type (for the panel / status)."""
        q = ("SELECT action_type, COUNT(*) AS n FROM actions "
             "WHERE campaign_id=? AND succeeded=1")
        args: list[Any] = [campaign_id]
        if session_id:
            q += " AND session_id=?"
            args.append(session_id)
        q += " GROUP BY action_type"
        rows = self._conn.execute(q, args).fetchall()
        return {r["action_type"]: r["n"] for r in rows}

    def action_counts_for_account_day(self, account_id: int, *, now: float,
                                      action_type: Optional[str] = None):
        """SUCCESSFUL warming actions by this account on the CURRENT local day,
        keyed on account_id (NOT a campaign_id) so per-day caps stay robust across
        multiple warming sessions in the same day (warming-writes PRD §3.7, X6).

        Day bucketing reuses the Tashkent +5h idiom (`_TASHKENT_OFFSET`), the same
        local-day grouping `_warmth_signals` uses. With `action_type` set, returns
        an int count for that one type; otherwise a {action_type: count} dict for
        the day. Additive read method — no DDL, no SCHEMA_VERSION bump.
        """
        today = int((now + self._TASHKENT_OFFSET) // 86400)
        lo = today * 86400 - self._TASHKENT_OFFSET
        hi = lo + 86400
        q = ("SELECT action_type, COUNT(*) AS n FROM actions "
             "WHERE account_id=? AND succeeded=1 AND created_at>=? AND created_at<?")
        args: list[Any] = [account_id, lo, hi]
        if action_type is not None:
            q += " AND action_type=?"
            args.append(action_type)
        q += " GROUP BY action_type"
        rows = self._conn.execute(q, args).fetchall()
        counts = {r["action_type"]: r["n"] for r in rows}
        if action_type is not None:
            return counts.get(action_type, 0)
        return counts

    # ----- v11: account warming pool -----
    # JSON columns parsed on read; the lifecycle guard + audit live in
    # core/accounts.py (warming PRD §3). All warmth signals join on account_id.

    # Mutable columns update_account_lifecycle may set alongside the state change.
    _ACCOUNT_MUTABLE_COLS = frozenset({
        "consecutive_flag_count", "last_warmed_at", "last_active_at",
        "cooling_until", "detail", "ramp_day", "warmth_floor"})

    @staticmethod
    def _account_row(row: Optional[sqlite3.Row]) -> Optional[dict[str, Any]]:
        """Row → dict with `fingerprint`/`detail` JSON decoded (tolerant — a bad
        blob becomes None, never a crash; PRD untrusted-text rule)."""
        if row is None:
            return None
        d = dict(row)
        for key in ("fingerprint", "detail"):
            raw = d.get(key)
            if isinstance(raw, str) and raw:
                try:
                    d[key] = json.loads(raw)
                except (json.JSONDecodeError, TypeError):
                    d[key] = None
        return d

    def add_account(self, org_id: int, platform: str, username: str, *,
                    profile_dir: Optional[str] = None,
                    cdp_port: Optional[int] = None,
                    fingerprint: Optional[dict[str, Any]] = None,
                    detail: Optional[dict[str, Any]] = None,
                    state: str = accounts_lib.PROVISIONED) -> int:
        """Record a managed account; returns its id. Rejects non-warmable platforms
        (§11) and an invalid initial state. Idempotent on (org, platform, username):
        an existing row's id is returned unchanged (UNIQUE constraint), so the v1
        legacy-account synthesis is safe to call repeatedly."""
        if not accounts_lib.is_warmable(platform):
            raise ValueError(
                f"platform {platform!r} is not warmable; expected one of "
                f"{', '.join(sorted(accounts_lib.WARMABLE_PLATFORMS))}")
        if state not in accounts_lib.ACCOUNT_STATES:
            raise ValueError(f"invalid account state: {state!r}")
        existing = self._conn.execute(
            "SELECT id FROM accounts WHERE org_id=? AND platform=? AND username=?",
            (org_id, platform, username)).fetchone()
        if existing is not None:
            return int(existing["id"])
        now = time.time()
        fp = json.dumps(fingerprint, ensure_ascii=False) if fingerprint else None
        dt = json.dumps(detail, ensure_ascii=False) if detail else None
        with self._tx() as c:
            cur = c.execute(
                """INSERT INTO accounts(org_id, platform, username, state, profile_dir,
                                        cdp_port, fingerprint, detail, added_at, updated_at)
                   VALUES(?,?,?,?,?,?,?,?,?,?)""",
                (org_id, platform, username, state, profile_dir, cdp_port, fp, dt,
                 now, now))
            return int(cur.lastrowid)

    def get_account(self, account_id: int) -> Optional[dict[str, Any]]:
        return self._account_row(self._conn.execute(
            "SELECT * FROM accounts WHERE id=?", (account_id,)).fetchone())

    def list_accounts(self, org_id: int, platform: Optional[str] = None,
                      state: Optional[str] = None) -> list[dict[str, Any]]:
        q = "SELECT * FROM accounts WHERE org_id=?"
        args: list[Any] = [org_id]
        if platform:
            q += " AND platform=?"
            args.append(platform)
        if state:
            q += " AND state=?"
            args.append(state)
        q += " ORDER BY id"
        return [self._account_row(r) for r in self._conn.execute(q, args).fetchall()]

    def update_account_lifecycle(self, account_id: int, to_state: str, *,
                                 reason: Optional[str] = None,
                                 session_id: Optional[str] = None,
                                 **fields: Any) -> dict[str, Any]:
        """Transition an account's lifecycle state and append an audit row in ONE
        transaction. Raises InvalidAccountTransition when the move is not allowed
        (§3.1) and KeyError for an unknown account. Extra `fields` (whitelisted in
        `_ACCOUNT_MUTABLE_COLS`) are written alongside; `detail` is JSON-encoded if
        a dict. Returns the updated account dict."""
        row = self._conn.execute(
            "SELECT * FROM accounts WHERE id=?", (account_id,)).fetchone()
        if row is None:
            raise KeyError(f"no account {account_id}")
        from_state = row["state"]
        if not accounts_lib.can_transition(from_state, to_state):
            raise accounts_lib.InvalidAccountTransition(from_state, to_state)
        sets = ["state=?", "updated_at=?"]
        args: list[Any] = [to_state, time.time()]
        for key, val in fields.items():
            if key not in self._ACCOUNT_MUTABLE_COLS:
                raise ValueError(f"non-mutable account column: {key!r}")
            if key == "detail" and isinstance(val, dict):
                val = json.dumps(val, ensure_ascii=False)
            sets.append(f"{key}=?")
            args.append(val)
        args.append(account_id)
        with self._tx() as c:
            c.execute(f"UPDATE accounts SET {', '.join(sets)} WHERE id=?", args)
            # Audit only a REAL state change (a no-op re-stamp writes no row).
            if from_state != to_state:
                c.execute(
                    """INSERT INTO account_state_changes(account_id, org_id, from_state,
                                                         to_state, reason, session_id, created_at)
                       VALUES(?,?,?,?,?,?,?)""",
                    (account_id, row["org_id"], from_state, to_state, reason,
                     session_id, time.time()))
        return self.get_account(account_id)

    def update_account_ramp(self, account_id: int, *, ramp_day: int,
                            warmth_floor: float,
                            last_warmed_at: Optional[float] = None) -> dict[str, Any]:
        """Persist ramp progress (no state change). Raises KeyError if unknown."""
        if self.get_account(account_id) is None:
            raise KeyError(f"no account {account_id}")
        with self._tx() as c:
            c.execute(
                """UPDATE accounts SET ramp_day=?, warmth_floor=?,
                     last_warmed_at=COALESCE(?, last_warmed_at), updated_at=?
                   WHERE id=?""",
                (ramp_day, warmth_floor, last_warmed_at, time.time(), account_id))
        return self.get_account(account_id)

    def account_state_changes(self, account_id: int) -> list[dict[str, Any]]:
        """Append-only lifecycle audit trail, oldest first."""
        return [dict(r) for r in self._conn.execute(
            "SELECT * FROM account_state_changes WHERE account_id=? ORDER BY id",
            (account_id,)).fetchall()]

    def resolve_account_for_campaign(self, campaign_id: str, platform: str
                                     ) -> Optional[dict[str, Any]]:
        """The pool account backing a campaign on `platform`, or None.

        Returns None (NOT an error) for a non-warmable platform or when no pool
        account exists — `warmth_for_campaign` short-circuits to the neutral
        default on None (§5.8, gap #6). A pinned/assigned account
        (`campaign_accounts`) wins; otherwise pool-pick the most harvest-ready,
        most-recently-warmed account. Asserts the resolved account's org matches
        the campaign's owner (cross-org integrity, §3.3)."""
        if not accounts_lib.is_warmable(platform):
            return None
        org_id = self.org_for_campaign(campaign_id)
        assigned = self._conn.execute(
            "SELECT account_id FROM campaign_accounts WHERE campaign_id=? AND platform=?",
            (campaign_id, platform)).fetchone()
        if assigned is not None:
            acct = self.get_account(int(assigned["account_id"]))
            if acct is None:
                return None
            if org_id is not None and acct["org_id"] != org_id:
                raise ValueError(
                    f"campaign_accounts row crosses org boundary: account "
                    f"{acct['id']} (org {acct['org_id']}) vs campaign org {org_id}")
            return acct
        if org_id is None:
            return None
        # Pool pick: prefer harvest-eligible, then most-recently warmed / newest.
        rows = self._conn.execute(
            "SELECT * FROM accounts WHERE org_id=? AND platform=? AND state!=?",
            (org_id, platform, accounts_lib.FLAGGED)).fetchall()
        if not rows:
            return None
        def _rank(r: sqlite3.Row) -> tuple:
            return (r["state"] in accounts_lib.HARVEST_ELIGIBLE,
                    r["last_warmed_at"] or 0.0, r["id"])
        return self._account_row(max(rows, key=_rank))

    def assign_account(self, campaign_id: str, platform: str, account_id: int, *,
                       pinned: bool = False) -> None:
        """Pin/assign a backing account to a campaign. Rejects a cross-org pairing
        (§3.3) — read-scoping alone is insufficient."""
        acct = self.get_account(account_id)
        if acct is None:
            raise KeyError(f"no account {account_id}")
        org_id = self.org_for_campaign(campaign_id)
        if org_id is not None and acct["org_id"] != org_id:
            raise ValueError(
                f"cannot assign account {account_id} (org {acct['org_id']}) to "
                f"campaign {campaign_id!r} (org {org_id}) — cross-org")
        with self._tx() as c:
            c.execute(
                """INSERT INTO campaign_accounts(campaign_id, org_id, platform,
                                                 account_id, pinned, assigned_at)
                   VALUES(?,?,?,?,?,?)
                   ON CONFLICT(campaign_id, platform) DO UPDATE SET
                     account_id=excluded.account_id, pinned=excluded.pinned,
                     org_id=excluded.org_id, assigned_at=excluded.assigned_at""",
                (campaign_id, acct["org_id"], platform, account_id,
                 int(pinned), time.time()))

    def unassign_account(self, campaign_id: str, platform: str) -> None:
        """Drop a campaign's backing-account assignment (reassign-on-cool, §6.4)."""
        with self._tx() as c:
            c.execute(
                "DELETE FROM campaign_accounts WHERE campaign_id=? AND platform=?",
                (campaign_id, platform))

    def resolve_flag(self, flag_id: int, org_id: int, *,
                     to_state: Optional[str] = None) -> bool:
        """SOLE writer of `health_flags.resolved_at`. In ONE transaction: verify the
        flag belongs to `org_id` (reject cross-org → False), set resolved_at, and —
        when `to_state` is given and the flag is account-keyed — apply the lifecycle
        transition so an operator can never resolve a flag yet leave the account
        stuck in `flagged` (§3.3, gap #7). Returns False for an unknown/cross-org
        flag; raises InvalidAccountTransition if the requested move is illegal."""
        row = self._conn.execute(
            "SELECT id, org_id, account_id, resolved_at FROM health_flags WHERE id=?",
            (flag_id,)).fetchone()
        if row is None or row["org_id"] != org_id:
            return False
        account_id = row["account_id"]
        if to_state is not None and account_id is not None:
            acct = self._conn.execute(
                "SELECT state FROM accounts WHERE id=?", (account_id,)).fetchone()
            if acct is not None and not accounts_lib.can_transition(acct["state"], to_state):
                raise accounts_lib.InvalidAccountTransition(acct["state"], to_state)
        with self._tx() as c:
            c.execute("UPDATE health_flags SET resolved_at=? WHERE id=?",
                      (time.time(), flag_id))
            if to_state is not None and account_id is not None:
                acct = c.execute("SELECT * FROM accounts WHERE id=?",
                                 (account_id,)).fetchone()
                if acct is not None and acct["state"] != to_state:
                    c.execute("UPDATE accounts SET state=?, updated_at=? WHERE id=?",
                              (to_state, time.time(), account_id))
                    c.execute(
                        """INSERT INTO account_state_changes(account_id, org_id, from_state,
                                                             to_state, reason, created_at)
                           VALUES(?,?,?,?,?,?)""",
                        (account_id, org_id, acct["state"], to_state,
                         "flag_resolved", time.time()))
        return True

    # ----- v11: per-account encrypted secrets (Fernet via SecretCipher) -----
    def put_account_secret(self, org_id: int, platform: str, account_id: int,
                           secret: dict[str, Any]) -> None:
        """Encrypt + upsert a per-account secret (proxy creds / cookie-backup /
        MTProto session). Raises SecretCipherError if no key is configured."""
        blob = self._cipher().encrypt(secret)
        with self._tx() as c:
            c.execute(
                """INSERT INTO account_secrets(org_id, platform, account_id, enc_blob, updated_at)
                   VALUES(?,?,?,?,?)
                   ON CONFLICT(org_id, platform, account_id) DO UPDATE SET
                     enc_blob=excluded.enc_blob, updated_at=excluded.updated_at""",
                (org_id, platform, account_id, blob, time.time()))

    def get_account_secret(self, org_id: int, platform: str, account_id: int
                           ) -> Optional[dict[str, Any]]:
        """Decrypt the per-account secret, or None if none stored."""
        row = self._conn.execute(
            "SELECT enc_blob FROM account_secrets "
            "WHERE org_id=? AND platform=? AND account_id=?",
            (org_id, platform, account_id)).fetchone()
        if row is None:
            return None
        return self._cipher().decrypt(row["enc_blob"])

    # ----- v11: warmth read-model (computed-on-read, never a column; §5) -----
    # Fixed Tashkent (+5h) day bucketing matches the panel — the dashboard period
    # filter does NOT apply to warmth windows (§5.6). Single producer: panel_org
    # calls these, never re-derives, so every endpoint returns identical scores.
    _TASHKENT_OFFSET = 5 * 3600

    def _warmth_signals(self, account: dict[str, Any], *, now: float
                        ) -> "warmth_lib.WarmthInputs":
        """Query the insert-only raw signals for one account (§5.2), all joined on
        account_id. Returns the pure-function input bundle."""
        acct_id = account["id"]
        platform = account["platform"]
        # age = days "under management" (NOT account age, §3.5); prefer a real
        # genesis date if the operator supplied one at warm-register.
        detail = account.get("detail") if isinstance(account.get("detail"), dict) else None
        genesis = (detail or {}).get("genesis_at") or account.get("added_at") or now
        age_days = max(0.0, (now - float(genesis)) / 86400.0)

        # ramp = distinct local-days with a COMPLETED warming session in 14d.
        since_ramp = now - warmth_lib.RAMP_WINDOW_DAYS * 86400.0
        starts = self._conn.execute(
            """SELECT started_at FROM sessions
               WHERE account_id=? AND engine_mode='warming' AND status='completed'
                 AND started_at>=?""",
            (acct_id, since_ramp)).fetchall()
        days = {int((r["started_at"] + self._TASHKENT_OFFSET) // 86400) for r in starts}

        # network = cumulative successful follow/connect/join (the C1 write signal).
        # 'join' is Telegram's channels-joined analog (PRD §9.2); IG/X/LinkedIn
        # never log 'join', so this widening does not change their counts.
        net = self._conn.execute(
            """SELECT COUNT(*) AS n FROM actions
               WHERE account_id=? AND succeeded=1
                 AND action_type IN ('follow','connect','join')""",
            (acct_id,)).fetchone()["n"]

        # trust = unresolved warming-challenge flags in 14d.
        since_trust = now - warmth_lib.TRUST_WINDOW_DAYS * 86400.0
        kinds = tuple(warmth_lib.WARMING_CHALLENGE_KINDS)
        placeholders = ",".join("?" * len(kinds))
        flags = self._conn.execute(
            f"""SELECT kind, severity, created_at FROM health_flags
                WHERE account_id=? AND resolved_at IS NULL
                  AND created_at>=? AND kind IN ({placeholders})""",
            (acct_id, since_trust, *kinds)).fetchall()
        return warmth_lib.WarmthInputs(
            platform=platform, age_days=age_days, ramp_completed_days=len(days),
            network_successes=int(net), detail=detail,
            open_challenge_flags=[dict(f) for f in flags])

    def account_warmth(self, org_id: int, platform: str, account_id: int, *,
                       now: Optional[float] = None) -> "warmth_lib.WarmthScore":
        """Warmth for a specific pool account (IntegrationsPanel tile, §5). Neutral
        default for an unknown/foreign account."""
        now = time.time() if now is None else now
        account = self.get_account(account_id)
        if account is None or account.get("org_id") != org_id:
            return warmth_lib.neutral_default()
        return warmth_lib.compute(self._warmth_signals(account, now=now), now=now)

    def warmth_for_campaign(self, campaign_id: str, *, now: Optional[float] = None,
                            platform: Optional[str] = None) -> "warmth_lib.WarmthScore":
        """Per-campaign warmth = the score of its resolved backing account (pool
        model, §5/§6). Short-circuits to the neutral default when no warmable
        account backs the campaign (non-warmable platform, un-onboarded, §5.8)."""
        now = time.time() if now is None else now
        if platform is None:
            brief = self.get_campaign_brief(campaign_id)
            platform = (brief or {}).get("platform", DEFAULT_PLATFORM)
        if not accounts_lib.is_warmable(platform):
            return warmth_lib.neutral_default()
        account = self.resolve_account_for_campaign(campaign_id, platform)
        if account is None:
            return warmth_lib.neutral_default()
        return warmth_lib.compute(self._warmth_signals(account, now=now), now=now)

    # ----- read surfaces for the admin panel -----
    def all_sessions(self, campaign_id: str) -> list[dict[str, Any]]:
        rows = self._conn.execute(
            "SELECT * FROM sessions WHERE campaign_id=? ORDER BY started_at",
            (campaign_id,),
        ).fetchall()
        return [dict(r) for r in rows]

    def reels(self, campaign_id: str, only_relevant: bool = False) -> list[dict[str, Any]]:
        """Seen reels joined with watchlist + match counts (for watchlist view)."""
        q = """
            SELECT s.platform, s.reel_id, s.author, s.caption, s.ocr_text, s.relevant,
                   s.transcript, s.transcript_lang,
                   s.first_seen, s.last_seen,
                   w.added_at, w.expires_at, w.match_count,
                   (SELECT COUNT(*) FROM matches m
                     WHERE m.campaign_id=s.campaign_id AND m.platform=s.platform
                       AND m.reel_id=s.reel_id) AS matches
            FROM seen_reels s
            LEFT JOIN watchlist w
              ON w.campaign_id=s.campaign_id AND w.platform=s.platform
                 AND w.reel_id=s.reel_id
            WHERE s.campaign_id=?
        """
        args: list[Any] = [campaign_id]
        if only_relevant:
            q += " AND s.relevant=1"
        q += " ORDER BY s.last_seen DESC"
        return [dict(r) for r in self._conn.execute(q, args).fetchall()]

    def spend_entries(self, campaign_id: str) -> list[dict[str, Any]]:
        rows = self._conn.execute(
            "SELECT * FROM spend_log WHERE campaign_id=? ORDER BY created_at",
            (campaign_id,),
        ).fetchall()
        return [dict(r) for r in rows]

    def all_flags(self, campaign_id: Optional[str] = None) -> list[dict[str, Any]]:
        if campaign_id:
            rows = self._conn.execute(
                "SELECT * FROM health_flags WHERE campaign_id=? OR campaign_id IS NULL "
                "ORDER BY created_at DESC", (campaign_id,)).fetchall()
        else:
            rows = self._conn.execute(
                "SELECT * FROM health_flags ORDER BY created_at DESC").fetchall()
        return [dict(r) for r in rows]

    # ----- v3 aggregations (dashboard / reports) -----
    def matches_by_day(self, campaign_id: str, since_ts: Optional[float] = None,
                       until_ts: Optional[float] = None,
                       platform: Optional[str] = None) -> list[dict[str, Any]]:
        """Sparse [{day:'YYYY-MM-DD', n:int}] of matches captured in the window."""
        args: list[Any] = [campaign_id]
        q = (f"SELECT strftime('%Y-%m-%d', datetime(captured_at,'unixepoch','{TZ_SQL_SHIFT}')) "
             "AS day, COUNT(*) AS n FROM matches WHERE campaign_id=?")
        if platform:
            q += " AND platform=?"
            args.append(platform)
        q += _time_filter("captured_at", since_ts, until_ts, args)
        q += " GROUP BY day ORDER BY day"
        return [{"day": r["day"], "n": r["n"]} for r in self._conn.execute(q, args).fetchall()]

    def matches_by_hour(self, campaign_id: str, since_ts: Optional[float] = None,
                        until_ts: Optional[float] = None,
                        platform: Optional[str] = None) -> dict[int, int]:
        """Hour-of-day (0..23) → match count, for the best-hour heatmap."""
        args: list[Any] = [campaign_id]
        q = (f"SELECT CAST(strftime('%H', datetime(captured_at,'unixepoch','{TZ_SQL_SHIFT}')) "
             "AS INTEGER) AS hr, COUNT(*) AS n FROM matches WHERE campaign_id=?")
        if platform:
            q += " AND platform=?"
            args.append(platform)
        q += _time_filter("captured_at", since_ts, until_ts, args)
        q += " GROUP BY hr"
        return {int(r["hr"]): r["n"] for r in self._conn.execute(q, args).fetchall()}

    def matches_by_platform(self, campaign_id: str, since_ts: Optional[float] = None,
                            until_ts: Optional[float] = None) -> dict[str, int]:
        """Platform → match count in the window (current-vs-previous comparison)."""
        args: list[Any] = [campaign_id]
        q = "SELECT platform, COUNT(*) AS n FROM matches WHERE campaign_id=?"
        q += _time_filter("captured_at", since_ts, until_ts, args)
        q += " GROUP BY platform"
        return {r["platform"]: r["n"] for r in self._conn.execute(q, args).fetchall()}

    def won_count(self, campaign_id: str, since_ts: Optional[float] = None,
                  until_ts: Optional[float] = None,
                  platform: Optional[str] = None) -> int:
        """Count of leads in a WIN_STATUS (the CPL / conversion numerator)."""
        placeholders = ",".join("?" for _ in WIN_STATUS)
        args: list[Any] = [campaign_id, *sorted(WIN_STATUS)]
        q = (f"SELECT COUNT(*) AS n FROM matches "
             f"WHERE campaign_id=? AND status IN ({placeholders})")
        if platform:
            q += " AND platform=?"
            args.append(platform)
        q += _time_filter("captured_at", since_ts, until_ts, args)
        return int(self._conn.execute(q, args).fetchone()["n"])

    def status_breakdown(self, campaign_id: str, since_ts: Optional[float] = None,
                         until_ts: Optional[float] = None,
                         platform: Optional[str] = None) -> dict[str, int]:
        """Per-status lead counts (all VALID_STATUS keys, zero-filled for a stable
        shape). Bucketed by captured_at — "of leads captured in this window, where
        are they now"."""
        args: list[Any] = [campaign_id]
        q = "SELECT status, COUNT(*) AS n FROM matches WHERE campaign_id=?"
        if platform:
            q += " AND platform=?"
            args.append(platform)
        q += _time_filter("captured_at", since_ts, until_ts, args)
        q += " GROUP BY status"
        counts = {s: 0 for s in VALID_STATUS}
        for r in self._conn.execute(q, args).fetchall():
            if r["status"] in counts:
                counts[r["status"]] = int(r["n"])
        return counts

    def pipeline_conversion(self, campaign_id: str, since_ts: Optional[float] = None,
                            until_ts: Optional[float] = None,
                            platform: Optional[str] = None) -> dict[str, Any]:
        """Win-rate / engagement over the captured-in-window leads, built on
        status_breakdown. `won` = Σ WIN_STATUS; `engaged` = anything past 'new'."""
        b = self.status_breakdown(campaign_id, since_ts, until_ts, platform)
        total = sum(b.values())
        won = sum(b[s] for s in WIN_STATUS)
        engaged = total - b["new"]
        lost = b["closed"] + b["couldnt_connect"] + b["archived"]
        return {"total": total, "won": won,
                "winRate": round(won / total, 4) if total else 0.0,
                "engagedRate": round(engaged / total, 4) if total else 0.0,
                "lost": lost}

    def status_changes_by_user(self, campaign_id: str, since_ts: Optional[float] = None,
                               until_ts: Optional[float] = None) -> list[dict[str, Any]]:
        """[{userId, email, changes}] — status transitions logged per actor in the
        window (bucketed by when the change happened)."""
        args: list[Any] = [campaign_id]
        q = ("SELECT user_id, user_email, COUNT(*) AS n FROM lead_status_changes "
             "WHERE campaign_id=?")
        q += _time_filter("created_at", since_ts, until_ts, args)
        q += " GROUP BY user_id, user_email ORDER BY n DESC"
        return [{"userId": r["user_id"], "email": r["user_email"], "changes": int(r["n"])}
                for r in self._conn.execute(q, args).fetchall()]

    def needs_attention(self, campaign_id: str, *, stuck_days: float = 7.0,
                        idle_days: float = 14.0, now: Optional[float] = None,
                        platform: Optional[str] = None) -> dict[str, Any]:
        """Operational counters (current-state, not windowed):
          - stuckInProgress: 'in_progress' leads whose last activity predates stuck_days.
          - couldntConnectTotal: leads currently 'couldnt_connect'.
          - noActivity: still-open leads (not closed/couldnt_connect/archived) with no
            status change AND no note for idle_days."""
        now = now if now is not None else time.time()
        stuck_floor = now - stuck_days * 86400
        idle_floor = now - idle_days * 86400
        pf = " AND m.platform=?" if platform else ""
        pargs = [platform] if platform else []

        stuck = self._conn.execute(
            f"""SELECT COUNT(*) AS n FROM matches m
                WHERE m.campaign_id=? AND m.status='in_progress'{pf}
                  AND COALESCE(
                        (SELECT MAX(created_at) FROM lead_status_changes lsc
                          WHERE lsc.campaign_id=m.campaign_id
                            AND lsc.platform=m.platform AND lsc.comment_id=m.comment_id),
                        m.updated_at) < ?""",
            (campaign_id, *pargs, stuck_floor)).fetchone()["n"]

        couldnt = self._conn.execute(
            f"SELECT COUNT(*) AS n FROM matches m "
            f"WHERE m.campaign_id=? AND m.status='couldnt_connect'{pf}",
            (campaign_id, *pargs)).fetchone()["n"]

        no_activity = self._conn.execute(
            f"""SELECT COUNT(*) AS n FROM matches m
                WHERE m.campaign_id=?{pf}
                  AND m.status NOT IN ('closed','couldnt_connect','archived')
                  AND COALESCE(
                        (SELECT MAX(t) FROM (
                           SELECT MAX(created_at) AS t FROM lead_status_changes lsc
                             WHERE lsc.campaign_id=m.campaign_id AND lsc.platform=m.platform
                               AND lsc.comment_id=m.comment_id
                           UNION ALL
                           SELECT MAX(created_at) AS t FROM lead_notes ln
                             WHERE ln.campaign_id=m.campaign_id AND ln.platform=m.platform
                               AND ln.comment_id=m.comment_id)),
                        m.captured_at) < ?""",
            (campaign_id, *pargs, idle_floor)).fetchone()["n"]

        return {"stuckInProgress": int(stuck), "couldntConnectTotal": int(couldnt),
                "noActivity": int(no_activity),
                "stuckDays": stuck_days, "idleDays": idle_days}

    def scored_count(self, campaign_id: str, since_ts: Optional[float] = None,
                     until_ts: Optional[float] = None) -> int:
        """Total comments scored (conversion denominator) from session counters."""
        args: list[Any] = [campaign_id]
        q = "SELECT COALESCE(SUM(comments_scored),0) AS n FROM sessions WHERE campaign_id=?"
        q += _time_filter("started_at", since_ts, until_ts, args)
        return int(self._conn.execute(q, args).fetchone()["n"])

    def spend_by_day(self, campaign_id: str, since_ts: Optional[float] = None,
                     until_ts: Optional[float] = None) -> list[dict[str, Any]]:
        args: list[Any] = [campaign_id]
        q = (f"SELECT strftime('%Y-%m-%d', datetime(created_at,'unixepoch','{TZ_SQL_SHIFT}')) "
             "AS day, COALESCE(SUM(usd),0) AS usd FROM spend_log WHERE campaign_id=?")
        q += _time_filter("created_at", since_ts, until_ts, args)
        q += " GROUP BY day ORDER BY day"
        return [{"day": r["day"], "usd": float(r["usd"])}
                for r in self._conn.execute(q, args).fetchall()]

    def spend_by_stage(self, campaign_id: str, since_ts: Optional[float] = None,
                       until_ts: Optional[float] = None) -> dict[str, float]:
        args: list[Any] = [campaign_id]
        q = "SELECT stage, COALESCE(SUM(usd),0) AS usd FROM spend_log WHERE campaign_id=?"
        q += _time_filter("created_at", since_ts, until_ts, args)
        q += " GROUP BY stage"
        return {r["stage"]: float(r["usd"]) for r in self._conn.execute(q, args).fetchall()}

    def scored_by_day(self, campaign_id: str, since_ts: Optional[float] = None,
                      until_ts: Optional[float] = None) -> list[dict[str, Any]]:
        """Per-day sum of comments_scored (bucketed by session start)."""
        args: list[Any] = [campaign_id]
        q = (f"SELECT strftime('%Y-%m-%d', datetime(started_at,'unixepoch','{TZ_SQL_SHIFT}')) "
             "AS day, COALESCE(SUM(comments_scored),0) AS n FROM sessions WHERE campaign_id=?")
        q += _time_filter("started_at", since_ts, until_ts, args)
        q += " GROUP BY day ORDER BY day"
        return [{"day": r["day"], "n": int(r["n"])}
                for r in self._conn.execute(q, args).fetchall()]

    def funnel_totals(self, campaign_id: str, since_ts: Optional[float] = None,
                      until_ts: Optional[float] = None) -> dict[str, int]:
        """reels → relevant → scored → matches, summed from session counters."""
        args: list[Any] = [campaign_id]
        q = ("SELECT COALESCE(SUM(reels_seen),0) AS reels, "
             "COALESCE(SUM(relevance_passes),0) AS relevant, "
             "COALESCE(SUM(comments_scored),0) AS scored, "
             "COALESCE(SUM(matches),0) AS matches FROM sessions WHERE campaign_id=?")
        q += _time_filter("started_at", since_ts, until_ts, args)
        r = self._conn.execute(q, args).fetchone()
        return {"reels": int(r["reels"]), "relevant": int(r["relevant"]),
                "scored": int(r["scored"]), "matches": int(r["matches"])}

    def per_campaign_rollup(self, org_id: Optional[int] = None) -> list[dict[str, Any]]:
        """Per-campaign lead/spend rollup, scoped to `org_id` when given (panel use).

        SPEND IS READ FROM `spend_log`, never inferred from the existence of leads.
        Building the row set from `matches` alone silently DROPPED every campaign
        that had burned budget without producing a match, and the panel then
        defaulted the missing row to `spent: 0` — while `/api/reports`, which sums
        `spend_by_stage` over the org's campaign ids, reported the same money. Two
        payloads, one DB, contradicting each other; budget caps looked untouched.

        Org scoping: `matches` carries `org_id` (stamped from the campaign), but
        `spend_log` does NOT, so a lead-less campaign is attributed to its org
        through `campaign_meta`. A campaign whose matches are org-stamped is kept
        even if it has no `campaign_meta` row, preserving the previous behaviour.

        Rows are ordered by campaign id. Callers key them by `campaignId`, so the
        order is for determinism, not meaning.
        """
        win_in = ",".join(f"'{s}'" for s in sorted(WIN_STATUS))
        q = (f"SELECT m.campaign_id AS campaign_id, COUNT(*) AS leads, "
             f"SUM(CASE WHEN m.status IN ({win_in}) THEN 1 ELSE 0 END) AS won "
             f"FROM matches m")
        args: list[Any] = []
        if org_id is not None:
            q += " WHERE m.org_id=?"
            args.append(org_id)
        q += " GROUP BY m.campaign_id"
        leads = {r["campaign_id"]: r for r in self._conn.execute(q, args).fetchall()}
        spend = {r["campaign_id"]: float(r["t"]) for r in self._conn.execute(
            "SELECT campaign_id, COALESCE(SUM(usd),0) AS t FROM spend_log "
            "GROUP BY campaign_id").fetchall()}
        cids = set(leads) | set(spend)
        if org_id is not None:
            owned = {r["campaign_id"] for r in self._conn.execute(
                "SELECT campaign_id FROM campaign_meta WHERE org_id=?", (org_id,)).fetchall()}
            cids = {cid for cid in cids if cid in leads or cid in owned}
        out = []
        for cid in sorted(cids):
            r = leads.get(cid)
            out.append({"campaignId": cid,
                        "leads": int(r["leads"]) if r else 0,
                        "won": int(r["won"]) if r else 0,
                        "spend": spend.get(cid, 0.0)})
        return out

    # ----- v3 campaign_meta (panel-editable overlay) -----
    def get_campaign_meta(self, campaign_id: str) -> Optional[dict[str, Any]]:
        row = self._conn.execute(
            "SELECT * FROM campaign_meta WHERE campaign_id=?", (campaign_id,)).fetchone()
        return dict(row) if row else None

    def list_campaign_meta(self, org_id: Optional[int] = None) -> list[dict[str, Any]]:
        """All campaign_meta rows, scoped to `org_id` when given (panel use)."""
        if org_id is not None:
            return [dict(r) for r in self._conn.execute(
                "SELECT * FROM campaign_meta WHERE org_id=? ORDER BY campaign_id",
                (org_id,)).fetchall()]
        return [dict(r) for r in self._conn.execute(
            "SELECT * FROM campaign_meta ORDER BY campaign_id").fetchall()]

    def upsert_campaign_meta(self, campaign_id: str, *, org_id: Optional[int] = None,
                             display_name: Optional[str] = None,
                             status: Optional[str] = None, budget_cap: Optional[float] = None,
                             goal_target: Optional[int] = None) -> dict[str, Any]:
        """Sparse upsert — only non-None fields change (COALESCE-merge). `org_id` is
        set on first insert and never reassigned (a campaign cannot change owner)."""
        if status is not None and status not in VALID_CAMPAIGN_STATUS:
            raise ValueError(f"invalid campaign status: {status!r}")
        now = time.time()
        with self._tx() as c:
            c.execute(
                """INSERT INTO campaign_meta(campaign_id, org_id, display_name, status,
                                             budget_cap, goal_target, created_at, updated_at)
                   VALUES(?, ?, ?, COALESCE(?, 'live'), ?, ?, ?, ?)
                   ON CONFLICT(campaign_id) DO UPDATE SET
                     org_id=COALESCE(campaign_meta.org_id, excluded.org_id),
                     display_name=COALESCE(excluded.display_name, campaign_meta.display_name),
                     status=COALESCE(?, campaign_meta.status),
                     budget_cap=COALESCE(excluded.budget_cap, campaign_meta.budget_cap),
                     goal_target=COALESCE(excluded.goal_target, campaign_meta.goal_target),
                     updated_at=excluded.updated_at""",
                (campaign_id, org_id, display_name, status, budget_cap, goal_target,
                 now, now, status),
            )
        if org_id is not None:
            self._org_cache[campaign_id] = org_id
        meta = self.get_campaign_meta(campaign_id)
        assert meta is not None  # just upserted
        return meta

    # ----- v12 campaign lifecycle controls -----
    # These use dedicated UPDATE statements, NOT upsert_campaign_meta: that upsert is
    # COALESCE(?, existing) for every field, where None means "keep" — so it can never
    # set a column back to NULL (un-archive, clear paused_reason). Set-and-clear needs
    # explicit UPDATEs.
    def set_campaign_archived(self, campaign_id: str, archived: bool, *,
                              new_status: Optional[str] = None) -> Optional[dict[str, Any]]:
        """Archive (archived=True → stamp archived_at) or un-archive (False → NULL it)
        a campaign via a dedicated UPDATE. Optionally transition `status` in the SAME
        tx — archive-while-live passes new_status='paused' so the (archived, live)
        contradiction is never reachable. Returns the updated meta, or None when no
        such campaign_meta row exists (caller maps to 404)."""
        if new_status is not None and new_status not in VALID_CAMPAIGN_STATUS:
            raise ValueError(f"invalid campaign status: {new_status!r}")
        now = time.time()
        archived_at = now if archived else None
        with self._tx() as c:
            if new_status is not None:
                cur = c.execute(
                    "UPDATE campaign_meta SET archived_at=?, status=?, updated_at=? "
                    "WHERE campaign_id=?", (archived_at, new_status, now, campaign_id))
            else:
                cur = c.execute(
                    "UPDATE campaign_meta SET archived_at=?, updated_at=? "
                    "WHERE campaign_id=?", (archived_at, now, campaign_id))
            if cur.rowcount == 0:
                return None
        return self.get_campaign_meta(campaign_id)

    def set_campaign_paused(self, campaign_id: str, *, paused: bool,
                            reason: str = "user") -> Optional[dict[str, Any]]:
        """Pause (paused=True → status='paused', paused_reason=reason) or resume
        (paused=False → status='live', clear paused_reason). Precedence: a resume
        request carries the actor's `reason`; it clears the pause only when that actor
        outranks (or equals) the stored reason — so a 'user' resume cannot clear an
        'auto' (system) halt. A resume that is outranked is a no-op. Returns updated
        meta, or None if no campaign_meta row exists."""
        if reason not in _PAUSED_REASON_RANK:
            raise ValueError(f"invalid paused_reason: {reason!r}")
        now = time.time()
        with self._tx() as c:
            row = c.execute(
                "SELECT status, paused_reason FROM campaign_meta WHERE campaign_id=?",
                (campaign_id,)).fetchone()
            if row is None:
                return None
            if paused:
                c.execute(
                    "UPDATE campaign_meta SET status='paused', paused_reason=?, updated_at=? "
                    "WHERE campaign_id=?", (reason, now, campaign_id))
            else:
                stored = row["paused_reason"]
                stored_rank = _PAUSED_REASON_RANK.get(stored, -1) if stored else -1
                if stored_rank > _PAUSED_REASON_RANK[reason]:
                    return self.get_campaign_meta(campaign_id)  # outranked — no-op
                c.execute(
                    "UPDATE campaign_meta SET status='live', paused_reason=NULL, updated_at=? "
                    "WHERE campaign_id=?", (now, campaign_id))
        return self.get_campaign_meta(campaign_id)

    def set_campaign_schedule(self, campaign_id: str, *, kind: str, hour: int,
                              minute: int, next_run_at: float,
                              dow: Optional[int] = None,
                              tz: str = "Asia/Tashkent",
                              target_leads: Optional[int] = None,
                              duration_minutes: Optional[int] = None
                              ) -> Optional[dict[str, Any]]:
        """Arm (or replace) a campaign's recurring schedule via a dedicated UPDATE.
        Sets schedule_enabled=1, the cadence columns, and the precomputed next_run_at.
        Returns the updated meta, or None when no campaign_meta row exists."""
        now = time.time()
        with self._tx() as c:
            cur = c.execute(
                "UPDATE campaign_meta SET schedule_enabled=1, schedule_kind=?, "
                "schedule_dow=?, schedule_hour=?, schedule_minute=?, schedule_tz=?, "
                "next_run_at=?, schedule_target_leads=?, schedule_duration_minutes=?, "
                "updated_at=? WHERE campaign_id=?",
                (kind, dow, hour, minute, tz, next_run_at, target_leads,
                 duration_minutes, now, campaign_id))
            if cur.rowcount == 0:
                return None
        return self.get_campaign_meta(campaign_id)

    def clear_campaign_schedule(self, campaign_id: str) -> Optional[dict[str, Any]]:
        """Disarm a campaign's schedule via a dedicated UPDATE — schedule_enabled=0 and
        next_run_at=NULL (the COALESCE upsert could never null next_run_at). The
        cadence columns are left as-is (harmless; schedule_enabled gates them). Returns
        updated meta, or None if no row."""
        now = time.time()
        with self._tx() as c:
            cur = c.execute(
                "UPDATE campaign_meta SET schedule_enabled=0, next_run_at=NULL, "
                "updated_at=? WHERE campaign_id=?", (now, campaign_id))
            if cur.rowcount == 0:
                return None
        return self.get_campaign_meta(campaign_id)

    def due_scheduled_campaigns(self, now_ts: float,
                                org_id: Optional[int] = None) -> list[dict[str, Any]]:
        """Campaigns whose recurring schedule is due to fire at ``now_ts``: armed,
        next_run_at in the past, and RUNNABLE (live AND not archived — the shared
        predicate, so the scheduler can never launch a paused/archived campaign)."""
        q = ("SELECT * FROM campaign_meta WHERE schedule_enabled=1 "
             "AND next_run_at IS NOT NULL AND next_run_at<=? "
             f"AND {RUNNABLE_SQL_PREDICATE}")
        args: list[Any] = [now_ts]
        if org_id is not None:
            q += " AND org_id=?"
            args.append(org_id)
        q += " ORDER BY next_run_at"
        return [dict(r) for r in self._conn.execute(q, args).fetchall()]

    def advance_scheduled_run(self, campaign_id: str, *, next_run_at: float,
                              fired_at: float) -> None:
        """Stamp last_scheduled_run_at and move next_run_at forward to the next fire.
        Used by the scheduler in the SAME transaction as its idempotency re-check, so a
        double-tick (or a tick + restart mid-launch) cannot launch the same window
        twice. A dedicated UPDATE (not the COALESCE upsert)."""
        now = time.time()
        with self._tx() as c:
            c.execute(
                "UPDATE campaign_meta SET next_run_at=?, last_scheduled_run_at=?, "
                "updated_at=? WHERE campaign_id=?",
                (next_run_at, fired_at, now, campaign_id))

    # ----- v4 campaign briefs (panel-authored runnable logic) -----
    def get_campaign_brief(self, campaign_id: str) -> Optional[dict[str, Any]]:
        row = self._conn.execute(
            "SELECT brief FROM campaign_briefs WHERE campaign_id=?", (campaign_id,)).fetchone()
        if not row:
            return None
        try:
            return json.loads(row["brief"])
        except json.JSONDecodeError:
            return None

    def upsert_campaign_brief(self, campaign_id: str, brief: dict[str, Any],
                              org_id: Optional[int] = None) -> None:
        now = time.time()
        blob = json.dumps(brief, ensure_ascii=False)
        with self._tx() as c:
            c.execute(
                """INSERT INTO campaign_briefs(campaign_id, org_id, brief, updated_at)
                   VALUES(?,?,?,?)
                   ON CONFLICT(campaign_id) DO UPDATE SET
                     org_id=COALESCE(campaign_briefs.org_id, excluded.org_id),
                     brief=excluded.brief, updated_at=excluded.updated_at""",
                (campaign_id, org_id, blob, now),
            )
        if org_id is not None:
            self._org_cache[campaign_id] = org_id

    # ----- v3 team members -----
    def list_team(self) -> list[dict[str, Any]]:
        return [dict(r) for r in self._conn.execute(
            "SELECT * FROM team_members ORDER BY created_at").fetchall()]

    def add_team_member(self, *, name: str, email: str, role: str = "member",
                        initials: Optional[str] = None) -> int:
        now = time.time()
        if not initials:
            initials = "".join(p[0] for p in name.split()[:2]).upper() or name[:2].upper()
        with self._tx() as c:
            cur = c.execute(
                """INSERT INTO team_members(name, email, role, initials, created_at, updated_at)
                   VALUES(?,?,?,?,?,?)""",
                (name, email, role, initials, now, now),
            )
            return int(cur.lastrowid)

    def update_team_member(self, member_id: int, *, name: Optional[str] = None,
                           email: Optional[str] = None, role: Optional[str] = None,
                           initials: Optional[str] = None) -> bool:
        with self._tx() as c:
            cur = c.execute(
                """UPDATE team_members SET
                     name=COALESCE(?, name), email=COALESCE(?, email),
                     role=COALESCE(?, role), initials=COALESCE(?, initials),
                     updated_at=? WHERE id=?""",
                (name, email, role, initials, time.time(), member_id),
            )
            return cur.rowcount > 0

    def delete_team_member(self, member_id: int) -> bool:
        with self._tx() as c:
            cur = c.execute("DELETE FROM team_members WHERE id=?", (member_id,))
            return cur.rowcount > 0

    # ----- v3 settings overlay (per-org since v7) -----
    def get_settings(self, org_id: int) -> dict[str, Any]:
        out: dict[str, Any] = {}
        for r in self._conn.execute(
                "SELECT key, value FROM settings WHERE org_id=?", (org_id,)).fetchall():
            try:
                out[r["key"]] = json.loads(r["value"]) if r["value"] is not None else None
            except json.JSONDecodeError:
                out[r["key"]] = r["value"]
        return out

    def set_setting(self, org_id: int, key: str, value: Any) -> None:
        with self._tx() as c:
            c.execute(
                """INSERT INTO settings(org_id, key, value, updated_at) VALUES(?,?,?,?)
                   ON CONFLICT(org_id, key) DO UPDATE SET value=excluded.value,
                                                          updated_at=excluded.updated_at""",
                (org_id, key, json.dumps(value, ensure_ascii=False), time.time()),
            )

    # ----- v3 integrations overlay (per-org since v7) -----
    def list_integrations(self, org_id: int) -> list[dict[str, Any]]:
        return [dict(r) for r in self._conn.execute(
            "SELECT * FROM integrations WHERE org_id=? ORDER BY platform",
            (org_id,)).fetchall()]

    def set_integration(self, org_id: int, platform: str, *,
                        connected: Optional[bool] = None,
                        detail: Optional[str] = None) -> dict[str, Any]:
        # `detail` is PLAINTEXT, panel-readable state — NEVER a credential. Guard
        # against a caller mistakenly routing a secret here (security review HIGH
        # #2); real secrets go through set_integration_secret (encrypted).
        if detail and any(k in detail.lower() for k in _SECRET_DETAIL_MARKERS):
            raise ValueError(
                "integration detail must not contain a credential — use "
                "set_integration_secret for api_key/api_hash/session/token")
        now = time.time()
        conn_int = None if connected is None else int(connected)
        with self._tx() as c:
            c.execute(
                """INSERT INTO integrations(org_id, platform, connected, detail, updated_at)
                   VALUES(?, ?, COALESCE(?, 0), ?, ?)
                   ON CONFLICT(org_id, platform) DO UPDATE SET
                     connected=COALESCE(?, integrations.connected),
                     detail=COALESCE(excluded.detail, integrations.detail),
                     updated_at=excluded.updated_at""",
                (org_id, platform, conn_int, detail, now, conn_int),
            )
        row = self._conn.execute(
            "SELECT * FROM integrations WHERE org_id=? AND platform=?",
            (org_id, platform)).fetchone()
        return dict(row)

    # ----- v8 encrypted integration secrets -----
    def _cipher(self) -> "SecretCipher":
        """The Fernet cipher, built once from AIZU_SECRET_KEY unless injected.
        Raises SecretCipherError (caught at the boundary) when no key is available."""
        if self._secret_cipher is None:
            from ..secrets import SecretCipher
            self._secret_cipher = SecretCipher.from_env()
        return self._secret_cipher

    def set_integration_secret(self, org_id: int, platform: str,
                               secret: dict[str, Any]) -> None:
        """Encrypt and upsert the credential for (org, platform). Overwrites any
        prior secret (rotation). Raises SecretCipherError if no key is configured."""
        blob = self._cipher().encrypt(secret)
        now = time.time()
        with self._tx() as c:
            c.execute(
                """INSERT INTO integration_secrets(org_id, platform, secret_blob, updated_at)
                   VALUES(?,?,?,?)
                   ON CONFLICT(org_id, platform) DO UPDATE SET
                     secret_blob=excluded.secret_blob, updated_at=excluded.updated_at""",
                (org_id, platform, blob, now),
            )

    def get_integration_secret(self, org_id: int, platform: str
                               ) -> Optional[dict[str, Any]]:
        """Decrypt the stored credential, or None if none is stored. Raises
        SecretCipherError on a key mismatch / tampered blob (a loud boundary)."""
        row = self._conn.execute(
            "SELECT secret_blob FROM integration_secrets WHERE org_id=? AND platform=?",
            (org_id, platform)).fetchone()
        if row is None:
            return None
        return self._cipher().decrypt(row["secret_blob"])

    def delete_integration_secret(self, org_id: int, platform: str) -> None:
        """Remove the stored credential (disconnect / revoke). Idempotent."""
        with self._tx() as c:
            c.execute(
                "DELETE FROM integration_secrets WHERE org_id=? AND platform=?",
                (org_id, platform))

    # ----- v13 billing subscriptions -----

    def get_subscription(self, org_id: int) -> dict[str, Any]:
        """The single choke point for both enforcement and the panel. Returns the
        implicit FREE default when no row exists — NEVER None (so `None >= cap`
        can't blow up the gate). `lead_cap` is already the EFFECTIVE cap:
        `lead_cap_override` when set (e.g. a per-deal Scale number), else the
        catalogue cap for the tier."""
        from ..billing import tier_lead_cap  # local import: avoid an import cycle
        row = self._conn.execute(
            """SELECT org_id, provider, tier, interval, lead_cap_override, status,
                      provider_subscription_id, provider_customer_id,
                      current_period_start, current_period_end, cancel_at_period_end,
                      last_event_ts, updated_at
                 FROM subscriptions WHERE org_id=?""", (org_id,)).fetchone()
        if row is None:
            return {
                "org_id": org_id, "provider": None, "tier": "free",
                "interval": None, "status": "active", "lead_cap": tier_lead_cap("free"),
                "provider_subscription_id": None, "provider_customer_id": None,
                "current_period_start": None, "current_period_end": None,
                "cancel_at_period_end": False, "last_event_ts": 0.0,
            }
        tier = row["tier"]
        override = row["lead_cap_override"]
        lead_cap = int(override) if override is not None else tier_lead_cap(tier)
        return {
            "org_id": row["org_id"], "provider": row["provider"], "tier": tier,
            "interval": row["interval"], "status": row["status"], "lead_cap": lead_cap,
            "provider_subscription_id": row["provider_subscription_id"],
            "provider_customer_id": row["provider_customer_id"],
            "current_period_start": row["current_period_start"],
            "current_period_end": row["current_period_end"],
            "cancel_at_period_end": bool(row["cancel_at_period_end"]),
            "last_event_ts": row["last_event_ts"],
        }

    def upsert_subscription(self, org_id: int, *, last_event_ts: float,
                            **fields: Any) -> bool:
        """Insert/update an org's subscription from a verified billing event.

        Conditional on MONOTONIC ordering: the update only applies when the
        incoming `last_event_ts` (provider `modified_at`) is strictly newer than
        the stored one, so a delayed `updated(active)` can never re-activate a
        `revoked` org, and an exact webhook redelivery (equal ts) is a no-op.
        Returns True when a row was written/changed, False when the event was
        dropped as stale. Only known columns in `fields` are persisted."""
        allowed = {
            "provider", "tier", "interval", "lead_cap_override", "status",
            "provider_subscription_id", "provider_customer_id",
            "current_period_start", "current_period_end", "cancel_at_period_end",
        }
        unknown = set(fields) - allowed
        if unknown:
            raise ValueError(f"upsert_subscription got unknown fields: {sorted(unknown)}")
        now = time.time()
        cols = ["org_id", "last_event_ts", "updated_at", *fields.keys()]
        vals = [org_id, last_event_ts, now, *fields.values()]
        placeholders = ",".join("?" for _ in cols)
        # ON CONFLICT updates every supplied column + bookkeeping, but ONLY when
        # the new event is newer (the WHERE guard drops stale/out-of-order events).
        set_clause = ", ".join(
            f"{col}=excluded.{col}"
            for col in ("last_event_ts", "updated_at", *fields.keys()))
        with self._tx() as c:
            cur = c.execute(
                f"""INSERT INTO subscriptions ({",".join(cols)})
                    VALUES ({placeholders})
                    ON CONFLICT(org_id) DO UPDATE SET {set_clause}
                    WHERE excluded.last_event_ts > subscriptions.last_event_ts""",
                vals)
            return cur.rowcount > 0

    def period_since(self, org_id: int, *, now: Optional[float] = None) -> float:
        """Start of the org's current billing period (the cap window). For a paid
        org, the persisted `current_period_start`; for a Free org (no provider
        period), the start of the current calendar month in Asia/Tashkent (UTC+5),
        matching the dashboard day/hour bucketing (TZ_SQL_SHIFT). Both enforcement
        AND the displayed `leadsUsed` MUST call this so the meter and the gate
        never disagree."""
        sub = self.get_subscription(org_id)
        start = sub.get("current_period_start")
        if start is not None:
            return float(start)
        dt = datetime.fromtimestamp(now if now is not None else time.time(), _TASHKENT_TZ)
        return dt.replace(day=1, hour=0, minute=0, second=0, microsecond=0).timestamp()

    def count_leads_this_period(self, org_id: int, since: float) -> int:
        """Leads (rows in `matches`) the org has surfaced since the period anchor.
        The metering unit. ALL surfaced matches count — NO status predicate
        (rejected/archived leads still count: the value/work is in surfacing them,
        and it keeps the meter monotonic within a period). NO platform predicate
        either — the cap aggregates EVERY rail/engine for the org. NULL-org rows
        (orphan campaigns) are correctly excluded by `org_id=?`. `captured_at` is
        first-capture time and is never moved by a re-score upsert, so a lead's
        period membership is stable."""
        row = self._conn.execute(
            "SELECT COUNT(*) FROM matches WHERE org_id=? AND captured_at>=?",
            (org_id, since)).fetchone()
        return int(row[0]) if row else 0

    # ----- v14 workers (distributed pool: token registry + presence) -----
    # The SERVER mints the plaintext token (new_session_token, at the HTTP boundary,
    # mirroring the auth_sessions flow) and passes it in here; the store persists ONLY
    # hash_session_token(token). Plaintext is never persisted or logged by the store.

    def register_worker(
        self,
        *,
        worker_id: str,
        token: str,
        org_id: Optional[int] = None,
        display_name: Optional[str] = None,
        host: Optional[str] = None,
        os: Optional[str] = None,
        agent_version: Optional[str] = None,
        max_sessions: int = 1,
        capabilities: Optional[list] = None,
        token_expires_at: Optional[float] = None,
        enrolment_scope_kind: Optional[str] = None,
        preflight: Optional[dict] = None,
    ) -> dict[str, Any]:
        """Idempotent register/re-register by stable worker id (UPSERT on PK).

        Stores ONLY hash_session_token(token) in worker_token_hash; the plaintext
        `token` is never persisted or logged here. A re-register from the SAME box
        (same worker_id) ROTATES the token hash, refreshes metadata, sets
        last_heartbeat_at = registered_at = now, current_sessions = 0, and CLEARS
        revoked_at (a box coming back is active again). `capabilities` is JSON-encoded.

        `enrolment_scope_kind` ('org'|'pool'|None, v22 BUILD-PLAN B8 fix) is STICKY:
        pass it only when THIS call just redeemed an enrolment token (the caller
        already clamped org_id/capabilities to that scope). Passing None (the
        default — every plain re-register on an already-enrolled worker's own
        bearer token) PRESERVES whatever scope kind the row already has via
        `COALESCE(excluded.enrolment_scope_kind, workers.enrolment_scope_kind)`,
        rather than clobbering it back to NULL. This is what lets the register
        HANDLER re-clamp a re-register against the worker's ORIGINAL enrolment
        scope instead of trusting the box's freshly self-declared org_id/
        capabilities on every subsequent call — see server.py's
        _handle_worker_register docstring.

        `preflight` (v23) is the box's own launch self-check summary — JSON-encoded
        verbatim, DIAGNOSTIC ONLY, never read back into an auth or dispatch decision
        (a box that cannot work says so by registering with EMPTY capabilities; this
        field only says WHY, which is the whole point when nobody can SSH into the box
        — ledger F12). Register REPLACES it, including with NULL: a register is a full
        re-statement of the box's identity, exactly like `capabilities`, so a downgrade
        to a pre-v23 sidecar clears a stale report rather than leaving a lie on screen.
        The heartbeat, by contrast, only ever refreshes it (COALESCE).

        Returns the stored row shape (NO token, NO hash):
            {"id", "orgId", "displayName", "host", "os", "agentVersion",
             "maxSessions", "currentSessions", "capabilities", "registeredAt",
             "lastHeartbeatAt", "tokenExpiresAt", "revokedAt", "preflight"}.
        """
        now = time.time()
        caps_json = json.dumps(capabilities or [])
        preflight_json = json.dumps(preflight) if preflight is not None else None
        token_hash = hash_session_token(token)
        with self._tx() as c:
            c.execute(
                """INSERT INTO workers
                       (id, org_id, display_name, host, os, agent_version,
                        last_heartbeat_at, registered_at, max_sessions,
                        current_sessions, capabilities, worker_token_hash,
                        token_expires_at, revoked_at, enrolment_scope_kind,
                        preflight_json)
                   VALUES (?,?,?,?,?,?, ?,?,?, 0, ?, ?, ?, NULL, ?, ?)
                   ON CONFLICT(id) DO UPDATE SET
                       org_id            = excluded.org_id,
                       display_name      = excluded.display_name,
                       host              = excluded.host,
                       os                = excluded.os,
                       agent_version     = excluded.agent_version,
                       last_heartbeat_at = excluded.last_heartbeat_at,
                       max_sessions      = excluded.max_sessions,
                       current_sessions  = 0,
                       capabilities      = excluded.capabilities,
                       worker_token_hash = excluded.worker_token_hash,
                       token_expires_at  = excluded.token_expires_at,
                       revoked_at        = NULL,
                       enrolment_scope_kind = COALESCE(excluded.enrolment_scope_kind,
                                                        workers.enrolment_scope_kind),
                       preflight_json    = excluded.preflight_json""",
                (worker_id, org_id, display_name, host, os, agent_version,
                 now, now, int(max_sessions), caps_json, token_hash,
                 token_expires_at, enrolment_scope_kind, preflight_json),
            )
            # Read back canonical timestamps: on a re-register the ON CONFLICT clause
            # PRESERVES the original registered_at, so the returned shape must mirror
            # the stored row rather than the fresh `now`.
            stored = c.execute(
                "SELECT registered_at, last_heartbeat_at FROM workers WHERE id=?",
                (worker_id,)).fetchone()
        return {
            "id": worker_id,
            "orgId": org_id,
            "displayName": display_name,
            "host": host,
            "os": os,
            "agentVersion": agent_version,
            "maxSessions": int(max_sessions),
            "currentSessions": 0,
            "capabilities": capabilities or [],
            "registeredAt": stored["registered_at"],
            "lastHeartbeatAt": stored["last_heartbeat_at"],
            "tokenExpiresAt": token_expires_at,
            "revokedAt": None,
            "preflight": preflight,
        }

    def record_worker_heartbeat(
        self,
        *,
        worker_id: str,
        current_sessions: Optional[int] = None,
        preflight: Optional[dict] = None,
    ) -> bool:
        """Presence heartbeat: stamp last_heartbeat_at = now() on the worker's OWN row
        only (WHERE id=? AND revoked_at IS NULL). Optionally update current_sessions
        when the body carries a load number, and preflight_json (v23) when the body
        carries a self-check summary. Returns True iff exactly one live row was
        updated (rowcount == 1); False when the worker is unknown or revoked — the
        caller maps False to 404/401. Touches NO other worker's row."""
        # ONE fully-hardcoded statement rather than an f-string-assembled SET clause:
        # there is NO dynamic SQL surface here, so a future field added to a `sets`
        # list can never become an injection point (security review M5). All values
        # remain parameterised. v23 collapsed the previous two-branch form into this
        # single COALESCE statement instead of adding a third and fourth branch — an
        # omitted field (NULL param) keeps whatever is stored, which is exactly what
        # "the body didn't carry it" means for both current_sessions and preflight.
        now = time.time()
        preflight_json = json.dumps(preflight) if preflight is not None else None
        with self._tx() as c:
            cur = c.execute(
                "UPDATE workers "
                "   SET last_heartbeat_at = ?, "
                "       current_sessions  = COALESCE(?, current_sessions), "
                "       preflight_json    = COALESCE(?, preflight_json) "
                " WHERE id = ? AND revoked_at IS NULL",
                (now,
                 None if current_sessions is None else int(current_sessions),
                 preflight_json,
                 worker_id),
            )
            return cur.rowcount == 1

    def get_worker_by_token(self, token: str) -> Optional[dict[str, Any]]:
        """Worker-plane authentication. Hash the incoming raw bearer token, match
        worker_token_hash WHERE revoked_at IS NULL AND (token_expires_at IS NULL OR
        token_expires_at > now()). Fail closed: returns None on no-match, revoked, or
        expired (LOCKED #5). Read-only.

        Returns the authenticated worker identity (NO hash):
            {"id", "orgId", "capabilities", "maxSessions", "currentSessions",
             "agentVersion", "enrolmentScopeKind"}
        or None.

        v23 deliberately does NOT add `preflight` here: this shape is the AUTH
        identity, not a console view, and nothing on the auth/lease path may branch on
        a self-reported diagnostic (a box withholds its capabilities to stop being
        leased to — that is the only mechanism). Stated so nobody "helpfully" adds it."""
        if not token:
            return None
        row = self._conn.execute(
            """SELECT id, org_id, capabilities, max_sessions, current_sessions,
                      agent_version, enrolment_scope_kind
                 FROM workers
                WHERE worker_token_hash = ?
                  AND revoked_at IS NULL
                  AND (token_expires_at IS NULL OR token_expires_at > ?)""",
            (hash_session_token(token), time.time()),
        ).fetchone()
        if row is None:
            return None
        return {
            "id": row["id"],
            "orgId": row["org_id"],
            "capabilities": _decode_capabilities(row["capabilities"]),
            "maxSessions": int(row["max_sessions"]),
            "currentSessions": int(row["current_sessions"]),
            "agentVersion": row["agent_version"],
            "enrolmentScopeKind": row["enrolment_scope_kind"],
        }

    def list_workers(self, *, now: Optional[float] = None) -> list[dict[str, Any]]:
        """Fleet snapshot — ALL workers (pool-wide; not org-scoped), newest
        registration first. status is DERIVED here, never stored (LOCKED #3, #6).
        `now` is injectable so tests can freeze the clock. Read-only.

        Each element:
            {"id", "orgId", "displayName", "host", "os", "agentVersion",
             "maxSessions", "currentSessions", "capabilities",
             "registeredAt", "lastHeartbeatAt", "lastSeenAgeSec",
             "status", "revokedAt", "currentJob", "preflight"}.
        `preflight` (v23) is the box's last reported launch self-check
        `{ok, blocking, enforced, ranAt, failed[]}` or None for a box that has never
        sent one (a pre-v23 sidecar). It is what turns "online with 0 capabilities"
        into "online, token_persistence FAIL, here is the remedy" for an admin who
        cannot SSH into that PC (F12). `readiness.fleet_readiness` also consumes it.
        `currentJob` is the leased/running job the box is executing right now (or None):
        `{"jobId","campaignId","platform","status","runId","leaseExpiresAt"}` — so the
        console shows WHAT each worker is doing, not just that it is online. A revoked
        worker is still listed with its real derived status plus revokedAt set (the
        console shows revoked boxes; it does not hide them)."""
        now_v = now if now is not None else time.time()
        current_by_worker = self._current_jobs_by_worker()
        rows = self._conn.execute(
            """SELECT id, org_id, display_name, host, os, agent_version,
                      last_heartbeat_at, registered_at, max_sessions,
                      current_sessions, capabilities, token_expires_at, revoked_at,
                      preflight_json
                 FROM workers
                ORDER BY registered_at DESC, id ASC""",
        ).fetchall()
        out: list[dict[str, Any]] = []
        for row in rows:
            last_hb = row["last_heartbeat_at"]
            age = None if last_hb is None else (now_v - float(last_hb))
            out.append({
                "id": row["id"],
                "orgId": row["org_id"],
                "displayName": row["display_name"],
                "host": row["host"],
                "os": row["os"],
                "agentVersion": row["agent_version"],
                "maxSessions": int(row["max_sessions"]),
                "currentSessions": int(row["current_sessions"]),
                "capabilities": _decode_capabilities(row["capabilities"]),
                "registeredAt": row["registered_at"],
                "lastHeartbeatAt": last_hb,
                "lastSeenAgeSec": age,
                "status": derive_worker_status(last_hb, now_v),
                "revokedAt": row["revoked_at"],
                "currentJob": current_by_worker.get(row["id"]),
                "preflight": _decode_preflight(row["preflight_json"]),
            })
        return out

    def _current_jobs_by_worker(self) -> dict[str, dict[str, Any]]:
        """Map worker id → the job it is executing right now (leased/running), for the
        fleet snapshot. One query, no N+1. At most one live job per box under the
        one-account↔one-box invariant; if two ever coexist, the later-expiring lease
        wins (defensive)."""
        rows = self._conn.execute(
            """SELECT id, leased_by, campaign_id, platform, spec, status,
                      lease_expires_at
                 FROM jobs
                WHERE leased_by IS NOT NULL AND status IN ('leased','running')""",
        ).fetchall()
        by_worker: dict[str, dict[str, Any]] = {}
        for row in rows:
            wid = row["leased_by"]
            le = row["lease_expires_at"] or 0.0
            prior = by_worker.get(wid)
            if prior is not None and (prior["leaseExpiresAt"] or 0.0) >= le:
                continue
            spec = _decode_job_spec(row["spec"]) or {}
            by_worker[wid] = {
                "jobId": row["id"],
                "campaignId": row["campaign_id"],
                "platform": row["platform"],
                "status": row["status"],
                "runId": spec.get("run_id"),
                "leaseExpiresAt": row["lease_expires_at"],
            }
        return by_worker

    def revoke_worker(self, worker_id: str, *, now: Optional[float] = None) -> bool:
        """Revoke a worker's token (sets revoked_at = now() WHERE id=? AND revoked_at
        IS NULL). After this, get_worker_by_token fails closed for that token at the
        NEXT request (LOCKED #5 — request-time revocation, not next-heartbeat). Returns
        True iff a live row was revoked; False if unknown or already revoked."""
        now_v = now if now is not None else time.time()
        with self._tx() as c:
            cur = c.execute(
                "UPDATE workers SET revoked_at = ? "
                "WHERE id = ? AND revoked_at IS NULL",
                (now_v, worker_id),
            )
            return cur.rowcount == 1

    def is_worker_revoked(self, worker_id: str) -> bool:
        """True iff a worker row EXISTS for ``worker_id`` and carries a revoked_at.

        Read by the register handler's legacy-bootstrap branch (ledger B10): a box whose
        token was retired must not be able to walk back in on the shared bootstrap secret,
        because `register_worker` UPSERTs `revoked_at = NULL` and would silently undo the
        revocation an operator just performed. An UNKNOWN worker returns False — a fresh
        box, or one whose row a DB reset removed (C3), still enrols normally. Read-only."""
        row = self._conn.execute(
            "SELECT revoked_at FROM workers WHERE id = ?", (worker_id,)).fetchone()
        return row is not None and row["revoked_at"] is not None

    # ----- v22 worker enrolment tokens (BUILD-PLAN B8 fix) -----
    # Per-worker, single-use, admin-minted tokens that carry a SERVER-ASSIGNED scope
    # ('org'+org_id or explicit 'pool'), so a worker's org reach is decided by an admin
    # at enrolment rather than self-declared by the box at register (see server.py's
    # _handle_worker_register redemption branch, which CLAMPS org_id/capabilities to
    # the redeemed token's scope). Mirrors register_worker's mint-plaintext-at-the-
    # HTTP-boundary / persist-only-hash discipline.

    def create_worker_enrolment_token(
        self,
        *,
        token_id: str,
        token: str,
        scope_kind: str,
        org_id: Optional[int],
        label: Optional[str],
        created_by_admin_id: Optional[int],
        expires_at: float,
        now: Optional[float] = None,
    ) -> dict[str, Any]:
        """Mint one enrolment token row. `scope_kind`/`org_id` pairing is validated
        HERE too (not just at the HTTP boundary) — a caller passing 'org' without an
        org_id, or 'pool' with one, is a PROGRAMMER error (the HTTP layer already
        rejects this in the request body), so it raises ValueError rather than
        silently writing an ambiguous row. Stores ONLY hash_session_token(token); the
        plaintext is never persisted or logged here — the caller (HTTP handler) is
        the one and only place it is returned to the admin."""
        if scope_kind not in ("org", "pool"):
            raise ValueError(f"scope_kind must be 'org' or 'pool', got {scope_kind!r}")
        if scope_kind == "org" and org_id is None:
            raise ValueError("scope_kind='org' requires an org_id")
        if scope_kind == "pool" and org_id is not None:
            raise ValueError("scope_kind='pool' must not carry an org_id")
        now_v = now if now is not None else time.time()
        token_hash = hash_session_token(token)
        with self._tx() as c:
            c.execute(
                """INSERT INTO worker_enrolment_tokens
                       (id, token_hash, scope_kind, org_id, label, created_at,
                        created_by_admin_id, expires_at, redeemed_at,
                        redeemed_by_worker_id, revoked_at, revoked_by_admin_id)
                   VALUES (?,?,?,?,?,?, ?,?, NULL, NULL, NULL, NULL)""",
                (token_id, token_hash, scope_kind, org_id, label, now_v,
                 created_by_admin_id, expires_at),
            )
        return {
            "id": token_id,
            "scopeKind": scope_kind,
            "orgId": org_id,
            "label": label,
            "createdAt": now_v,
            "createdByAdminId": created_by_admin_id,
            "expiresAt": expires_at,
            "redeemedAt": None,
            "redeemedByWorkerId": None,
            "revokedAt": None,
            "revokedByAdminId": None,
        }

    def redeem_worker_enrolment_token(
        self, *, token: str, worker_id: str, now: Optional[float] = None,
    ) -> Optional[dict[str, Any]]:
        """Atomically claim an enrolment token for `worker_id`, or None on any
        failure (unknown/expired/revoked/already-redeemed token). Single-use, race-
        safe: BEGIN IMMEDIATE (write lock at statement one, same as lease_one_job —
        SQLite has no SELECT … FOR UPDATE SKIP LOCKED, D5) + a conditional UPDATE
        guarded on `redeemed_at IS NULL` + a rowcount==1 backstop, so of N concurrent
        redeemers of the SAME token exactly one wins. Returns the token's dict (scope
        the caller clamps onto the new worker row) — never the hash."""
        now_v = now if now is not None else time.time()
        token_hash = hash_session_token(token)
        with self._tx_immediate() as c:
            row = c.execute(
                """SELECT id, scope_kind, org_id, label, created_at,
                          created_by_admin_id, expires_at
                     FROM worker_enrolment_tokens
                    WHERE token_hash = ?
                      AND redeemed_at IS NULL
                      AND revoked_at IS NULL
                      AND expires_at > ?""",
                (token_hash, now_v),
            ).fetchone()
            if row is None:
                return None
            cur = c.execute(
                """UPDATE worker_enrolment_tokens
                      SET redeemed_at = ?, redeemed_by_worker_id = ?
                    WHERE id = ? AND redeemed_at IS NULL""",
                (now_v, worker_id, row["id"]),
            )
            if cur.rowcount != 1:
                return None
            return {
                "id": row["id"],
                "scopeKind": row["scope_kind"],
                "orgId": row["org_id"],
                "label": row["label"],
                "createdAt": row["created_at"],
                "createdByAdminId": row["created_by_admin_id"],
                "expiresAt": row["expires_at"],
                "redeemedAt": now_v,
                "redeemedByWorkerId": worker_id,
                "revokedAt": None,
                "revokedByAdminId": None,
            }

    def list_worker_enrolment_tokens(self) -> list[dict[str, Any]]:
        """All enrolment tokens (pending/redeemed/revoked), newest-minted first, for
        the admin console. Never exposes token/hash. Read-only."""
        rows = self._conn.execute(
            """SELECT id, scope_kind, org_id, label, created_at, created_by_admin_id,
                      expires_at, redeemed_at, redeemed_by_worker_id, revoked_at,
                      revoked_by_admin_id
                 FROM worker_enrolment_tokens
                ORDER BY created_at DESC, id ASC""",
        ).fetchall()
        return [_enrolment_token_row_to_dict(r) for r in rows]

    def revoke_worker_enrolment_token(
        self, token_id: str, *, by_admin_id: Optional[int], now: Optional[float] = None,
    ) -> bool:
        """Cancel a still-pending token (sets revoked_at/revoked_by_admin_id WHERE
        id=? AND redeemed_at IS NULL AND revoked_at IS NULL). Idempotent, matching
        revoke_worker's exact contract: True iff a live pending row was revoked;
        False (a no-op) for an unknown, already-redeemed, or already-revoked token —
        an already-redeemed token has nothing left to cancel (the operator revokes
        the WORKER it enrolled instead)."""
        now_v = now if now is not None else time.time()
        with self._tx() as c:
            cur = c.execute(
                """UPDATE worker_enrolment_tokens
                      SET revoked_at = ?, revoked_by_admin_id = ?
                    WHERE id = ? AND redeemed_at IS NULL AND revoked_at IS NULL""",
                (now_v, by_admin_id, token_id),
            )
            return cur.rowcount == 1

    # ----- v14 jobs (distributed pool: enqueue + SQLite-correct leasing) -----
    # Leasing is the one place a deferred read-lock would let two workers grab the same
    # row (BUILD-PLAN C2): lease_one_job + extend_lease run under _tx_immediate
    # (BEGIN IMMEDIATE), every other job write keeps the cheaper deferred _tx.

    def count_capable_workers(self, *, platform: str,
                              org_id: Optional[int] = None,
                              account_handle: Optional[str] = None) -> int:
        """How many NON-revoked workers declare a capability that could lease a job for
        this (org, platform, account)? Enqueue rejects a job no worker can ever serve so
        it never idles in the queue forever (BUILD-PLAN Phase 3 enqueue validation).
        Capability JSON is scanned in Python (PRD scale; LOCKED — no JSON1 dependency)."""
        rows = self._conn.execute(
            "SELECT capabilities FROM workers WHERE revoked_at IS NULL"
        ).fetchall()
        count = 0
        for row in rows:
            caps = _decode_capabilities(row["capabilities"])
            if _job_capability_covers(caps, org_id=org_id, platform=platform,
                                      account_handle=account_handle):
                count += 1
        return count

    def enqueue_job(
        self,
        *,
        job_id: str,
        campaign_id: str,
        platform: str,
        spec: dict,
        org_id: Optional[int] = None,
        required_account_handle: Optional[str] = None,
        max_attempts: int = DEFAULT_JOB_MAX_ATTEMPTS,
    ) -> dict[str, Any]:
        """Insert a queued job. `spec` (target_leads/duration_minutes/engine_mode/
        soul_text…) is JSON-encoded. Idempotent on `job_id` (INSERT OR IGNORE) so a
        retried enqueue never duplicates a job. Returns the stored row shape; the
        caller validates capability coverage BEFORE calling (so the 'no capable worker'
        rejection is a 400 at the boundary, not a silent never-leased row)."""
        now = time.time()
        spec_json = json.dumps(spec or {})
        with self._tx() as c:
            c.execute(
                """INSERT OR IGNORE INTO jobs
                       (id, org_id, campaign_id, platform, required_account_handle,
                        spec, status, attempts, max_attempts, created_at, updated_at)
                   VALUES (?,?,?,?,?, ?, 'queued', 0, ?, ?, ?)""",
                (job_id, org_id, campaign_id, platform, required_account_handle,
                 spec_json, int(max_attempts), now, now),
            )
        job = self.get_job(job_id)
        if job is None:  # pragma: no cover — INSERT OR IGNORE then immediate read
            raise RuntimeError(f"enqueue_job: job {job_id!r} vanished after insert")
        return job

    def enqueue_job_deduped(
        self,
        *,
        job_id: str,
        campaign_id: str,
        platform: str,
        spec: dict,
        org_id: Optional[int] = None,
        required_account_handle: Optional[str] = None,
        max_attempts: int = DEFAULT_JOB_MAX_ATTEMPTS,
    ) -> Optional[dict[str, Any]]:
        """Enqueue a queued job for `campaign_id`, but SKIP (return None) if that campaign
        already has an ACTIVE job (`queued|leased|running`, not dead-lettered). This is the
        double-Run guard: a user double-clicking Run (or a duplicate request) must not
        enqueue a second job set for a campaign already in flight. The exists-check and the
        insert run in ONE `_tx_immediate` (write lock at statement one) so two concurrent
        Runs can't both pass the check — exactly one wins, the other gets None (no TOCTOU).
        A terminal (`done|failed|interrupted`) or dead-lettered prior job never blocks a
        fresh run. Admin manual enqueue keeps the un-deduped `enqueue_job` (explicit)."""
        now = time.time()
        spec_json = json.dumps(spec or {})
        with self._tx_immediate() as c:
            active = c.execute(
                """SELECT 1 FROM jobs
                    WHERE campaign_id=? AND dead_lettered_at IS NULL
                      AND status IN ('queued','leased','running') LIMIT 1""",
                (campaign_id,)).fetchone()
            if active is not None:
                return None
            c.execute(
                """INSERT OR IGNORE INTO jobs
                       (id, org_id, campaign_id, platform, required_account_handle,
                        spec, status, attempts, max_attempts, created_at, updated_at)
                   VALUES (?,?,?,?,?, ?, 'queued', 0, ?, ?, ?)""",
                (job_id, org_id, campaign_id, platform, required_account_handle,
                 spec_json, int(max_attempts), now, now),
            )
        return self.get_job(job_id)

    def lease_one_job(
        self,
        *,
        worker_id: str,
        capabilities: list,
        now: Optional[float] = None,
        lease_ttl: Optional[float] = None,
    ) -> Optional[dict[str, Any]]:
        """Atomically lease the oldest leaseable job this worker can serve, or None.

        SQLite-correct (BUILD-PLAN C2): BEGIN IMMEDIATE takes the write lock at
        statement one, so the candidate SELECT and the claiming UPDATE are serialised
        against any other worker — exactly one wins, the rest get None. A job is
        leaseable when it is `queued` (and past any `retry_after_at`) OR its prior lease
        EXPIRED (`leased`/`running` with `lease_expires_at < now`); a dead-lettered job
        is never leaseable. Capability matching scans the JSON in Python after a
        platform pre-filter in SQL (PRD scale).

        The UPDATE is guarded on the same leaseable predicate + the row id, and the
        `rowcount == 1` check is the final backstop if two callers somehow reached the
        UPDATE — only one row transitions. Increments nothing (attempts move on nack)."""
        now_v = now if now is not None else time.time()
        ttl = lease_ttl if lease_ttl is not None else default_lease_ttl_sec()
        platforms = sorted({c[1] for c in (capabilities or [])
                            if isinstance(c, (list, tuple)) and len(c) == 3})
        if not platforms:
            return None
        placeholders = ",".join("?" for _ in platforms)
        with self._tx_immediate() as c:
            # A job pinned to another box (reclaim after an offline drop, Phase 4) is
            # NOT leaseable here — one account ↔ one box, no cross-box failover. Its own
            # worker may lease it (pin cleared on lease).
            rows = c.execute(
                f"""SELECT id, org_id, campaign_id, platform, required_account_handle,
                           spec, attempts, max_attempts
                      FROM jobs
                     WHERE dead_lettered_at IS NULL
                       AND platform IN ({placeholders})
                       AND (pinned_worker_id IS NULL OR pinned_worker_id = ?)
                       AND (
                            (status='queued' AND (retry_after_at IS NULL OR retry_after_at <= ?))
                            OR (status IN ('leased','running') AND lease_expires_at IS NOT NULL
                                AND lease_expires_at < ?)
                       )
                     ORDER BY created_at ASC, id ASC
                     LIMIT 200""",
                (*platforms, worker_id, now_v, now_v),
            ).fetchall()
            for row in rows:
                if not _job_capability_covers(
                        capabilities, org_id=row["org_id"], platform=row["platform"],
                        account_handle=row["required_account_handle"]):
                    continue
                cur = c.execute(
                    """UPDATE jobs
                          SET status='leased', leased_by=?, lease_expires_at=?,
                              retry_after_at=NULL, pinned_worker_id=NULL, updated_at=?
                        WHERE id=?
                          AND dead_lettered_at IS NULL
                          AND (pinned_worker_id IS NULL OR pinned_worker_id=?)
                          AND (status='queued'
                               OR (status IN ('leased','running')
                                   AND lease_expires_at IS NOT NULL
                                   AND lease_expires_at < ?))""",
                    (worker_id, now_v + ttl, now_v, row["id"], worker_id, now_v),
                )
                if cur.rowcount == 1:
                    # B9: ship the campaign's cloud-side spend total WITH the lease, on
                    # the same _tx_immediate as the claim, so the box can subtract it
                    # from its box-local AIZU_SPEND_CAP instead of starting at $0.
                    # Resolved here (not baked at enqueue) because a queued job may sit
                    # while other boxes spend against the same campaign.
                    prior = c.execute(
                        "SELECT COALESCE(SUM(usd),0) AS t FROM spend_log WHERE campaign_id=?",
                        (row["campaign_id"],)).fetchone()
                    return _job_row_to_lease(row, lease_expires_at=now_v + ttl,
                                             prior_spend_usd=float(prior["t"]))
        return None

    def extend_lease(
        self,
        *,
        job_id: str,
        worker_id: str,
        now: Optional[float] = None,
        lease_ttl: Optional[float] = None,
    ) -> bool:
        """Job heartbeat: re-extend the lease and mark the job `running` (BUILD-PLAN
        Phase 3 lease-extension math). Succeeds ONLY while the job is still leased/
        running BY THIS WORKER — never reassigns, never resurrects a terminal/dead-
        lettered job. Returns True iff exactly one such row was extended; False tells
        the caller the lease is gone (stop the run / drain). Uses _tx_immediate so the
        extension can't race a concurrent reclaim."""
        now_v = now if now is not None else time.time()
        ttl = lease_ttl if lease_ttl is not None else default_lease_ttl_sec()
        with self._tx_immediate() as c:
            cur = c.execute(
                """UPDATE jobs
                      SET status='running', lease_expires_at=?, updated_at=?
                    WHERE id=? AND leased_by=? AND status IN ('leased','running')
                      AND dead_lettered_at IS NULL""",
                (now_v + ttl, now_v, job_id, worker_id),
            )
            return cur.rowcount == 1

    def ack_job(self, *, job_id: str, worker_id: str,
                summary: dict, leads: Optional[list[dict]] = None,
                spend: Optional[list[dict]] = None,
                worker_db_id: Optional[str] = None) -> bool:
        """Mark a leased job `done`, store its result summary, and persist the captured
        lead BODIES (`leads`) plus the run's SPEND rollup (`spend`, B9) — ALL IN ONE
        TRANSACTION so a crash can never leave a job `done` with its leads or its spend
        lost (Phase-3 atomicity fix). Idempotent: a second ack (row already terminal or
        owned by no one) updates nothing and returns False, so the sync runs exactly
        once. Every lead and every spend row is FORCED under the job's own campaign/org
        (BOLA guard). The `sessions` mirror is observational (panel display only) and
        stays best-effort OUTSIDE the transaction — losing it never loses lead data."""
        now = time.time()
        session_id = summary.get("session_id") or summary.get("sessionId")
        # Warm the (possibly freshly minted) local database identity BEFORE the tx —
        # see database_id(): minting inside would nest a _tx() and commit early.
        if spend and worker_db_id:
            self.database_id()
        with self._tx() as c:
            cur = c.execute(
                """UPDATE jobs
                      SET status='done', result=?, session_id=?,
                          leased_by=NULL, lease_expires_at=NULL, updated_at=?
                    WHERE id=? AND leased_by=? AND status IN ('leased','running')""",
                (json.dumps(summary or {}), session_id, now, job_id, worker_id),
            )
            if cur.rowcount != 1:
                return False
            job = c.execute(
                "SELECT id, org_id, campaign_id, platform FROM jobs WHERE id=?",
                (job_id,)).fetchone()
            # Same transaction as the done-marking: either the job is done AND every
            # lead is committed, or the whole ack rolls back and the job stays leased
            # for ReclaimManager to requeue. No partial 'done-but-leads-gone' window.
            self._sync_acked_leads(c, job, leads)
            self._sync_acked_spend(c, job, spend, session_id=session_id,
                                   worker_db_id=worker_db_id)
        self._mirror_acked_session(job, session_id, summary)
        return True

    def _sync_acked_leads(self, c: sqlite3.Cursor, job,
                          leads: Optional[list[dict]]) -> None:
        """Persist the worker-supplied captured leads into the cloud `matches` table on
        the caller's cursor `c` (the ack transaction — see `ack_job`). Each lead is
        FORCED under the job's OWN campaign (org is stamped from that campaign) — a
        worker can never write a lead into another campaign or org, whatever it claims
        in the payload (the BOLA guard). A malformed row is skipped after validation;
        a genuine write failure PROPAGATES so the whole ack rolls back (job stays leased
        → requeued) rather than committing a half-synced batch. Idempotent via the
        matches PK, so a re-run preserves any human-set status."""
        if not leads or job is None or not isinstance(leads, list):
            return
        campaign_id = job["campaign_id"]
        # A job whose campaign resolves to no org is an infrastructure invariant
        # violation — skip the whole sync (never write NULL-org leads that pollute
        # the matches table and are invisible to tenant reads) and log it loudly.
        if self.org_for_campaign(campaign_id) is None:
            logger.warning("lead sync skipped: job %s campaign %s resolves to no org",
                           job["id"], campaign_id)
            return
        default_platform = job["platform"] or DEFAULT_PLATFORM
        rows = leads[:MAX_SYNC_LEADS]
        if len(leads) > MAX_SYNC_LEADS:
            logger.warning(
                "lead sync capped at %d of %d for campaign %s (excess dropped)",
                MAX_SYNC_LEADS, len(leads), campaign_id)
        synced = 0
        for lead in rows:
            if not isinstance(lead, dict):
                continue
            comment_id = _lead_str(lead.get("commentId") or lead.get("comment_id"))
            reel_id = _lead_str(lead.get("reelId") or lead.get("reel_id"))
            if not comment_id or not reel_id:
                continue  # a match needs both keys; skip a malformed row
            extracted = lead.get("extracted")
            if not isinstance(extracted, dict):
                extracted = None
            elif len(json.dumps(extracted, ensure_ascii=False)) > MAX_EXTRACTED_BYTES:
                # An oversized untrusted blob is dropped (the lead still syncs) rather
                # than bloating the row — the structured fields carry the real signal.
                logger.warning("lead sync dropped an oversized extracted blob "
                               "(job=%s comment=%s)", job["id"], comment_id)
                extracted = None
            # Every field is sanitized above (types coerced, strings capped, oversized
            # blob dropped), so the row is well-formed; a failure here is a genuine DB
            # error and must ABORT the atomic ack (rollback → requeue), not be swallowed.
            self._upsert_match_row(
                c,
                campaign_id=campaign_id,                       # FORCED (BOLA)
                reel_id=reel_id,
                comment_id=comment_id,
                username=_lead_str(lead.get("username")),
                text=_lead_str(lead.get("text")),
                lang=_lead_str(lead.get("lang")),
                score=_lead_float(lead.get("score"), 0.0) or 0.0,
                reason=_lead_str(lead.get("reason")) or "",
                # v27: an OLDER worker omits the key entirely — `_upsert_match_row`
                # COALESCEs a None, so it never blanks an intent already stored.
                intent=_lead_str(lead.get("intent")),
                extracted=extracted,
                tier=_lead_str(lead.get("tier")) or "local",
                session_id=_lead_str(lead.get("sessionId") or lead.get("session_id")),
                platform=_lead_str(lead.get("platform")) or default_platform,
                captured_at=_lead_float(
                    lead.get("capturedAt") or lead.get("captured_at"), None),
                found_by_models=_lead_str_list(
                    lead.get("foundByModels") or lead.get("found_by_models")),
            )
            synced += 1
        logger.info("lead sync: %d/%d leads upserted for campaign %s",
                    synced, len(rows), campaign_id)

    def _sync_acked_spend(self, c: sqlite3.Cursor, job,
                          spend: Optional[list[dict]], *,
                          session_id: Optional[str] = None,
                          worker_db_id: Optional[str] = None) -> None:
        """Roll the worker-reported spend delta into the CLOUD `spend_log` on the
        caller's cursor `c` (the ack/nack transaction) — B9.

        Without this the cloud spend_log never sees a single fleet dollar: the only
        writer of spend is `router._record` on the box, so a campaign's cap silently
        restarted at $0 on every machine and the panel showed `spent` $0 / `cpl` None
        for a fleet-run campaign. Rows are FORCED under the job's OWN campaign (org
        stamped from that campaign — the same BOLA guard as `_sync_acked_leads`).

        SHARED-DATABASE GUARD (the reason `worker_db_id` exists): the worker's
        `AIZU_DB` defaults to the SAME `aizu.db` filename the bridge uses, and the
        same-box dev/desktop topology genuinely points both at one file. There the
        child ALREADY wrote these rows into this very table, and spend_log is an
        append-only AUTOINCREMENT table with no unique key — so unlike
        `_sync_acked_leads` (idempotent via the matches PK) a second insert would
        silently DOUBLE the campaign's spend and trip its cap at half the budget. When
        the reported database identity is ours, the rollup is skipped entirely.

        A malformed/zero/non-finite row is SKIPPED, never raised on — losing a
        best-effort accounting row must not roll back a legitimate ack/nack."""
        if not spend or job is None or not isinstance(spend, list):
            return
        if worker_db_id and worker_db_id == self.database_id():
            logger.debug("spend sync skipped: worker reports our own database id")
            return
        campaign_id = job["campaign_id"]
        if self.org_for_campaign(campaign_id) is None:
            logger.warning("spend sync skipped: job %s campaign %s resolves to no org",
                           job["id"], campaign_id)
            return
        rows = spend[:MAX_SYNC_SPEND_ROWS]
        if len(spend) > MAX_SYNC_SPEND_ROWS:
            logger.warning(
                "spend sync capped at %d of %d for campaign %s (excess dropped)",
                MAX_SYNC_SPEND_ROWS, len(spend), campaign_id)
        now = time.time()
        synced, total = 0, 0.0
        for row in rows:
            if not isinstance(row, dict):
                continue
            usd = _lead_float(row.get("usd"), None)
            if usd is None or not math.isfinite(usd) or usd <= 0:
                continue  # zero/negative/NaN spend carries no information — drop it
            stage = _lead_str(row.get("stage")) or "fleet"
            model = _lead_str(row.get("model"))
            at = _lead_float(
                row.get("at") or row.get("createdAt") or row.get("created_at"), None)
            # Clamp to now: `spend_by_day` buckets on created_at, so an honest earlier
            # timestamp keeps a midnight-spanning run on the right day, while a bogus
            # future one can never park spend in a day that has not happened.
            created_at = min(at, now) if (at is not None and math.isfinite(at)
                                          and at > 0) else now
            c.execute(
                """INSERT INTO spend_log(campaign_id, session_id, stage, model, usd, created_at)
                   VALUES(?,?,?,?,?,?)""",
                (campaign_id, session_id, stage, model, float(usd), created_at))
            synced += 1
            total += float(usd)
        logger.info("spend sync: %d/%d rows ($%.4f) rolled up for campaign %s",
                    synced, len(rows), total, campaign_id)

    # ----- v16 platform settings (superadmin execution-backend switch) -----

    def get_platform_setting(self, key: str, default: Optional[str] = None) -> Optional[str]:
        """Read a platform-wide setting value, or `default` if unset."""
        row = self._conn.execute(
            "SELECT value FROM platform_settings WHERE key=?", (key,)).fetchone()
        return row["value"] if row else default

    def set_platform_setting(self, key: str, value: str, *,
                             by: Optional[str] = None) -> None:
        """Upsert a platform-wide setting (records the acting admin for provenance)."""
        now = time.time()
        with self._tx() as c:
            c.execute(
                """INSERT INTO platform_settings(key, value, updated_at, updated_by)
                   VALUES(?,?,?,?)
                   ON CONFLICT(key) DO UPDATE SET
                     value=excluded.value, updated_at=excluded.updated_at,
                     updated_by=excluded.updated_by""",
                (key, value, now, by))

    def database_id(self) -> str:
        """This database's stable identity (see DATABASE_ID_KEY), minted on first read.

        Lives in `platform_settings`, which is base DDL — so EVERY Store has it,
        worker-local ones included, with no migration. Cached per Store instance: the
        ack/nack spend roll-up compares it inside a write transaction, and minting it
        there would open a NESTED `_tx()` whose commit would prematurely commit the
        ack's own UPDATE. `ack_job`/`nack_job` therefore warm this BEFORE opening their
        transaction; the in-transaction call is then always a cache hit."""
        cached = getattr(self, "_database_id", None)
        if cached:
            return cached
        existing = self.get_platform_setting(DATABASE_ID_KEY)
        if not existing:
            # DO NOTHING, not set_platform_setting's DO UPDATE: in the shared-db
            # topology the worker's Store and the bridge's Store are the SAME file and
            # may mint concurrently. First writer must win — a last-writer upsert would
            # leave the two processes holding different ids, defeating the guard exactly
            # when it matters. Re-read afterwards so we adopt whoever won.
            with self._tx() as c:
                c.execute(
                    """INSERT INTO platform_settings(key, value, updated_at, updated_by)
                       VALUES(?,?,?,NULL) ON CONFLICT(key) DO NOTHING""",
                    (DATABASE_ID_KEY, uuid.uuid4().hex, time.time()))
            existing = self.get_platform_setting(DATABASE_ID_KEY)
            if not existing:  # unreachable in practice; never cache/return an empty id
                raise RuntimeError("could not establish a database id")
        self._database_id = existing
        return existing

    def execution_backend(self) -> str:
        """The active run execution backend — `in_process` (default) or `distributed`.
        An unknown/corrupt stored value falls back to the safe in-process default."""
        value = self.get_platform_setting(EXECUTION_BACKEND_KEY, EXECUTION_IN_PROCESS)
        return value if value in EXECUTION_BACKENDS else EXECUTION_IN_PROCESS

    def set_execution_backend(self, backend: str, *, by: Optional[str] = None) -> None:
        """Set the platform-wide run execution backend. Raises ValueError on an
        unknown backend (the API boundary maps that to a 400)."""
        if backend not in EXECUTION_BACKENDS:
            raise ValueError(f"unknown execution backend: {backend!r}")
        self.set_platform_setting(EXECUTION_BACKEND_KEY, backend, by=by)

    # ----- v17 platform settings (superadmin model-comparison switch) -----

    def model_comparison_enabled(self) -> bool:
        """Whether the model-comparison fan-out is on. OFF by default — an unset or
        corrupt stored value is read as OFF, never as an accidental opt-in."""
        return self.get_platform_setting(MODEL_COMPARISON_ENABLED_KEY, "0") == "1"

    def set_model_comparison_enabled(self, enabled: bool, *, by: Optional[str] = None) -> None:
        """Set the platform-wide model-comparison switch."""
        self.set_platform_setting(MODEL_COMPARISON_ENABLED_KEY, "1" if enabled else "0", by=by)

    # ----- v17 model-comparison call log (superadmin "Model Performance" page) -----

    def log_model_comparison(self, *, campaign_id: str, stage: str, model: str,
                             is_primary: bool, session_id: Optional[str] = None,
                             platform: Optional[str] = None, label: Optional[str] = None,
                             score: Optional[float] = None, confidence: Optional[float] = None,
                             agreed: Optional[bool] = None, latency_ms: Optional[float] = None,
                             usd: Optional[float] = None, error: Optional[str] = None) -> None:
        """Record one model's outcome for one fan-out call. Called from the router's
        calling thread only, after every comparison future has resolved — the store's
        sqlite3 connection is not safe to touch from a spawned worker thread."""
        with self._tx() as c:
            c.execute(
                """INSERT INTO model_comparison_log
                     (campaign_id, session_id, platform, stage, model, is_primary,
                      label, score, confidence, agreed, latency_ms, usd, error, created_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (campaign_id, session_id, platform, stage, model, int(is_primary),
                 label, score, confidence,
                 None if agreed is None else int(agreed),
                 latency_ms, usd, error, time.time()))

    def model_comparison_stats(self, campaign_id: Optional[str] = None) -> list[dict[str, Any]]:
        """Per-model rollup for the Model Performance page: call volume, average
        latency/cost/score, and the agreement rate vs. the primary model's verdict
        (only counting calls a threshold was supplied for — see `agreed`)."""
        where = "WHERE campaign_id=?" if campaign_id else ""
        args = (campaign_id,) if campaign_id else ()
        rows = self._conn.execute(
            f"""SELECT model,
                       MAX(is_primary) AS is_primary,
                       COUNT(*) AS calls,
                       AVG(latency_ms) AS avg_latency_ms,
                       AVG(usd) AS avg_usd,
                       AVG(score) AS avg_score,
                       AVG(CASE WHEN agreed IS NOT NULL THEN agreed END) AS agreement_rate,
                       SUM(CASE WHEN error IS NOT NULL THEN 1 ELSE 0 END) AS errors
                  FROM model_comparison_log
                  {where}
                 GROUP BY model
                 ORDER BY is_primary DESC, calls DESC""",
            args).fetchall()
        out = []
        for r in rows:
            leads_found = self._conn.execute(
                "SELECT COUNT(*) FROM matches WHERE found_by_models LIKE ?",
                (f'%"{r["model"]}"%',)).fetchone()[0]
            out.append({
                "model": r["model"],
                "isPrimary": bool(r["is_primary"]),
                "calls": r["calls"],
                "avgLatencyMs": r["avg_latency_ms"],
                "avgUsd": r["avg_usd"],
                "avgScore": r["avg_score"],
                "agreementRate": r["agreement_rate"],
                "errors": r["errors"],
                "leadsFound": leads_found,
            })
        return out

    def model_comparison_recent(self, *, campaign_id: Optional[str] = None,
                                limit: int = 200) -> list[dict[str, Any]]:
        """Raw recent fan-out call rows, newest first, for a table view."""
        where = "WHERE campaign_id=?" if campaign_id else ""
        args = (campaign_id, limit) if campaign_id else (limit,)
        rows = self._conn.execute(
            f"""SELECT campaign_id, session_id, platform, stage, model, is_primary,
                       label, score, confidence, agreed, latency_ms, usd, error, created_at
                  FROM model_comparison_log
                  {where}
                 ORDER BY created_at DESC, id DESC LIMIT ?""",
            args).fetchall()
        return [dict(r) for r in rows]

    def _mirror_acked_session(self, job, session_id: Optional[str],
                              summary: dict) -> None:
        """Best-effort cloud-side session mirror for an acked job. A mirror failure must
        NOT fail the ack (the job is already authoritatively done) — log and move on."""
        if not session_id or job is None:
            return
        try:
            self.start_session(
                session_id, job["campaign_id"], job["platform"],
                run_id=summary.get("run_id"), org_id=job["org_id"],
                engine_mode=summary.get("engine_mode", "harvest"))
            self.update_counters(session_id, SessionCounters(
                reels_seen=int(summary.get("reels_seen", 0) or 0),
                matches=int(summary.get("matches", 0) or 0),
                escalations=int(summary.get("escalations", 0) or 0),
                spend_usd=float(summary.get("spend_usd", 0.0) or 0.0)))
            self.end_session(session_id, status="completed",
                             halt_reason=summary.get("halt_reason"))
        except Exception:  # noqa: BLE001 — mirror is observational; never fail the ack
            logger.warning("ack session mirror failed for job session %s",
                           session_id, exc_info=True)

    def _mirror_nacked_sessions(self, job, run_id: Optional[str],
                                leads: Optional[list[dict]]) -> None:
        """Cloud-side session rows for leads that arrived on a NACK, so those leads are
        actually COUNTABLE.

        A fleet job's sessions live in the worker's local DB; the cloud only ever learns
        of one through `_mirror_acked_session`, which runs on the ACK path alone. Leads
        synced from a nack therefore carry a `session_id` with no matching cloud row —
        and every customer-facing count (`lead_counts_by_run`, `matches_for_run`) JOINs
        matches->sessions. Without this the leads are stored, correct, BOLA-forced, and
        completely invisible: the campaign card still reads "0 leads" beside a real
        spend figure. Storing a lead nobody can count is not a fix.

        Best-effort and per-session isolated, exactly like the ack mirror: this is
        observational data and a failure here must never undo a nack that is already
        recorded. Sessions are stamped `halted` because that is what they are — the
        attempt ended without an ack — and an existing row is left alone so a later ack
        mirror can overwrite it with the real summary counters."""
        if job is None or not run_id or not leads:
            return
        seen: list[str] = []
        for lead in leads:
            if not isinstance(lead, dict):
                continue
            sid = lead.get("sessionId") or lead.get("session_id")
            if isinstance(sid, str) and sid and sid not in seen:
                seen.append(sid)
        for sid in seen:
            try:
                exists = self._conn.execute(
                    "SELECT 1 FROM sessions WHERE session_id=?", (sid,)).fetchone()
                if exists:
                    continue
                self.start_session(sid, job["campaign_id"], job["platform"],
                                   run_id=run_id, org_id=job["org_id"])
                self.end_session(sid, status="halted",
                                 halt_reason="attempt ended without an ack")
            except Exception:  # noqa: BLE001 — mirror is observational; never fail a nack
                logger.warning("nack session mirror failed for session %s",
                               sid, exc_info=True)

    def _close_sessions_for_run(self, c: sqlite3.Cursor, run_id: Optional[str], *,
                                halt_reason: str, now: float) -> int:
        """Close (status='halted') every session of `run_id` still stuck 'running'
        (ended_at IS NULL), on the CALLER's cursor `c` so it commits atomically with the
        job mutation that abandoned them. A fleet job's engine child writes its own
        'running' session row; on a hard-stop the child is SIGKILLed and never reaches
        `end_session`/`session_crash_guard`, so every requeue/dead-letter/reclaim of the
        job MUST close the row(s) it stranded — otherwise a failed/retried job leaves
        orphan 'running' sessions that the run history shows as live forever. Correlates
        by run_id, NOT jobs.session_id, which is empty until ack (a failed/never-acked
        job never populates it). No-op (returns 0) when run_id is falsy. Returns the
        count closed. run_id is unique per enqueue, so this only touches this job's
        own dead attempts."""
        if not run_id:
            return 0
        cur = c.execute(
            """UPDATE sessions SET ended_at=?, status='halted', halt_reason=?
                 WHERE run_id=? AND status='running' AND ended_at IS NULL""",
            (now, halt_reason, run_id))
        return cur.rowcount

    def nack_job(self, *, job_id: str, worker_id: str, reason: str,
                 retry_after_at: Optional[float] = None,
                 poison: bool = False,
                 leads: Optional[list[dict]] = None,
                 spend: Optional[list[dict]] = None,
                 worker_db_id: Optional[str] = None,
                 now: Optional[float] = None) -> dict[str, Any]:
        """Fail a leased job: requeue with backoff, or dead-letter when attempts are
        exhausted or the failure is poison (won't fix itself on retry). Increments
        `attempts`. Returns ``{"outcome": "requeued"|"dead_lettered"|"ignored",
        "attempts", "retryAfterAt"}``. Idempotent on a terminal/foreign row (outcome
        'ignored' — never double-counts attempts or resurrects a done job).

        `spend` is the B9 rollup: a crashed/halted attempt spent real money before it
        died, and a REQUEUE is unpinned (attempt 2 can land on a box that has neither
        the local spend rows nor any cloud record of them), so the nack path must roll
        spend up exactly like the ack path or up to DEFAULT_JOB_MAX_ATTEMPTS attempts'
        worth of spend goes unaccounted. Nothing is recorded on the 'ignored' outcome —
        that row is not this worker's to write against.

        `leads` is spend's sibling and lands here for the SAME argument, which was
        simply never made for leads: a job that exhausts its attempts NEVER acks, and
        the ack body was the only place leads travelled — so a dead-lettered run shipped
        its spend and stranded its whole harvest on the worker's disk. Not an edge case:
        a run that hits its wall-clock cap before its lead target dead-letters BY
        DESIGN, so the ordinary outcome billed the customer and delivered nothing.
        Written in the SAME transaction as the jobs mutation (an interrupted nack leaves
        neither), FORCED under the job's own campaign by `_sync_acked_leads` (BOLA), and
        idempotent via the matches PK — a run that nacks with leads and later acks with
        the same leads upserts rather than duplicating, and a human-set `status`
        survives because it is written on first insert only. Recorded on 'requeued' and
        'dead_lettered', NEVER on 'ignored' — a late nack from a superseded attempt must
        not write leads into a run that already finished."""
        now_v = now if now is not None else time.time()
        # See ack_job: warm the local database identity outside the transaction.
        if spend and worker_db_id:
            self.database_id()
        with self._tx() as c:
            # id/org_id/campaign_id/platform are selected for _sync_acked_spend's BOLA
            # forcing — reading them off the sqlite3.Row would otherwise raise IndexError
            # inside the tx, 500 the nack, and strand the job leased until reclaim.
            row = c.execute(
                """SELECT id, org_id, campaign_id, platform, attempts, max_attempts,
                          status, leased_by, spec
                     FROM jobs WHERE id=?""",
                (job_id,)).fetchone()
            if row is None or row["leased_by"] != worker_id \
                    or row["status"] not in ("leased", "running"):
                return {"outcome": "ignored", "attempts": row["attempts"] if row else 0,
                        "retryAfterAt": None}
            # The child that ran this attempt is already dead by the time its worker
            # nacks, so its 'running' session is an orphan — close it in the SAME tx as
            # the jobs mutation (crash between the two leaves BOTH unchanged).
            run_id = _decode_job_spec(row["spec"]).get("run_id")
            attempts = int(row["attempts"]) + 1
            if poison or attempts >= int(row["max_attempts"]):
                c.execute(
                    """UPDATE jobs
                          SET status='failed', attempts=?, dead_lettered_at=?,
                              result=?, leased_by=NULL, lease_expires_at=NULL,
                              retry_after_at=NULL, updated_at=?
                        WHERE id=?""",
                    (attempts, now_v, json.dumps({"reason": reason, "poison": poison}),
                     now_v, job_id),
                )
                self._close_sessions_for_run(
                    c, run_id, now=now_v,
                    halt_reason="job dead-lettered: worker never closed session")
                self._sync_acked_leads(c, row, leads)
                self._sync_acked_spend(c, row, spend, worker_db_id=worker_db_id)
                outcome = {"outcome": "dead_lettered", "attempts": attempts,
                           "retryAfterAt": None,
                           "leadsSynced": len(leads or [])}
            else:
                ra = retry_after_at if retry_after_at is not None \
                    else now_v + nack_backoff_sec(attempts)
                # Persist the reason on the REQUEUE branch too (B6): without it a job that
                # fails its CDP probe leaves no record of why anywhere but the worker box's
                # local log, and the panel renders a blank "Finished on the fleet". `result`
                # is diagnostic only — `status`/`dead_lettered_at` remain the sole terminal
                # signal, and a later ack_job overwrites this blob with the run summary.
                c.execute(
                    """UPDATE jobs
                          SET status='queued', attempts=?, retry_after_at=?, result=?,
                              leased_by=NULL, lease_expires_at=NULL, updated_at=?
                        WHERE id=?""",
                    (attempts, ra,
                     json.dumps({"reason": reason, "poison": poison, "requeued": True}),
                     now_v, job_id),
                )
                self._close_sessions_for_run(
                    c, run_id, now=now_v,
                    halt_reason="job requeued: prior attempt session abandoned")
                self._sync_acked_leads(c, row, leads)
                self._sync_acked_spend(c, row, spend, worker_db_id=worker_db_id)
                outcome = {"outcome": "requeued", "attempts": attempts,
                           "retryAfterAt": ra, "leadsSynced": len(leads or [])}
        # OUTSIDE the tx, exactly like ack_job's mirror. Without a cloud `sessions` row
        # the leads we just wrote are invisible: `lead_counts_by_run` and
        # `matches_for_run` both JOIN matches->sessions on session_id, so a nack-synced
        # lead with no mirrored session would store fine and still render "0 leads" on
        # the campaign card — the exact symptom this change exists to remove.
        # Observational: a mirror failure must never undo a recorded nack.
        self._mirror_nacked_sessions(row, run_id, leads)
        return outcome

    def get_job(self, job_id: str) -> Optional[dict[str, Any]]:
        """Read one job by id (read-only). Returns the decoded row or None."""
        row = self._conn.execute(
            "SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
        return _job_row_to_dict(row) if row else None

    def get_leased_job_for_worker(self, job_id: str,
                                  worker_id: str) -> Optional[dict[str, Any]]:
        """Read-only: the job row IFF it is CURRENTLY leased/running AND `leased_by`
        is exactly `worker_id`; None otherwise (unknown id, another worker's job, an
        already-terminal job, or a lease that expired but hasn't been reclaimed yet —
        `lease_expires_at` is intentionally NOT re-checked against now() here, mirroring
        `ack_job`/`nack_job`'s own `leased_by=?` ownership check, not `extend_lease`'s
        freshness one).

        This is the per-job worker-plane credential endpoint's authorization gate
        (SECURITY REVIEW CRITICAL/HIGH): deliberately TIGHTER than
        `_job_capability_covers`, which only proves a worker COULD serve jobs like this
        one — it says nothing about whether THIS worker currently holds THIS job. A
        worker that merely shares the job's (org, platform) capability, or held the
        lease previously, must never be able to pull the job's decrypted credential."""
        row = self._conn.execute(
            """SELECT * FROM jobs
                WHERE id=? AND leased_by=? AND status IN ('leased','running')
                  AND dead_lettered_at IS NULL""",
            (job_id, worker_id)).fetchone()
        return _job_row_to_dict(row) if row else None

    def get_job_for_run(self, run_id: str,
                        org_id: int) -> Optional[dict[str, Any]]:
        """The latest job whose `spec` JSON `run_id` equals `run_id` AND belongs to
        `org_id` (BOLA guard — a run id from another tenant never resolves). Read-only;
        returns the same decoded dict shape as `get_job`, or None when no such job.

        Prefers a JSON1 `json_extract` filter; if JSON1 is unavailable (older SQLite),
        falls back to a Python-side scan of the org's jobs, decoding each `spec`."""
        try:
            row = self._conn.execute(
                """SELECT * FROM jobs
                    WHERE json_extract(spec, '$.run_id')=? AND org_id=?
                    ORDER BY created_at DESC, rowid DESC LIMIT 1""",
                (run_id, org_id)).fetchone()
            return _job_row_to_dict(row) if row else None
        except sqlite3.OperationalError:
            # JSON1 not compiled in → scan the org's jobs newest-first, decode spec.
            rows = self._conn.execute(
                "SELECT * FROM jobs WHERE org_id=? ORDER BY created_at DESC, rowid DESC",
                (org_id,)).fetchall()
            for row in rows:
                if _decode_job_spec(row["spec"]).get("run_id") == run_id:
                    return _job_row_to_dict(row)
            return None

    def last_event_at_for_run(self, run_id: str,
                              org_id: Optional[int] = None) -> Optional[float]:
        """MAX(created_at) across this run's narrative events (epoch seconds), or None
        when the run has emitted no events. Org-scoped when `org_id` is given (durable
        ownership gate); pass None to read unscoped (ownership already proven)."""
        if org_id is None:
            row = self._conn.execute(
                "SELECT MAX(created_at) AS m FROM run_events WHERE run_id=?",
                (run_id,)).fetchone()
        else:
            row = self._conn.execute(
                "SELECT MAX(created_at) AS m FROM run_events "
                "WHERE run_id=? AND org_id=?", (run_id, org_id)).fetchone()
        m = row["m"] if row else None
        return float(m) if m is not None else None

    def active_fleet_runs_for_org(self, org_id: int) -> dict[str, str]:
        """Map ``campaign_id -> run_id`` for the org's ACTIVE fleet jobs (status in
        queued|leased|running), keeping only the newest run per campaign. `run_id` is
        read from each job's decoded `spec`; a job whose spec carries no run_id is
        skipped. Backs the refresh-durable `fleetRunId` on the campaigns page — a DB
        read, so it survives a server restart. One query + a Python-side spec decode
        (fine at current job volumes)."""
        rows = self._conn.execute(
            """SELECT campaign_id, spec FROM jobs
                WHERE org_id=? AND status IN ('queued','leased','running')
                ORDER BY created_at DESC, rowid DESC""",
            (org_id,)).fetchall()
        out: dict[str, str] = {}
        for row in rows:
            cid = row["campaign_id"]
            if cid in out:  # newest already taken (rows are DESC)
                continue
            run_id = _decode_job_spec(row["spec"]).get("run_id")
            if run_id:
                out[cid] = run_id
        return out

    def reclaim_offline_jobs(
        self, *, now: Optional[float] = None,
        offline_after_sec: Optional[float] = None,
        alert_after_sec: Optional[float] = None,
        dead_after_sec: Optional[float] = None,
    ) -> list[dict[str, Any]]:
        """Proactively reclaim jobs whose lease has EXPIRED (BUILD-PLAN Phase 4 atomic
        offline→interrupted→requeue). A job is reclaimable the moment its lease lapses
        (``lease_expires_at < now``) — an expired lease IS the authoritative "the job's
        per-job heartbeat stopped extending → the job is dead" signal. A healthy worker
        renews its lease before expiry and the lease TTL already tolerates ordinary
        slowness, so an expired lease means the run wedged/crashed, NOT that the box is
        merely slow. We deliberately DO NOT also require the worker to be offline: a box
        keeps a SEPARATE presence heartbeat alive even after a job child wedges (e.g. a
        180s hang on connect_over_cdp), so gating reclaim on worker-offline let a
        still-present-but-wedged box hold an expired lease forever — the job stayed
        ``leased``/``running`` and ``enqueue_job_deduped`` rejected every redispatch with
        "already running" (root-caused live 2026-07-03). Each reclaimed job is either
        requeued PINNED to its original worker (one account ↔ one box, no cross-box
        failover) with backoff + an incremented attempt, or DEAD-LETTERED when attempts
        are exhausted. The whole scan+mutation runs under ``_tx_immediate`` so it can't
        race a concurrent lease/extend. Returns one record per reclaimed job for
        alerting: ``{"jobId", "workerId", "offlineSec", "outcome", "attempts"}``.

        SAFETY (no two concurrent live children for one account): reclaim requeues PINNED
        to the ORIGINAL box, so if that box is still online it may re-lease this same job.
        That is safe because (a) the worker's per-account single-flight lock
        (``sidecar._handle_lease``) forbids a second concurrent child for the same account
        on that box, and (b) each reclaim increments ``attempts`` and dead-letters at
        ``max_attempts``, bounding retries. Reclaim only ever RE-QUEUES an expired-lease
        job (never spawns a run), so it cannot itself create a second live child.

        ``offline_after_sec`` no longer gates reclaim; it now governs ONLY the
        presence-based health-flag ALERT below (a box dark past the threshold is an
        operator cue), which is orthogonal to whether an expired lease gets reclaimed.

        Raises a visible health_flag (best-effort, outside the write lock) for any worker
        dark longer than ``alert_after_sec`` (default 5 min) — an operator's cue that a
        box may be gone for good and its pinned account needs manual reassignment."""
        now_v = now if now is not None else time.time()
        # ``offline_after_sec`` is accepted for signature stability but no longer gates
        # reclaim (an expired lease is the death signal, not worker presence — see the
        # docstring). It is retained only so callers/tests may still pass it.
        _ = offline_after_sec
        alert_after = (alert_after_sec if alert_after_sec is not None
                       else WORKER_RECLAIM_ALERT_SEC)
        dead_after = (dead_after_sec if dead_after_sec is not None
                      else WORKER_PINNED_DEAD_LETTER_SEC)
        dead_before = now_v - dead_after
        reclaimed: list[dict[str, Any]] = []
        with self._tx_immediate() as c:
            rows = c.execute(
                """SELECT j.id AS id, j.leased_by AS leased_by, j.attempts AS attempts,
                          j.max_attempts AS max_attempts, j.org_id AS org_id,
                          j.campaign_id AS campaign_id, j.platform AS platform,
                          j.spec AS spec,
                          w.last_heartbeat_at AS last_heartbeat_at
                     FROM jobs j
                     LEFT JOIN workers w ON w.id = j.leased_by
                    WHERE j.status IN ('leased','running')
                      AND j.leased_by IS NOT NULL
                      AND j.dead_lettered_at IS NULL
                      AND j.lease_expires_at IS NOT NULL AND j.lease_expires_at < ?""",
                (now_v,),
            ).fetchall()
            for row in rows:
                worker_id = row["leased_by"]
                run_id = _decode_job_spec(row["spec"]).get("run_id")
                attempts = int(row["attempts"]) + 1
                last_hb = row["last_heartbeat_at"]
                offline_sec = None if last_hb is None else (now_v - float(last_hb))
                if attempts >= int(row["max_attempts"]):
                    c.execute(
                        """UPDATE jobs
                              SET status='failed', attempts=?, dead_lettered_at=?,
                                  result=?, leased_by=NULL, lease_expires_at=NULL,
                                  retry_after_at=NULL, pinned_worker_id=NULL, updated_at=?
                            WHERE id=?""",
                        (attempts, now_v,
                         json.dumps({"reason": "worker_offline", "interrupted": True,
                                     "prev_worker": worker_id}),
                         now_v, row["id"]),
                    )
                    outcome = "dead_lettered"
                    self._close_sessions_for_run(
                        c, run_id, now=now_v,
                        halt_reason="reclaimed: worker offline/wedged, session abandoned")
                else:
                    ra = now_v + nack_backoff_sec(attempts)
                    c.execute(
                        """UPDATE jobs
                              SET status='queued', attempts=?, retry_after_at=?,
                                  leased_by=NULL, lease_expires_at=NULL,
                                  pinned_worker_id=?, result=?, updated_at=?
                            WHERE id=?""",
                        (attempts, ra, worker_id,
                         json.dumps({"reason": "worker_offline", "interrupted": True,
                                     "prev_worker": worker_id}),
                         now_v, row["id"]),
                    )
                    outcome = "requeued"
                    self._close_sessions_for_run(
                        c, run_id, now=now_v,
                        halt_reason="reclaimed: worker offline/wedged, session abandoned")
                reclaimed.append({
                    "jobId": row["id"], "workerId": worker_id,
                    "offlineSec": offline_sec, "outcome": outcome,
                    "attempts": attempts, "orgId": row["org_id"],
                    "campaignId": row["campaign_id"], "platform": row["platform"],
                })
            # Second pass — a QUEUED job PINNED to a box that has stayed dark past the
            # long dead-letter grace. reclaim's leased/running scan above never touches a
            # queued row, and no other box may lease it (the pin), so without this it
            # lingers forever. The box is presumed gone: dead-letter + alert for manual
            # reassignment (never un-pin/failover — the one-account↔one-box safety
            # invariant holds even here, risk #2).
            orphans = c.execute(
                """SELECT j.id AS id, j.pinned_worker_id AS pinned_worker_id,
                          j.attempts AS attempts, j.org_id AS org_id,
                          j.campaign_id AS campaign_id, j.platform AS platform,
                          j.spec AS spec,
                          w.id AS worker_row_id, w.last_heartbeat_at AS last_heartbeat_at,
                          w.registered_at AS registered_at
                     FROM jobs j
                     LEFT JOIN workers w ON w.id = j.pinned_worker_id
                    WHERE j.status='queued'
                      AND j.pinned_worker_id IS NOT NULL
                      AND j.dead_lettered_at IS NULL
                      AND (w.id IS NULL
                           OR COALESCE(w.last_heartbeat_at, w.registered_at, 0) < ?)""",
                (dead_before,),
            ).fetchall()
            for row in orphans:
                worker_id = row["pinned_worker_id"]
                run_id = _decode_job_spec(row["spec"]).get("run_id")
                last_hb = row["last_heartbeat_at"] or row["registered_at"]
                offline_sec = None if last_hb is None else (now_v - float(last_hb))
                c.execute(
                    """UPDATE jobs
                          SET status='failed', dead_lettered_at=?, result=?,
                              pinned_worker_id=NULL, retry_after_at=NULL, updated_at=?
                        WHERE id=?""",
                    (now_v,
                     json.dumps({"reason": "pinned_worker_dead", "interrupted": True,
                                 "prev_worker": worker_id}),
                     now_v, row["id"]),
                )
                self._close_sessions_for_run(
                    c, run_id, now=now_v,
                    halt_reason="reclaimed: pinned worker dead, session abandoned")
                reclaimed.append({
                    "jobId": row["id"], "workerId": worker_id,
                    "offlineSec": offline_sec, "outcome": "pinned_dead_lettered",
                    "attempts": int(row["attempts"]), "orgId": row["org_id"],
                    "campaignId": row["campaign_id"], "platform": row["platform"],
                })
        # Alerting runs AFTER the write lock is released (raise_flag opens its own _tx).
        for r in reclaimed:
            if (r["outcome"] == "pinned_dead_lettered"
                    or (r["offlineSec"] is not None and r["offlineSec"] >= alert_after)):
                try:
                    offline_txt = ("unknown" if r["offlineSec"] is None
                                   else f"{int(r['offlineSec'])}s")
                    self.raise_flag(
                        "worker_offline", "warn",
                        detail=(f"worker {r['workerId']} offline {offline_txt} — "
                                f"job {r['jobId']} {r['outcome']}"),
                        campaign_id=r["campaignId"], org_id=r["orgId"])
                except Exception:  # noqa: BLE001 — alerting must never fail the reclaim
                    logger.warning("reclaim alert failed for job %s", r["jobId"],
                                   exc_info=True)
        return reclaimed

    def reconcile_orphan_sessions(self, *, now=None,
                                  active_run_ids: frozenset = frozenset()) -> int:
        """Close sessions stuck at status='running' (ended_at IS NULL) that no longer
        correspond to any live run. Originally called once at server startup: an
        in-process run can't survive a restart (RunManager's active slot is
        in-memory), so any such row is an orphan from a crashed/killed run. A
        session backing a still-in-flight fleet job IS excluded — but correlated by
        run_id (from the job spec), NOT by jobs.session_id: a fleet job populates
        session_id only at ack, so a mid-flight (never-acked) live run had NO
        protection under the old session_id join and a restart would wrongly halt
        it. We exclude any session whose run_id belongs to a live
        (queued/leased/running, not dead-lettered) job.

        v20 (SessionWatchdog): also called PERIODICALLY, not just at startup, so a
        currently-active IN-PROCESS run (RunManager, never in the jobs table) needs
        its own exclusion — pass its run_id(s) as ``active_run_ids`` each tick. A
        session with run_id IS NULL (a direct CLI run outside the panel/RunManager)
        is still always reconciled here, exactly as at startup — that path has no
        run_id to protect it either way; use the panel/RunManager to get
        watchdog/reconcile protection for a long-lived session.

        Returns the count reconciled. Write-locked via _tx_immediate."""
        now_v = now if now is not None else time.time()
        halt_reason = ("reconciled: run ended without closing session "
                       "(server restart)")
        with self._tx_immediate() as c:
            # run_ids of jobs still live in the jobs table (these DO survive a restart);
            # their sessions must be spared. A corrupt/absent spec yields no run_id.
            live_run_ids = {
                rid for r in c.execute(
                    """SELECT spec FROM jobs
                        WHERE dead_lettered_at IS NULL
                          AND status IN ('queued','leased','running')""").fetchall()
                if (rid := _decode_job_spec(r["spec"]).get("run_id"))
            }
            live_run_ids |= set(active_run_ids)
            if live_run_ids:
                placeholders = ",".join("?" * len(live_run_ids))
                cur = c.execute(
                    f"""UPDATE sessions
                          SET status='halted', ended_at=?, halt_reason=?
                        WHERE status='running' AND ended_at IS NULL
                          AND (run_id IS NULL OR run_id NOT IN ({placeholders}))""",
                    (now_v, halt_reason, *live_run_ids))
            else:
                cur = c.execute(
                    """UPDATE sessions
                          SET status='halted', ended_at=?, halt_reason=?
                        WHERE status='running' AND ended_at IS NULL""",
                    (now_v, halt_reason))
            return cur.rowcount

    # ----- v14 Phase 4: lifecycle control flags (source of truth, BUILD-PLAN C6) -----

    def set_control_flag(
        self, *, scope: str, scope_key: str = "",
        drain: Optional[bool] = None, halt: Optional[bool] = None,
        update_required: Optional[bool] = None,
        reason: Optional[str] = None, set_by: Optional[str] = None,
    ) -> dict[str, Any]:
        """UPSERT the control flags for one (scope, scope_key). Only the flags passed
        (non-None) change; omitted flags KEEP their stored value so a caller can flip
        `halt` without clobbering a prior `drain`. `scope` must be one of
        CONTROL_FLAG_SCOPES; `scope_key` is '' for global, else the org id / platform /
        worker id (stringified). Returns the stored row shape."""
        if scope not in CONTROL_FLAG_SCOPES:
            raise ValueError(f"unknown control-flag scope {scope!r}")
        key = "" if scope == "global" else str(scope_key)
        now = time.time()
        with self._tx() as c:
            row = c.execute(
                "SELECT drain, halt, update_required FROM control_flags "
                "WHERE scope=? AND scope_key=?", (scope, key)).fetchone()
            cur = {"drain": bool(row["drain"]), "halt": bool(row["halt"]),
                   "update_required": bool(row["update_required"])} if row else \
                  {"drain": False, "halt": False, "update_required": False}
            merged = {
                "drain": cur["drain"] if drain is None else bool(drain),
                "halt": cur["halt"] if halt is None else bool(halt),
                "update_required": (cur["update_required"] if update_required is None
                                    else bool(update_required)),
            }
            c.execute(
                """INSERT INTO control_flags
                       (scope, scope_key, drain, halt, update_required,
                        reason, set_by, updated_at)
                   VALUES (?,?,?,?,?,?,?,?)
                   ON CONFLICT(scope, scope_key) DO UPDATE SET
                       drain=excluded.drain, halt=excluded.halt,
                       update_required=excluded.update_required,
                       reason=excluded.reason, set_by=excluded.set_by,
                       updated_at=excluded.updated_at""",
                (scope, key, int(merged["drain"]), int(merged["halt"]),
                 int(merged["update_required"]), reason, set_by, now),
            )
        return {"scope": scope, "scopeKey": key, **merged,
                "reason": reason, "setBy": set_by, "updatedAt": now}

    def clear_control_flags(self, *, scope: str, scope_key: str = "") -> bool:
        """Delete the control-flag row for one (scope, scope_key) — resets every flag to
        off. Returns True iff a row was removed. Global uses scope_key=''."""
        if scope not in CONTROL_FLAG_SCOPES:
            raise ValueError(f"unknown control-flag scope {scope!r}")
        key = "" if scope == "global" else str(scope_key)
        with self._tx() as c:
            cur = c.execute(
                "DELETE FROM control_flags WHERE scope=? AND scope_key=?", (scope, key))
            return cur.rowcount > 0

    def list_control_flags(self) -> list[dict[str, Any]]:
        """All set control-flag rows (read-only), newest first. For the admin console."""
        rows = self._conn.execute(
            "SELECT scope, scope_key, drain, halt, update_required, reason, set_by, "
            "updated_at FROM control_flags ORDER BY updated_at DESC").fetchall()
        return [{
            "scope": r["scope"], "scopeKey": r["scope_key"],
            "drain": bool(r["drain"]), "halt": bool(r["halt"]),
            "update_required": bool(r["update_required"]),
            "reason": r["reason"], "setBy": r["set_by"], "updatedAt": r["updated_at"],
        } for r in rows]

    def resolve_control_flags(
        self, *, org_id: Optional[int] = None, platform: Optional[str] = None,
        worker_id: Optional[str] = None,
    ) -> dict[str, bool]:
        """OR-merge every control-flag scope that applies to this job/worker into one
        ``{"drain", "halt", "update_required"}`` (BUILD-PLAN C6). Applicable scopes:
        global (always), org (when org_id given), platform (when platform given), worker
        (when worker_id given). A flag is on if ANY applicable scope has it on — so a
        global halt overrides an unset org, and an org drain adds to a platform halt.
        Read-only, cheap (indexed by PK), re-checkable every heartbeat."""
        wanted = [("global", "")]
        if org_id is not None:
            wanted.append(("org", str(org_id)))
        if platform is not None:
            wanted.append(("platform", str(platform)))
        if worker_id is not None:
            wanted.append(("worker", str(worker_id)))
        merged = {"drain": False, "halt": False, "update_required": False}
        placeholders = " OR ".join("(scope=? AND scope_key=?)" for _ in wanted)
        params = [v for pair in wanted for v in pair]
        rows = self._conn.execute(
            f"SELECT drain, halt, update_required FROM control_flags "
            f"WHERE {placeholders}", params).fetchall()
        for r in rows:
            merged["drain"] = merged["drain"] or bool(r["drain"])
            merged["halt"] = merged["halt"] or bool(r["halt"])
            merged["update_required"] = (merged["update_required"]
                                         or bool(r["update_required"]))
        return merged

    # ----- v5 users + auth sessions -----
    def create_user(self, email: str, password_hash: str) -> int:
        """Insert a user; raises sqlite3.IntegrityError on a duplicate email.

        Email is stored exactly as given — callers normalise (lowercase/strip)
        before this, and the UNIQUE constraint enforces one account per address.
        """
        now = time.time()
        with self._tx() as c:
            cur = c.execute(
                """INSERT INTO users(email, password_hash, created_at, updated_at)
                   VALUES(?,?,?,?)""",
                (email, password_hash, now, now),
            )
            return int(cur.lastrowid)

    def create_user_with_session(self, email: str, password_hash: str, token: str,
                                 expires_at: float) -> int:
        """Atomically create a user and their first session in ONE transaction.

        Either both rows commit or neither does — so a signup never leaves an
        orphaned account with no session. Raises sqlite3.IntegrityError (and
        commits nothing) on a duplicate email.
        """
        now = time.time()
        with self._tx() as c:
            cur = c.execute(
                """INSERT INTO users(email, password_hash, created_at, updated_at)
                   VALUES(?,?,?,?)""",
                (email, password_hash, now, now),
            )
            user_id = int(cur.lastrowid)
            c.execute(
                """INSERT INTO auth_sessions(token, user_id, created_at, expires_at)
                   VALUES(?,?,?,?)""",
                (hash_session_token(token), user_id, now, expires_at),
            )
            return user_id

    def get_user_by_email(self, email: str) -> Optional[dict[str, Any]]:
        row = self._conn.execute(
            "SELECT * FROM users WHERE email=?", (email,)).fetchone()
        return dict(row) if row else None

    def get_user_by_id(self, user_id: int) -> Optional[dict[str, Any]]:
        row = self._conn.execute(
            "SELECT * FROM users WHERE id=?", (user_id,)).fetchone()
        return dict(row) if row else None

    def count_users(self) -> int:
        return int(self._conn.execute("SELECT COUNT(*) AS n FROM users").fetchone()["n"])

    def create_auth_session(self, token: str, user_id: int, expires_at: float) -> None:
        with self._tx() as c:
            c.execute(
                """INSERT INTO auth_sessions(token, user_id, created_at, expires_at)
                   VALUES(?,?,?,?)""",
                (hash_session_token(token), user_id, time.time(), expires_at),
            )

    def get_auth_session_user(self, token: str) -> Optional[dict[str, Any]]:
        """Return the user for a live (non-expired) session token, else None.

        The stored column holds the SHA-256 of the token, so we hash the incoming
        raw token before the lookup. Expired tokens return None and are not an
        error — the caller sees an anonymous request. Sweeping expired rows is
        left to `purge_expired_auth_sessions`.
        """
        row = self._conn.execute(
            """SELECT u.id AS id, u.email AS email, u.org_id AS org_id, u.role AS role,
                      s.expires_at AS expires_at,
                      o.name AS org_name, o.logo AS org_logo, o.description AS org_description
               FROM auth_sessions s JOIN users u ON u.id = s.user_id
               LEFT JOIN organizations o ON o.id = u.org_id
               WHERE s.token=?""",
            (hash_session_token(token),),
        ).fetchone()
        if not row:
            return None
        if float(row["expires_at"]) <= time.time():
            return None
        return {"id": int(row["id"]), "email": row["email"],
                "orgId": int(row["org_id"]) if row["org_id"] is not None else None,
                "role": row["role"],
                "orgName": row["org_name"], "orgLogo": row["org_logo"],
                "orgDescription": row["org_description"]}

    def delete_auth_session(self, token: str) -> bool:
        with self._tx() as c:
            cur = c.execute("DELETE FROM auth_sessions WHERE token=?",
                            (hash_session_token(token),))
            return cur.rowcount > 0

    def purge_expired_auth_sessions(self) -> int:
        with self._tx() as c:
            cur = c.execute(
                "DELETE FROM auth_sessions WHERE expires_at <= ?", (time.time(),))
            return cur.rowcount

    # ----- v15 superadmin plane: platform_admins -----
    def create_platform_admin(self, *, email: str, password_hash: str,
                              mfa_secret: str) -> int:
        """Create a platform admin. `password_hash` is a PBKDF2 string (auth.hash_password);
        `mfa_secret` is a Fernet blob of the TOTP secret (aizu.secrets) — NEVER
        plaintext. Email is stored lowercased (the login identity)."""
        email = (email or "").strip().lower()
        if not email:
            raise ValueError("admin email is required")
        if not password_hash or not mfa_secret:
            raise ValueError("password_hash and mfa_secret are required")
        now = time.time()
        with self._tx() as c:
            cur = c.execute(
                """INSERT INTO platform_admins(email, password_hash, mfa_secret,
                                               created_at, updated_at)
                   VALUES(?,?,?,?,?)""",
                (email, password_hash, mfa_secret, now, now))
            return int(cur.lastrowid)

    def get_platform_admin_by_email(self, email: str) -> Optional[dict[str, Any]]:
        row = self._conn.execute(
            "SELECT * FROM platform_admins WHERE email=?",
            ((email or "").strip().lower(),)).fetchone()
        return dict(row) if row else None

    def get_platform_admin_by_id(self, admin_id: int) -> Optional[dict[str, Any]]:
        row = self._conn.execute(
            "SELECT * FROM platform_admins WHERE id=?", (admin_id,)).fetchone()
        return dict(row) if row else None

    def count_platform_admins(self) -> int:
        return int(self._conn.execute(
            "SELECT COUNT(*) AS n FROM platform_admins").fetchone()["n"])

    def set_platform_admin_password(self, admin_id: int, password_hash: str) -> bool:
        """Replace an admin's password hash (the out-of-band reset in admin_bootstrap).
        The MFA secret is deliberately untouched — a forgotten password must not cost
        the operator their authenticator enrolment. Returns False if no such admin.

        This does NOT revoke live sessions; the caller pairs it with
        delete_admin_sessions_for_admin so a stolen cookie can't outlive the reset."""
        if not password_hash:
            raise ValueError("password_hash is required")
        with self._tx() as c:
            cur = c.execute(
                "UPDATE platform_admins SET password_hash=?, updated_at=? WHERE id=?",
                (password_hash, time.time(), admin_id))
            return cur.rowcount > 0

    def get_admin_totp_secret(self, admin_id: int) -> Optional[str]:
        """Decrypt the admin's TOTP shared secret from its Fernet blob. Raises
        SecretCipherError (caught at the API boundary) if AIZU_SECRET_KEY is
        missing/invalid; returns None if the admin/secret is absent or the blob has no
        `totp` key. The plaintext secret never touches the DB or a log."""
        row = self._conn.execute(
            "SELECT mfa_secret FROM platform_admins WHERE id=?", (admin_id,)).fetchone()
        if not row or not row["mfa_secret"]:
            return None
        data = self._cipher().decrypt(row["mfa_secret"])
        secret = data.get("totp")
        return secret if isinstance(secret, str) and secret else None

    # ----- v15 superadmin plane: sessions (parallel to auth_sessions) -----
    def create_admin_session(self, token: str, admin_id: int,
                             expires_at: float) -> None:
        with self._tx() as c:
            c.execute(
                """INSERT INTO platform_admin_sessions(token, admin_id, created_at, expires_at)
                   VALUES(?,?,?,?)""",
                (hash_session_token(token), admin_id, time.time(), expires_at))

    def get_admin_session(self, token: str, *,
                          now: Optional[float] = None) -> Optional[dict[str, Any]]:
        """Resolve + authenticate the admin behind an admin session token. Returns the
        admin identity + the impersonation principal (effective org/user), or None for an
        expired session or a disabled admin. The stored token column is the SHA-256 at
        rest, so the raw token is hashed before the lookup (never persisted plaintext)."""
        now = time.time() if now is None else now
        row = self._conn.execute(
            """SELECT s.admin_id AS admin_id, s.effective_org_id AS eff_org,
                      s.effective_user_id AS eff_user,
                      s.impersonation_started_at AS imp_start,
                      s.impersonation_reason AS imp_reason, s.expires_at AS expires_at,
                      a.email AS email, a.disabled_at AS disabled_at
               FROM platform_admin_sessions s JOIN platform_admins a ON a.id = s.admin_id
               WHERE s.token=?""",
            (hash_session_token(token),)).fetchone()
        if not row:
            return None
        if float(row["expires_at"]) <= now:
            return None
        if row["disabled_at"] is not None:  # a disabled admin's live sessions are dead
            return None
        return {"adminId": int(row["admin_id"]), "email": row["email"],
                "effectiveOrgId": (int(row["eff_org"])
                                   if row["eff_org"] is not None else None),
                "effectiveUserId": (int(row["eff_user"])
                                    if row["eff_user"] is not None else None),
                "impersonationStartedAt": row["imp_start"],
                "impersonationReason": row["imp_reason"],
                "expiresAt": float(row["expires_at"])}

    def delete_admin_session(self, token: str) -> bool:
        with self._tx() as c:
            cur = c.execute("DELETE FROM platform_admin_sessions WHERE token=?",
                            (hash_session_token(token),))
            return cur.rowcount > 0

    def purge_expired_admin_sessions(self) -> int:
        with self._tx() as c:
            cur = c.execute(
                "DELETE FROM platform_admin_sessions WHERE expires_at <= ?",
                (time.time(),))
            return cur.rowcount

    def delete_admin_sessions_for_admin(self, admin_id: int) -> int:
        """Kill every live session of one admin, returning how many were dropped. A
        credential change has to invalidate the cookies minted under the old one —
        otherwise a password reset prompted by a suspected theft leaves the thief
        logged in for the rest of the 12h TTL."""
        with self._tx() as c:
            cur = c.execute(
                "DELETE FROM platform_admin_sessions WHERE admin_id=?", (admin_id,))
            return cur.rowcount

    def set_admin_impersonation(self, token: str, *, effective_org_id: Optional[int],
                                effective_user_id: Optional[int],
                                reason: Optional[str],
                                now: Optional[float] = None) -> bool:
        """Write the impersonation principal onto the LIVE admin session (Phase 5c). The
        one sanctioned place a foreign org/user is set. Returns False if no such live
        session (nothing updated)."""
        now = time.time() if now is None else now
        with self._tx() as c:
            cur = c.execute(
                """UPDATE platform_admin_sessions
                   SET effective_org_id=?, effective_user_id=?,
                       impersonation_started_at=?, impersonation_reason=?
                   WHERE token=? AND expires_at > ?""",
                (effective_org_id, effective_user_id, now, reason,
                 hash_session_token(token), now))
            return cur.rowcount > 0

    def clear_admin_impersonation(self, token: str) -> bool:
        """End impersonation: null the effective principal on the admin session."""
        with self._tx() as c:
            cur = c.execute(
                """UPDATE platform_admin_sessions
                   SET effective_org_id=NULL, effective_user_id=NULL,
                       impersonation_started_at=NULL, impersonation_reason=NULL
                   WHERE token=?""",
                (hash_session_token(token),))
            return cur.rowcount > 0

    # ----- v15 superadmin plane: DB-backed login throttle -----
    def admin_login_is_locked(self, key: str, *,
                              now: Optional[float] = None) -> bool:
        now = time.time() if now is None else now
        row = self._conn.execute(
            "SELECT locked_until FROM admin_login_throttle WHERE key=?", (key,)).fetchone()
        if not row or row["locked_until"] is None:
            return False
        if float(row["locked_until"]) <= now:  # lock elapsed → clear, fresh window next fail
            with self._tx() as c:
                c.execute("DELETE FROM admin_login_throttle WHERE key=?", (key,))
            return False
        return True

    def admin_login_record_failure(self, key: str, *,
                                   now: Optional[float] = None) -> None:
        now = time.time() if now is None else now
        with self._tx() as c:
            row = c.execute(
                "SELECT fail_count, window_start FROM admin_login_throttle WHERE key=?",
                (key,)).fetchone()
            if row is None or (now - float(row["window_start"])) > ADMIN_LOGIN_WINDOW_SEC:
                fail_count, window_start = 1, now
            else:
                fail_count, window_start = int(row["fail_count"]) + 1, float(row["window_start"])
            locked_until = (now + ADMIN_LOGIN_LOCKOUT_SEC
                            if fail_count >= ADMIN_LOGIN_MAX_FAILURES else None)
            c.execute(
                """INSERT INTO admin_login_throttle(key, fail_count, window_start, locked_until)
                   VALUES(?,?,?,?)
                   ON CONFLICT(key) DO UPDATE SET
                     fail_count=excluded.fail_count, window_start=excluded.window_start,
                     locked_until=excluded.locked_until""",
                (key, fail_count, window_start, locked_until))

    def admin_login_reset(self, key: str) -> None:
        with self._tx() as c:
            c.execute("DELETE FROM admin_login_throttle WHERE key=?", (key,))

    # ----- v15 superadmin plane: TOTP anti-replay -----
    def claim_totp_counter(self, admin_id: int, counter: int, *,
                           now: Optional[float] = None) -> bool:
        """Atomically consume a TOTP step counter for an admin. Returns True if this is
        the FIRST use (login may proceed), False if it was already used (replay → reject).
        Uses INSERT-OR-IGNORE under a write lock so two concurrent replays can't both win.
        Prunes counters older than the acceptance window opportunistically."""
        now = time.time() if now is None else now
        horizon = now - (2 * _TOTP_WINDOW_STEPS_TTL_SEC)
        with self._tx_immediate() as c:
            c.execute("DELETE FROM admin_totp_used WHERE used_at < ?", (horizon,))
            cur = c.execute(
                "INSERT OR IGNORE INTO admin_totp_used(admin_id, counter, used_at) "
                "VALUES(?,?,?)", (admin_id, counter, now))
            return cur.rowcount > 0

    # ----- v15 superadmin plane: append-only hash-chained audit -----
    @staticmethod
    def _admin_audit_row_hash(prev_hash: str, core: dict[str, Any]) -> str:
        """row_hash = SHA-256(prev_hash || canonical-json(core)). Canonical = sorted keys,
        tight separators — so the chain is reproducible byte-for-byte on verify."""
        payload = (prev_hash + _ADMIN_AUDIT_SEP
                   + json.dumps(core, sort_keys=True, separators=(",", ":"),
                                ensure_ascii=False))
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()

    def append_admin_audit(self, *, acting_admin_id: Optional[int], action: str,
                           target_org_id: Optional[int] = None,
                           target_user_id: Optional[int] = None,
                           target_resource: Optional[str] = None,
                           ip: Optional[str] = None, user_agent: Optional[str] = None,
                           reason: Optional[str] = None,
                           impersonation_start: Optional[float] = None,
                           impersonation_end: Optional[float] = None,
                           at: Optional[float] = None) -> dict[str, Any]:
        """Append one immutable, hash-chained audit row (PRD §10). Chained to the prior
        row's row_hash so any later edit/delete breaks the chain (verify_admin_audit_chain
        finds the break). The `core` that is hashed excludes the id/row_hash themselves."""
        at = time.time() if at is None else at
        # _tx_immediate (write lock at statement one) so two concurrent appends can't
        # both read the same prev_hash and fork the chain (security review HIGH #2).
        with self._tx_immediate() as c:
            prev = c.execute(
                "SELECT row_hash FROM admin_audit_log ORDER BY id DESC LIMIT 1").fetchone()
            prev_hash = prev["row_hash"] if prev else ADMIN_AUDIT_GENESIS_HASH
            core = {"acting_admin_id": acting_admin_id, "action": action,
                    "target_org_id": target_org_id, "target_user_id": target_user_id,
                    "target_resource": target_resource, "at": at, "ip": ip,
                    "user_agent": user_agent, "reason": reason,
                    "impersonation_start": impersonation_start,
                    "impersonation_end": impersonation_end}
            row_hash = self._admin_audit_row_hash(prev_hash, core)
            cur = c.execute(
                """INSERT INTO admin_audit_log(prev_hash, row_hash, acting_admin_id, action,
                        target_org_id, target_user_id, target_resource, at, ip, user_agent,
                        reason, impersonation_start, impersonation_end)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (prev_hash, row_hash, acting_admin_id, action, target_org_id,
                 target_user_id, target_resource, at, ip, user_agent, reason,
                 impersonation_start, impersonation_end))
            return {"id": int(cur.lastrowid), "prevHash": prev_hash, "rowHash": row_hash}

    def verify_admin_audit_chain(self) -> dict[str, Any]:
        """Walk the audit chain oldest→newest, recomputing each row_hash. Returns
        {ok, count, firstBadId}: ok=False + the id of the first row whose stored hash or
        prev-link doesn't recompute (tamper evidence). An empty log is ok."""
        # Stream row-by-row (no fetchall) so verifying a long-lived log can't balloon
        # memory / stall the single-threaded server (security review MEDIUM #6).
        cur = self._conn.execute(
            """SELECT id, prev_hash, row_hash, acting_admin_id, action, target_org_id,
                      target_user_id, target_resource, at, ip, user_agent, reason,
                      impersonation_start, impersonation_end
               FROM admin_audit_log ORDER BY id ASC""")
        expected_prev = ADMIN_AUDIT_GENESIS_HASH
        count = 0
        for r in cur:
            count += 1
            core = {"acting_admin_id": r["acting_admin_id"], "action": r["action"],
                    "target_org_id": r["target_org_id"],
                    "target_user_id": r["target_user_id"],
                    "target_resource": r["target_resource"], "at": r["at"], "ip": r["ip"],
                    "user_agent": r["user_agent"], "reason": r["reason"],
                    "impersonation_start": r["impersonation_start"],
                    "impersonation_end": r["impersonation_end"]}
            recomputed = self._admin_audit_row_hash(r["prev_hash"], core)
            if r["prev_hash"] != expected_prev or r["row_hash"] != recomputed:
                bad_id = int(r["id"])
                cur.close()
                # count is a lower bound here (we stopped early) — report the break.
                return {"ok": False, "count": count, "firstBadId": bad_id}
            expected_prev = r["row_hash"]
        return {"ok": True, "count": count, "firstBadId": None}

    def list_admin_audit(self, *, limit: int = 200) -> list[dict[str, Any]]:
        """Recent audit rows, newest first, for the admin console."""
        limit = max(1, min(int(limit), 1000))
        rows = self._conn.execute(
            "SELECT * FROM admin_audit_log ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        return [dict(r) for r in rows]

    # ----- v7 organizations -----
    def create_organization(self, *, name: str, logo: Optional[str] = None,
                            description: Optional[str] = None,
                            created_by_user_id: Optional[int] = None) -> int:
        name = (name or "").strip()
        if not name:
            raise ValueError("organization name is required")
        now = time.time()
        with self._tx() as c:
            cur = c.execute(
                """INSERT INTO organizations(name, logo, description, created_by_user_id,
                                             created_at, updated_at)
                   VALUES(?,?,?,?,?,?)""",
                (name, logo, description, created_by_user_id, now, now))
            return int(cur.lastrowid)

    def get_organization(self, org_id: int) -> Optional[dict[str, Any]]:
        row = self._conn.execute(
            "SELECT * FROM organizations WHERE id=?", (org_id,)).fetchone()
        return dict(row) if row else None

    def list_organizations(self) -> list[dict[str, Any]]:
        """Every org, with a member count — the superadmin cross-org index (Phase 5d).
        Cross-tenant BY DESIGN: only ever reachable behind the platform-admin gate."""
        rows = self._conn.execute(
            """SELECT o.id AS id, o.name AS name, o.logo AS logo,
                      o.description AS description, o.created_at AS created_at,
                      (SELECT COUNT(*) FROM users u WHERE u.org_id = o.id) AS member_count
               FROM organizations o ORDER BY o.name""").fetchall()
        return [dict(r) for r in rows]

    def update_organization(self, org_id: int, *, name: Optional[str] = None,
                            logo: Optional[str] = None,
                            description: Optional[str] = None) -> Optional[dict[str, Any]]:
        """Sparse update of the company profile. `name` cannot be blanked."""
        if name is not None:
            name = name.strip()
            if not name:
                raise ValueError("organization name is required")
        with self._tx() as c:
            c.execute(
                """UPDATE organizations SET
                     name=COALESCE(?, name),
                     logo=COALESCE(?, logo),
                     description=COALESCE(?, description),
                     updated_at=? WHERE id=?""",
                (name, logo, description, time.time(), org_id))
        return self.get_organization(org_id)

    def create_org_with_owner(self, *, email: str, password_hash: str, token: str,
                              expires_at: float, company_name: str,
                              logo: Optional[str] = None,
                              description: Optional[str] = None) -> dict[str, Any]:
        """Signup: atomically create the org, its owner account, and a session.

        Either all three commit or none do — a failed signup leaves no orphan org.
        Raises ValueError on a blank company name, sqlite3.IntegrityError on a
        duplicate email (whole transaction rolls back)."""
        company_name = (company_name or "").strip()
        if not company_name:
            raise ValueError("company name is required")
        now = time.time()
        with self._tx() as c:
            cur = c.execute(
                """INSERT INTO organizations(name, logo, description, created_by_user_id,
                                             created_at, updated_at)
                   VALUES(?,?,?,?,?,?)""",
                (company_name, logo, description, None, now, now))
            org_id = int(cur.lastrowid)
            ucur = c.execute(
                """INSERT INTO users(email, password_hash, org_id, role, created_at, updated_at)
                   VALUES(?,?,?, 'owner', ?,?)""",
                (email, password_hash, org_id, now, now))
            user_id = int(ucur.lastrowid)
            c.execute("UPDATE organizations SET created_by_user_id=? WHERE id=?",
                      (user_id, org_id))
            c.execute(
                """INSERT INTO auth_sessions(token, user_id, created_at, expires_at)
                   VALUES(?,?,?,?)""",
                (hash_session_token(token), user_id, now, expires_at))
            return {"userId": user_id, "orgId": org_id, "role": "owner"}

    # ----- v7 org members (role lives on users.role) -----
    def create_user_in_org(self, *, org_id: int, email: str, password_hash: str,
                           role: str) -> int:
        """Direct-add a teammate with a password set by the inviter. Raises
        sqlite3.IntegrityError on a duplicate email."""
        now = time.time()
        with self._tx() as c:
            cur = c.execute(
                """INSERT INTO users(email, password_hash, org_id, role, created_at, updated_at)
                   VALUES(?,?,?,?,?,?)""",
                (email, password_hash, org_id, role, now, now))
            return int(cur.lastrowid)

    def list_org_users(self, org_id: int) -> list[dict[str, Any]]:
        rows = self._conn.execute(
            "SELECT id, email, role, created_at FROM users WHERE org_id=? ORDER BY created_at",
            (org_id,)).fetchall()
        return [{"id": r["id"], "email": r["email"], "role": r["role"],
                 "createdAt": r["created_at"]} for r in rows]

    def get_org_user(self, org_id: int, user_id: int) -> Optional[dict[str, Any]]:
        row = self._conn.execute(
            "SELECT id, email, role, created_at FROM users WHERE id=? AND org_id=?",
            (user_id, org_id)).fetchone()
        return dict(row) if row else None

    def update_user_role(self, org_id: int, user_id: int, role: str) -> bool:
        """Set a teammate's role (org-scoped). Returns False for an unknown user in
        this org. Role validity + authorization are enforced by the caller (rbac)."""
        with self._tx() as c:
            cur = c.execute(
                "UPDATE users SET role=?, updated_at=? WHERE id=? AND org_id=?",
                (role, time.time(), user_id, org_id))
            return cur.rowcount > 0

    def delete_user(self, org_id: int, user_id: int) -> bool:
        """Remove a teammate (org-scoped). Their auth_sessions cascade away."""
        with self._tx() as c:
            cur = c.execute("DELETE FROM users WHERE id=? AND org_id=?", (user_id, org_id))
            return cur.rowcount > 0

    def count_owners(self, org_id: int) -> int:
        return int(self._conn.execute(
            "SELECT COUNT(*) AS n FROM users WHERE org_id=? AND role='owner'",
            (org_id,)).fetchone()["n"])

    # ----- v7 invites (copy-link path; token hashed at rest) -----
    def create_invite(self, *, org_id: int, role: str, token: str, expires_at: float,
                      invited_by_user_id: Optional[int] = None,
                      email: Optional[str] = None) -> str:
        """Store a pending invite (the raw token lives only in the shared link).
        Returns the token_hash (the invite's stable identifier for list/revoke)."""
        th = hash_session_token(token)
        now = time.time()
        with self._tx() as c:
            c.execute(
                """INSERT INTO invites(token_hash, org_id, email, role, invited_by_user_id,
                                       created_at, expires_at)
                   VALUES(?,?,?,?,?,?,?)""",
                (th, org_id, email, role, invited_by_user_id, now, expires_at))
        return th

    def get_invite(self, token: str) -> Optional[dict[str, Any]]:
        """Look up an invite by its RAW token (for the public join landing). Returns
        org branding + intended role + a `valid` flag (pending and not expired)."""
        row = self._conn.execute(
            """SELECT i.org_id AS org_id, i.email AS email, i.role AS role,
                      i.expires_at AS expires_at, i.accepted_at AS accepted_at,
                      o.name AS org_name, o.logo AS org_logo
               FROM invites i JOIN organizations o ON o.id = i.org_id
               WHERE i.token_hash=?""",
            (hash_session_token(token),)).fetchone()
        if not row:
            return None
        valid = row["accepted_at"] is None and float(row["expires_at"]) > time.time()
        return {"orgId": int(row["org_id"]), "orgName": row["org_name"],
                "orgLogo": row["org_logo"], "email": row["email"], "role": row["role"],
                "expiresAt": row["expires_at"], "acceptedAt": row["accepted_at"],
                "valid": valid}

    def list_invites(self, org_id: int, *, include_accepted: bool = False) -> list[dict[str, Any]]:
        q = ("SELECT token_hash, email, role, invited_by_user_id, created_at, "
             "expires_at, accepted_at FROM invites WHERE org_id=?")
        args: list[Any] = [org_id]
        if not include_accepted:
            q += " AND accepted_at IS NULL"
        q += " ORDER BY created_at DESC"
        now = time.time()
        out = []
        for r in self._conn.execute(q, args).fetchall():
            accepted = r["accepted_at"] is not None
            expired = float(r["expires_at"]) <= now
            out.append({"id": r["token_hash"], "email": r["email"], "role": r["role"],
                        "invitedBy": r["invited_by_user_id"], "createdAt": r["created_at"],
                        "expiresAt": r["expires_at"], "acceptedAt": r["accepted_at"],
                        "status": "accepted" if accepted else "expired" if expired else "pending"})
        return out

    def revoke_invite(self, org_id: int, invite_id: str) -> bool:
        """Delete a pending invite by its id (token_hash), org-scoped."""
        with self._tx() as c:
            cur = c.execute(
                "DELETE FROM invites WHERE token_hash=? AND org_id=? AND accepted_at IS NULL",
                (invite_id, org_id))
            return cur.rowcount > 0

    def accept_invite(self, *, token: str, email: str, password_hash: str,
                      session_token: str, expires_at: float) -> dict[str, Any]:
        """Accept an invite: create the user (with the invited role) + a session and
        mark the invite used, atomically. Raises ValueError for an invalid/expired/used
        invite, sqlite3.IntegrityError on a duplicate email."""
        th = hash_session_token(token)
        now = time.time()
        with self._tx() as c:
            inv = c.execute(
                "SELECT org_id, role, expires_at, accepted_at FROM invites WHERE token_hash=?",
                (th,)).fetchone()
            if inv is None:
                raise ValueError("invalid invite")
            if inv["accepted_at"] is not None:
                raise ValueError("invite already used")
            if float(inv["expires_at"]) <= now:
                raise ValueError("invite expired")
            org_id = int(inv["org_id"])
            role = inv["role"]
            ucur = c.execute(
                """INSERT INTO users(email, password_hash, org_id, role, created_at, updated_at)
                   VALUES(?,?,?,?,?,?)""",
                (email, password_hash, org_id, role, now, now))
            user_id = int(ucur.lastrowid)
            c.execute(
                """INSERT INTO auth_sessions(token, user_id, created_at, expires_at)
                   VALUES(?,?,?,?)""",
                (hash_session_token(session_token), user_id, now, expires_at))
            c.execute("UPDATE invites SET accepted_at=? WHERE token_hash=?", (now, th))
            return {"userId": user_id, "orgId": org_id, "role": role}

    def purge_expired_invites(self) -> int:
        with self._tx() as c:
            cur = c.execute(
                "DELETE FROM invites WHERE accepted_at IS NULL AND expires_at <= ?",
                (time.time(),))
            return cur.rowcount

    # ----- v9 security audit log (insert-only; org-scoped reads) -----
    def record_audit(self, org_id: int, actor_user_id: Optional[int], action: str,
                     target: Optional[str] = None, detail: Optional[str] = None) -> None:
        """Append one immutable audit row stamped with an ISO timestamp.

        Defensive by contract: the audit trail must never take down the primary
        operation, so any failure is logged and swallowed (callers proceed)."""
        try:
            with self._tx() as c:
                c.execute(
                    """INSERT INTO audit_log(org_id, actor_user_id, action, target,
                                             detail, created_at)
                       VALUES(?,?,?,?,?,?)""",
                    (org_id, actor_user_id, action, target, detail, _now_iso()))
        except Exception:  # noqa: BLE001 — auditing must never crash the caller
            logger.exception(
                "audit_log insert failed (org=%s action=%s target=%s)",
                org_id, action, target)

    def audit_entries(self, org_id: int, limit: int = 100) -> list[dict[str, Any]]:
        """Org-scoped audit rows, newest first. id is the monotonic insertion order."""
        rows = self._conn.execute(
            """SELECT id, org_id, actor_user_id, action, target, detail, created_at
               FROM audit_log WHERE org_id=? ORDER BY id DESC LIMIT ?""",
            (org_id, limit)).fetchall()
        return [{"id": r["id"], "orgId": r["org_id"], "actorUserId": r["actor_user_id"],
                 "action": r["action"], "target": r["target"], "detail": r["detail"],
                 "createdAt": r["created_at"]} for r in rows]

    # ----- v28 opaque org-facing lead key -----

    def resolve_lead_token(self, org_id: Optional[int], token: str
                           ) -> Optional[dict[str, Any]]:
        """One org-facing lead key → the real `(campaign_id, platform, comment_id)`.

        This is the ONLY way an org-scoped write reaches a lead. The token is what
        `/api/leads` ships as `commentId`; the real comment id never leaves the
        bridge, because on reddit/youtube/telegram/x it IS a permalink to the
        comment (see `new_lead_token`).

        Scoped by ORG, not by campaign, and the two are not interchangeable. The
        token is unique table-wide, so the lookup does not need a campaign — and
        taking the campaign from the CALLER would let one be paired with another
        org's token to probe existence. So: resolve, then check the row's own
        `org_id`, and answer None for both "no such token" and "not yours". A caller
        that needs the campaign checked too compares it against the returned row,
        which is the row's own value rather than the request's.

        Returns None rather than raising: every caller turns it into the same 404
        the pre-v28 unknown-lead path returned, and for the same reason — a 403 here
        would confirm the lead exists and rebuild the cross-tenant existence oracle
        the reveal endpoint is careful not to be.
        """
        token = (token or "").strip()
        if not token:
            return None
        row = self._conn.execute(
            "SELECT campaign_id, org_id, platform, comment_id FROM matches "
            "WHERE lead_token=?", (token,)).fetchone()
        if row is None:
            return None
        # An org-scoped caller must own the row. `org_id is None` is the local-first
        # single-tenant case (no org on the session) — it is NOT a wildcard for a
        # multi-tenant caller, which always has one.
        if org_id is not None and row["org_id"] != org_id:
            return None
        return {"campaignId": row["campaign_id"], "platform": row["platform"],
                "commentId": row["comment_id"]}

    def ensure_lead_token(self, campaign_id: str, platform: str,
                          comment_id: str) -> str:
        """The opaque org-facing key for one lead, minting it if the row has none.

        Read paths call this instead of reading `lead_token` with a fallback, and the
        difference is the whole point: every fallback that can be DERIVED from the row
        is the comment id or something that contains it, which is what an org-facing
        key must never be. Fail closed by minting, never open by deriving.

        A row without a token is not hypothetical. The v28 backfill covers everything
        present at upgrade time, but a worker still running a pre-v28 binary inserts
        into the migrated DB without the column and writes NULL. That box is doing
        nothing wrong; it simply predates the key. So this heals the row on first read
        rather than treating a mixed-version fleet as a corruption.

        Idempotent, and safe to call on every row of every lead page: it writes only
        when the column is NULL/blank, and the UNIQUE index makes a double-mint a
        loud error rather than a silent duplicate.
        """
        row = self._conn.execute(
            "SELECT lead_token FROM matches "
            "WHERE campaign_id=? AND platform=? AND comment_id=?",
            (campaign_id, platform, comment_id)).fetchone()
        existing = (row["lead_token"] or "").strip() if row else ""
        if existing:
            return existing
        token = new_lead_token()
        with self._tx() as c:
            c.execute(
                "UPDATE matches SET lead_token=? "
                "WHERE campaign_id=? AND platform=? AND comment_id=? "
                "  AND (lead_token IS NULL OR lead_token='')",
                (token, campaign_id, platform, comment_id))
        # Re-read rather than returning `token`: under a concurrent writer the UPDATE
        # above may have matched nothing because someone else minted first, and the
        # caller must get the token that is actually STORED — handing back a token no
        # row carries would produce a lead the panel can see and can never write to.
        row = self._conn.execute(
            "SELECT lead_token FROM matches "
            "WHERE campaign_id=? AND platform=? AND comment_id=?",
            (campaign_id, platform, comment_id)).fetchone()
        return (row["lead_token"] if row and row["lead_token"] else token)

    def lead_token_for(self, campaign_id: str, platform: str,
                       comment_id: str) -> Optional[str]:
        """The opaque key for one lead, by its real composite key. The inverse of
        `resolve_lead_token`, and server-side only — used to echo a token back on a
        payload built from a row the bridge already resolved."""
        row = self._conn.execute(
            "SELECT lead_token FROM matches "
            "WHERE campaign_id=? AND platform=? AND comment_id=?",
            (campaign_id, platform, comment_id)).fetchone()
        return row["lead_token"] if row else None

    # ----- v27 reveal metering (reads of the same insert-only audit_log) -----
    @staticmethod
    def reveal_audit_detail(campaign_id: str, platform: str, result: str) -> str:
        """The `detail` blob for one `reveal_lead` audit row.

        The WRITER and the meter's READER are the same line of code on purpose. The
        outcome lives inside a JSON TEXT column, so the count below has to match on
        it with LIKE; deriving the needle from this very `json.dumps` call is what
        stops a formatting change here (separators, key order) from silently turning
        the meter into a no-op that lets every org reveal without limit.
        """
        return json.dumps({"campaignId": campaign_id, "platform": platform,
                           "result": result})

    @staticmethod
    def _revealed_needle() -> str:
        """The LIKE pattern matching a `revealed` outcome inside a detail blob.

        `json.dumps({...})[1:-1]` is the encoded `"result": "revealed"` pair with the
        braces stripped, so it is literally a substring of every row this method must
        count. Safe against a forged campaign id: JSON escapes an embedded quote as
        `\\"`, so an attacker-controlled string can never produce the UNESCAPED quote
        this needle starts with, and only the real key position can match.
        """
        return "%" + json.dumps({"result": REVEAL_RESULT_REVEALED})[1:-1] + "%"

    def count_reveals_this_period(self, org_id: int, since: float) -> int:
        """DISTINCT leads this org has revealed since the period anchor.

        DISTINCT on `target` (the composite lead uid) — NEVER the number of calls.
        Revealed data is session-local and never cached client-side (that is what
        keeps "anonymized by default" from decaying into "anonymized until first
        viewed"), so a drawer re-reveals every time it is reopened. Metering calls
        would burn a Free org's ten-lead allowance on ONE lead opened ten times.

        Only `revealed` rows count: a denial, a 404 and a refusal at the cap are all
        audited too, and none of them handed out an identity.

        `created_at` is ISO-8601 UTC TEXT (see `_now_iso`), so the period anchor is
        formatted the same way and compared lexicographically — which is exactly
        chronological for a fixed-offset ISO string.
        """
        row = self._conn.execute(
            """SELECT COUNT(DISTINCT target) FROM audit_log
                WHERE org_id=? AND action=? AND created_at>=? AND detail LIKE ?""",
            (org_id, REVEAL_ACTION, _iso_from_epoch(since),
             self._revealed_needle())).fetchone()
        return int(row[0]) if row else 0

    def lead_revealed_this_period(self, org_id: int, target: str,
                                  since: float) -> bool:
        """Has THIS lead already been revealed in the current period?

        The free-re-reveal check, and the reason the cap counts distinct leads at all:
        a lead whose identity this org has already been given costs nothing to hand
        over again — the disclosure already happened, and refusing the second look
        would only punish an operator for closing a drawer. Same predicate as
        `count_reveals_this_period`, narrowed to one target, so the two can never
        disagree about what "already revealed" means.
        """
        row = self._conn.execute(
            """SELECT 1 FROM audit_log
                WHERE org_id=? AND action=? AND created_at>=? AND target=?
                  AND detail LIKE ? LIMIT 1""",
            (org_id, REVEAL_ACTION, _iso_from_epoch(since), target,
             self._revealed_needle())).fetchone()
        return row is not None

    # ----- v10 run activity feed -----
    def emit_run_event(self, run_id: str, seq: int, phase: str, level: str,
                       message: str, *, campaign_id: Optional[str] = None,
                       session_id: Optional[str] = None,
                       detail: Optional[str] = None,
                       org_id: Optional[int] = None) -> None:
        """Append one immutable run-activity row (the panel's live feed). `seq` is the
        emitting session's monotonic ordinal; the table's autoincrement `id` is the
        cross-session cursor the panel pages on. org_id falls back to the campaign's
        owner. Defensive by contract: the activity trail must never take down a run,
        so any failure is logged and swallowed (matching record_audit)."""
        if org_id is None:
            org_id = self.org_for_campaign(campaign_id)
        try:
            with self._tx() as c:
                c.execute(
                    """INSERT INTO run_events(run_id, org_id, campaign_id, session_id,
                                              seq, phase, level, message, detail, created_at)
                       VALUES(?,?,?,?,?,?,?,?,?,?)""",
                    (run_id, org_id, campaign_id, session_id, seq, phase, level,
                     message, detail, time.time()))
        except Exception:  # noqa: BLE001 — the activity feed must never crash the run
            logger.exception("run_events insert failed (run=%s seq=%s)", run_id, seq)

    def sync_run_events(self, run_id: str, events: list, *,
                        org_id: Optional[int], campaign_id: Optional[str]) -> int:
        """Persist run_events a DISTRIBUTED worker shipped for its live job (the fleet
        run's activity feed — the worker emits into its OWN local store, so the cloud
        never sees them unless synced). `org_id`/`campaign_id` are FORCED from the job
        (BOLA) — never trusted from the payload, exactly like the lead sync. Idempotent
        on (run_id, session_id, seq) so a heartbeat retry can't double-list a line.
        Best-effort: a malformed row is skipped, a failure never fails the heartbeat.
        Returns the number of rows actually inserted."""
        if not events or not isinstance(events, list):
            return 0
        rows = events[:MAX_RUN_EVENTS_SYNC]
        inserted = 0
        try:
            with self._tx() as c:
                for ev in rows:
                    if not isinstance(ev, dict):
                        continue
                    seq = int(_lead_float(ev.get("seq"), 0) or 0)
                    phase = _lead_str(ev.get("phase")) or "info"
                    level = _lead_str(ev.get("level")) or "info"
                    message = _lead_str(ev.get("message")) or ""
                    detail = _lead_str(ev.get("detail"))
                    session_id = _lead_str(ev.get("sessionId") or ev.get("session_id"))
                    created_at = _lead_float(
                        ev.get("createdAt") or ev.get("created_at"), None) or time.time()
                    cur = c.execute(
                        """INSERT INTO run_events(run_id, org_id, campaign_id, session_id,
                                                  seq, phase, level, message, detail,
                                                  created_at)
                           SELECT ?,?,?,?,?,?,?,?,?,?
                            WHERE NOT EXISTS (
                                SELECT 1 FROM run_events
                                 WHERE run_id=? AND session_id IS ? AND seq=?)""",
                        (run_id, org_id, campaign_id, session_id, seq, phase, level,
                         message, detail, created_at, run_id, session_id, seq))
                    inserted += cur.rowcount
        except Exception:  # noqa: BLE001 — activity sync must never fail the heartbeat
            logger.exception("run_events sync failed (run=%s)", run_id)
        return inserted

    def fetch_run_events(self, run_id: str, after_id: int = 0,
                         org_id: Optional[int] = None,
                         limit: int = 500) -> list[dict[str, Any]]:
        """Run-activity rows after the cursor, oldest-first (insertion order). The
        cursor is the global autoincrement `id`, which is strictly increasing across
        ALL of a run's sessions (a batch run spans several) and never reused after a
        prune — so a per-session `seq` reset can't make the panel miss or replay rows.
        Scoped to `org_id` when given (the endpoint passes the caller's org).

        Each row carries the originating session's `platform` (multi-platform plan
        C7) via a LEFT JOIN on `sessions` — join-only, no schema change. It is null
        when the session row is missing/pruned. Org scoping stays on the event's own
        `re.org_id` (never the joined session's) so a pruned session can't widen scope.
        """
        q = ("SELECT re.id, re.run_id, re.campaign_id, re.session_id, re.seq, "
             "re.phase, re.level, re.message, re.detail, re.created_at, "
             "s.platform AS platform "
             "FROM run_events re "
             "LEFT JOIN sessions s ON s.session_id = re.session_id "
             "WHERE re.run_id=? AND re.id>?")
        args: list[Any] = [run_id, after_id]
        if org_id is not None:
            q += " AND re.org_id=?"
            args.append(org_id)
        q += " ORDER BY re.id ASC LIMIT ?"
        args.append(limit)
        rows = self._conn.execute(q, args).fetchall()
        return [{"id": r["id"], "seq": r["seq"], "campaignId": r["campaign_id"],
                 "sessionId": r["session_id"], "phase": r["phase"], "level": r["level"],
                 "message": r["message"], "detail": r["detail"],
                 "createdAt": r["created_at"], "platform": r["platform"]} for r in rows]

    def sessions_for_run(self, run_id: str,
                         org_id: Optional[int] = None) -> list[dict[str, Any]]:
        """All session rows belonging to a run (a batch run spans several), newest
        first. The activity endpoint aggregates their live counters + finished state.
        Org-scoped when given."""
        q = "SELECT * FROM sessions WHERE run_id=?"
        args: list[Any] = [run_id]
        if org_id is not None:
            q += " AND org_id=?"
            args.append(org_id)
        q += " ORDER BY started_at DESC"
        return [dict(r) for r in self._conn.execute(q, args).fetchall()]

    # ----- v27: per-run lead numbers for a LIST of runs (the superadmin picker) -----
    # The activity endpoint answers "how many leads did THIS run find/deliver" for one
    # run at a time, out of two sources that disagree on purpose (see server.py's
    # _aggregate_run_progress). The picker needs the same pair for up to fifty runs at
    # once, so both are answered here in ONE query each rather than as a per-run N+1.

    def match_event_details_by_run(
            self, org_id: int, run_ids: Sequence[str]) -> dict[str, list[str]]:
        """run_id -> the raw `detail` blobs of that run's per-match events.

        The narrow slice `phase='comments' AND level='success'` is the only one the
        lead estimate reads, and the blobs come back UNDECODED: this is a store read,
        and the dedupe rule that turns them into a number lives in exactly one place
        in server.py. Returning parsed numbers here would be a second copy of it.

        `org_id` is a BOLA guard on the event's OWN column, never a joined one. Empty
        `run_ids` short-circuits — SQLite would otherwise see `IN ()`, a syntax error.
        """
        ids = [r for r in run_ids if r]
        if not ids:
            return {}
        out: dict[str, list[str]] = {}
        # Chunked so a long picker page can never blow SQLITE_MAX_VARIABLE_NUMBER
        # (999 on older builds) — the org id costs one of the slots in each chunk.
        for start in range(0, len(ids), 500):
            chunk = ids[start:start + 500]
            placeholders = ",".join("?" * len(chunk))
            rows = self._conn.execute(
                f"""SELECT run_id, detail FROM run_events
                     WHERE org_id=? AND run_id IN ({placeholders})
                       AND phase='comments' AND level='success'""",
                [org_id, *chunk]).fetchall()
            for r in rows:
                out.setdefault(r["run_id"], []).append(r["detail"])
        return out

    def run_event_runs(self, org_id: int, limit: int = 50) -> list[dict[str, Any]]:
        """The org's runs as the EVENT feed knows them, newest activity first.

        Sessions are the durable record of a run — but a fleet run mirrors its sessions
        into the cloud at ACK, and a job that dead-lettered never acked. Its events are
        here (they land on the ~45s heartbeat) while it has no session row at all, so a
        picker built only from `sessions` cannot list the very run whose log is the only
        surviving account of what it did.

        `campaignId` is MAX() rather than a group key because a batch run spans several
        campaigns and aggregates skip NULLs, so this prefers a real id over the NULL an
        unregistered campaign's events carry. `sessions` counts DISTINCT session ids for
        the same reason the run folder does: one run id spans many.
        """
        rows = self._conn.execute(
            """SELECT run_id AS run_id,
                      MAX(campaign_id) AS campaign_id,
                      MIN(created_at) AS first_at,
                      MAX(created_at) AS last_at,
                      COUNT(DISTINCT session_id) AS sessions
                 FROM run_events
                WHERE org_id=?
                GROUP BY run_id
                ORDER BY last_at DESC
                LIMIT ?""",
            (org_id, limit)).fetchall()
        return [{"runId": r["run_id"], "campaignId": r["campaign_id"],
                 "firstAt": r["first_at"], "lastAt": r["last_at"],
                 "sessions": int(r["sessions"] or 0)} for r in rows]

    def lead_counts_by_run(self, org_id: int) -> dict[str, int]:
        """run_id -> how many `matches` rows the org actually HOLDS for that run.

        `leadsDelivered`, in bulk. Counts real rows joined through `sessions` exactly
        the way `matches_for_run` does — deliberately NOT `sessions.matches`, which is
        a per-session progress counter that overshoots (it increments once per POST
        after a whole comment batch) and is therefore not a row count at all.

        A run whose job dead-lettered never acked, so its harvest is still in the
        WORKER's sqlite and it simply has no rows here: absent from this map, which
        the caller reads as the 0 it truly is.
        """
        rows = self._conn.execute(
            """SELECT s.run_id AS run_id, COUNT(*) AS n
                 FROM matches m JOIN sessions s ON m.session_id = s.session_id
                WHERE s.org_id=? AND s.run_id IS NOT NULL
                GROUP BY s.run_id""",
            (org_id,)).fetchall()
        return {r["run_id"]: int(r["n"]) for r in rows}

    def run_events_open_flags(self, run_id: str,
                              org_id: Optional[int] = None) -> list[dict[str, Any]]:
        """Open health flags raised by this run's sessions (the activity drawer's
        warning/halt banner). Org-scoped when given."""
        q = ("SELECT h.* FROM health_flags h WHERE h.resolved_at IS NULL "
             "AND h.session_id IN (SELECT session_id FROM sessions WHERE run_id=?)")
        args: list[Any] = [run_id]
        if org_id is not None:
            q += " AND h.org_id=?"
            args.append(org_id)
        q += " ORDER BY h.created_at DESC"
        return [dict(r) for r in self._conn.execute(q, args).fetchall()]

    def action_counts_for_run(self, run_id: str,
                              org_id: Optional[int] = None) -> dict[str, int]:
        """Successful engagement counts (like/follow) across a run's sessions, for the
        activity drawer's metrics row (likes/follows live in `actions`, not sessions)."""
        q = ("SELECT a.action_type AS t, COUNT(*) AS n FROM actions a "
             "WHERE a.succeeded=1 AND a.session_id IN "
             "(SELECT session_id FROM sessions WHERE run_id=?")
        args: list[Any] = [run_id]
        if org_id is not None:
            q += " AND org_id=?"
            args.append(org_id)
        q += ") GROUP BY a.action_type"
        return {r["t"]: r["n"] for r in self._conn.execute(q, args).fetchall()}

    def prune_run_events(self, ttl_seconds: float = RUN_EVENTS_TTL_SECONDS,
                         keep_runs: int = RUN_EVENTS_KEEP_RUNS) -> None:
        """Opportunistic retention sweep: drop events older than the TTL and any
        belonging to runs beyond the most recent `keep_runs`. Defensive — pruning
        must never crash the caller (called at launch / on run completion)."""
        try:
            with self._tx() as c:
                c.execute("DELETE FROM run_events WHERE created_at < ?",
                          (time.time() - ttl_seconds,))
                c.execute(
                    """DELETE FROM run_events WHERE run_id NOT IN (
                           SELECT run_id FROM run_events
                           GROUP BY run_id ORDER BY MAX(id) DESC LIMIT ?)""",
                    (keep_runs,))
        except Exception:  # noqa: BLE001 — retention must never crash the caller
            logger.exception("run_events prune failed")
