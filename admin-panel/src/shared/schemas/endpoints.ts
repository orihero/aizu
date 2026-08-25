import { z } from 'zod';
import {
  EMPTY_HEALTH,
  alertSchema,
  billingSchema,
  campaignSchema,
  dashboardSchema,
  healthSchema,
  integrationSchema,
  inviteSchema,
  matchSchema,
  panelConfigSchema,
  reportsSchema,
  runBlockSchema,
  sessionSchema,
  teamMemberSchema,
} from './panelState';

/**
 * Per-page response schemas for the org-wide endpoints that supersede the monolithic
 * /api/state (one per Pulse page). Composed entirely from the existing sub-schemas in
 * panelState.ts — the single source of truth for each record shape — so the split adds
 * no duplicated field definitions and can't drift from /api/state.
 *
 * Backend contract: engine/aizu/panel_org.py. Keep the two in sync.
 */

const EMPTY_RUN = { active: null, recent: [] };

/** GET /api/dashboard → org-wide bento + ticker matches + health/alerts + run block. */
export const dashboardPayloadSchema = z.object({
  DASHBOARD: dashboardSchema,
  MATCHES: z.array(matchSchema),
  HEALTH: healthSchema.catch(EMPTY_HEALTH),
  ALERTS: z.array(alertSchema).catch([]),
  RUN: runBlockSchema.catch(EMPTY_RUN),
  CONFIG: panelConfigSchema,
});

/** GET /api/campaigns → every campaign card + pooled org-wide sessions + run block. */
export const campaignsPayloadSchema = z.object({
  CAMPAIGNS: z.array(campaignSchema),
  SESSIONS: z.array(sessionSchema).catch([]),
  RUN: runBlockSchema.catch(EMPTY_RUN),
});

/** Org-wide lead stat tiles (computed over the UNfiltered set, stable as you filter). */
export const leadStatsSchema = z.object({
  total: z.number().catch(0),
  counts: z.record(z.string(), z.number()).catch({}),
  won: z.number().catch(0),
  escalated: z.number().catch(0),
  labeled: z.number().catch(0),
});

/** Inner data of GET /api/leads (the server wraps this in the {ok,data,error} envelope). */
export const leadsPayloadSchema = z.object({
  items: z.array(matchSchema),
  total: z.number(),
  page: z.number(),
  pageSize: z.number(),
  stats: leadStatsSchema,
  platforms: z.array(z.string()).catch([]),
  // Org campaigns ({id,name}) for the leads-page campaign filter — shipped here so
  // a leads-only member (who can't read /api/campaigns) still gets names.
  campaigns: z.array(z.object({ id: z.string(), name: z.string() })).catch([]),
  CONFIG: panelConfigSchema,
});

export const leadsResponseSchema = z.object({
  ok: z.boolean(),
  data: leadsPayloadSchema.nullable(),
  error: z.string().nullable(),
});

/** POST /api/billing/checkout → the Polar hosted-checkout URL to redirect to. */
export const checkoutSessionResponseSchema = z.object({
  ok: z.boolean(),
  data: z.object({ checkoutUrl: z.string() }).nullable(),
  error: z.string().nullable(),
});

/** POST /api/billing/portal → the Polar customer-portal URL, or hasAccount:false
 * for a Free org with no Polar customer yet (degrades cleanly, not a 500). */
export const billingPortalResponseSchema = z.object({
  ok: z.boolean(),
  data: z.object({ portalUrl: z.string(), hasAccount: z.boolean() }).nullable(),
  error: z.string().nullable(),
});

/** POST /api/run → run accepted. A fleet-routed live run carries `runId`/`backend`
 * (so the drawer can poll the live feed); in-process runs leave them absent. Tolerant:
 * an in-process 202 with neither field still parses (both default to null). */
export const runStartResponseSchema = z.object({
  ok: z.boolean(),
  data: z.object({
    runId: z.string().nullable().catch(null),
    backend: z.string().nullable().catch(null),
    // v27 plan bounds the server resolved for THIS run (A9). Optional: a pre-v27 bridge
    // omits them entirely. `targetLeads` is the CLAMPED target the run actually started
    // with — the run drawer shows it as the denominator for an IN-PROCESS run, whose
    // target reaches the panel nowhere else (only a fleet job persists it, in its spec).
    // A SOFT bound (E.6 — a run can overshoot), so never render it as a promise.
    // `.default(null)` rather than `.optional()`: an absent field must land as an
    // explicit "the bridge did not report this", not as `undefined` — the repo's
    // unknown-is-never-zero invariant, and the shape `RunStartResult` declares.
    targetLeads: z.number().nullable().default(null).catch(null),
    maxRunLeads: z.number().nullable().default(null).catch(null),
    leadsRemaining: z.number().nullable().default(null).catch(null),
  }).nullable().catch(null),
  error: z.string().nullable(),
});

/**
 * GET /api/agent/readiness → whether the Instagram warmed-browser agent (CDP + logged-in
 * session) can run a live campaign right now. A raw keys dict, not the {ok,data,error}
 * envelope — same shape as the other per-page GETs above. `ready` is server-computed
 * (cdp==='ok' && instagram==='logged_in'); the panel never re-derives it.
 * Backend contract: engine/aizu/server.py (agent readiness gate).
 */
export const agentReadinessSchema = z.object({
  ready: z.boolean(),
  cdp: z.enum(['ok', 'unreachable']),
  instagram: z.enum(['logged_in', 'logged_out', 'unknown']),
  checkedAt: z.number(),
  detail: z.string().nullable(),
  cdpUrl: z.string(),
  /** Which backend the server measured. `distributed` means live runs execute on
   * worker PCs, so `cdp`/`instagram` describe fleet presence rather than a browser on
   * the server — read `detail` for the accurate sentence, and don't offer to launch a
   * login browser there is no browser for. Optional: a server predating the field
   * still parses. */
  backend: z.enum(['in_process', 'distributed']).optional(),
});

/** POST /api/agent/launch-login → attempted to spawn/attach Chrome and open a login
 * tab (owner/admin only — RBAC action `fix_agent`). Also a raw dict, not the write
 * envelope: `launched` is true iff a Chrome was spawned or a login tab was opened. */
export const launchAgentLoginResponseSchema = z.object({
  launched: z.boolean(),
  readiness: agentReadinessSchema,
});

/** The 500 shape POST /api/agent/launch-login returns when Chrome itself couldn't be
 * started (as opposed to a successful attempt that still leaves the agent unready). */
export const agentLaunchFailureSchema = z.object({
  error: z.string(),
  detail: z.string(),
});

/**
 * The 409 gate POST /api/run returns when the CDP/Instagram readiness check fails —
 * its own shape (not the {ok,data,error} write envelope every other run-control write
 * uses), carrying the full readiness snapshot so the caller can explain precisely what
 * is wrong without a second round-trip.
 */
export const agentNotReadyResponseSchema = z.object({
  error: z.literal('agent_not_ready'),
  detail: z.string(),
  readiness: agentReadinessSchema,
});

/** GET /api/reports → org-wide time series + per-campaign rollup + health. */
export const reportsPayloadSchema = z.object({
  REPORTS: reportsSchema,
  HEALTH: healthSchema.catch(EMPTY_HEALTH),
});

/** GET /api/settings → workspace config + team + invites + integrations + billing.
 * BILLING is role-pruned server-side (view_billing: owner/admin), so it is optional. */
export const settingsPayloadSchema = z.object({
  CONFIG: panelConfigSchema,
  TEAM: z.array(teamMemberSchema).catch([]),
  INVITES: z.array(inviteSchema).catch([]),
  INTEGRATIONS: z.array(integrationSchema).catch([]),
  BILLING: billingSchema.optional(),
});
