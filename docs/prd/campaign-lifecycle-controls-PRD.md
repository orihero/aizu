# Campaign Lifecycle Controls — PRD & Build Plan

**Status:** Approved 2026-06-29 (ultracode workflow: 5-explorer map → design → 3-lens adversarial review → finalize)
**Scope confirmed by user:** all four controls, phased; scheduled runs default to campaign `goalTarget`; collision = skip + visible log; archive = non-destructive hide.

## Goal

Add four operator controls to campaigns: **Schedule** (recurring runs), **Stop** (exists), **Pause** (two senses), **Archive** (reversible hide).

## Core model decisions

- `{live, paused, draft, ended}` status enum is **preserved** (do NOT add archive/schedule as statuses).
- **Archive** = `archived_at REAL` timestamp dimension on `campaign_meta`. Non-destructive, reversible.
- **Schedule-suspend** = `schedule_enabled BOOLEAN`, decoupled from engagement status.
- **Pause-run** = cooperative pause-file the engine polls between reels; resumable.
- **Schedule** = fixed-cadence columns (`kind`/`dow`/`hour`/`minute`/`tz`) + single daemon thread. **No cron parser** (dropped v1; UI only offers Daily/Weekdays/Weekly).
- Scheduler injected via `serve(..., schedule_manager=None)` like `run_manager` — never construct in-serve (would spawn a DB-touching daemon in every server test).
- One shared `_pause_path(run_id)` helper (run_id only, NOT scope-suffixed). Engine self-deletes pause file on any natural exit; `_monitor` clears `paused` on child exit; `ScheduleManager.start()` sweeps orphans.
- Clear paths (unarchive, clear-schedule, resume) use **dedicated UPDATE** statements, never the COALESCE-merge upsert (which cannot null a column).
- Centralized runnable predicate (`status='live' AND archived_at IS NULL`) shared by scheduler SQL, `cli._live_campaigns`, and React `selectors.isRunnable`.
- `paused_reason` precedence: `'user'` pause cleared by operator resume; `'auto'` (system) NOT silently cleared.
- `RunSpec.launch_source ∈ {manual, scheduled}` threaded into run history.

## Data model (schema v11 → v12)

New `campaign_meta` columns (all additive, self-healing `_add_column_if_missing`):
`archived_at REAL`, `paused_reason TEXT`, `schedule_enabled INTEGER NOT NULL DEFAULT 0`,
`schedule_kind TEXT NOT NULL DEFAULT ''`, `schedule_dow INTEGER`, `schedule_hour INTEGER`,
`schedule_minute INTEGER`, `schedule_tz TEXT NOT NULL DEFAULT 'Asia/Tashkent'`,
`next_run_at REAL`, `last_scheduled_run_at REAL`, `schedule_target_leads INTEGER`,
`schedule_duration_minutes INTEGER`.
Indexes: `idx_campaign_meta_next_run` (partial, next_run_at NOT NULL), `idx_campaign_meta_archived`.

New store methods (dedicated UPDATEs): `set_campaign_archived`, `set_campaign_paused`,
`set_campaign_schedule`, `clear_campaign_schedule`, `due_scheduled_campaigns(now_ts)`.

## Endpoints

- `POST /api/run/pause` / `/api/run/resume` — RBAC `run_campaigns`, org-scoped, idempotent (re-pause = 200), 409 only when nothing active.
- `POST /api/campaign/archive` — body `{campaignId, archived}`, RBAC `edit_campaigns`, ownership. Archive-while-live stops the active run + transitions `live→paused` atomically.
- `POST /api/campaign/schedule` — body `{campaignId, enabled, kind?, dow?, hour?, minute?, tz?, targetLeads?, durationMinutes?}`, RBAC `edit_campaigns`. Computes initial `next_run_at`; `enabled=false` clears.
- `POST /api/campaign` (existing) — Play/Pause toggle sets `paused_reason='user'`; resume only clears when reason ∈ {user}.

## UI (admin-panel)

`domain.ts` + Zod `panelState.ts` + `endpoints.ts` extended same-PR per phase.
Selectors: `isArchived`, `isScheduled`, `selectIsRunPaused`, `isRunnable && !isArchived`.
CampaignsPage: 6th "archived" filter chip. CampaignCard: schedule badge, Archive/Unarchive + Schedule… menu, Pause/Resume + Stop on active run. RunDrawer: Pause/Resume toggle. New `ScheduleDialog`. Repository + `useWriteMutations` for all four. `roles.ts`: archive/schedule reuse `edit_campaigns`, pause/resume reuse `run_campaigns`.

## Phased rollout (each phase independently shippable + tested)

- **Phase 1 — Archive + Pause-schedule** (near-zero engine work): schema v12, store methods, `/api/campaign/archive`, `cli._live_campaigns` archived filter, paused_reason precedence, UI archived chip + card menu.
- **Phase 2 — Pause/Resume a live run**: `_pause_path` helper, `_check_pause()` cooperative loop, `/api/run/pause|resume`, RunDrawer/card controls.
- **Phase 3 — Schedule persistence**: `set/clear_campaign_schedule`, fixed-cadence `next_fire`, `/api/campaign/schedule`, `ScheduleDialog`.
- **Phase 4 — Scheduler daemon**: `scheduler.py` ScheduleManager (60s tick, idempotent advance-before-launch, skip+log on contention), injected via `serve()`, `launch_source` tagging.

## Key risks

- Single global run lock → scheduled & manual runs serialize; collision drops the occurrence (skip + visible health_flag).
- In-memory run state lost on restart → mitigated by self-delete + monitor reconcile + startup sweep.
- Cooperative pause checkpoints between reels (bounded seconds), not mid-reel.
- next_fire must agree with `TZ_SQL_SHIFT +5h` / daytime guard window — pinned + DST-free test.

## Open questions (resolved)

1. Custom cron — deferred (fixed cadence ships v1).
2. Archive retention — non-destructive hide, no purge.
3. Skip notification — visible in-app run_event only, no push v1.
4. Manual-run-satisfies-schedule — current plan emits skip each time; revisit if alarm fatigue.
