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
 * Backend contract: engine/reelradar/panel_org.py. Keep the two in sync.
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
  }).nullable().catch(null),
  error: z.string().nullable(),
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
