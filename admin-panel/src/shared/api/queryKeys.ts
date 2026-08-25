import type { LeadsQuery } from '@/shared/types/domain';

export const queryKeys = {
  // ---- per-page, org-wide endpoints ----
  /** GET /api/dashboard. */
  dashboard: ['dashboard'] as const,
  /** GET /api/campaigns (also the RUN-block owner). */
  campaigns: ['campaigns'] as const,
  /** GET /api/reports. */
  reports: ['reports'] as const,
  /** GET /api/settings. */
  settings: ['settings'] as const,
  /** Prefix matching every cached leads page — invalidate the whole list at once. */
  leads: ['leads'] as const,
  /** One leads page: the page/size + active filters/sort each get their own cache. */
  leadsPage: (query: LeadsQuery) =>
    ['leads', query.page, query.pageSize, query.q ?? '', query.status ?? '',
      query.platform ?? '', query.campaign ?? '', query.sort ?? 'capturedAt',
      query.dir ?? 'desc'] as const,

  /** One run's live activity feed (polled while the run is in flight). v27: this is
   * the org-facing PROGRESS poll — scalars only, `events` always empty. */
  runActivity: (runId: string) => ['run-activity', runId] as const,
  // NOTE: there is deliberately NO key for the lead reveal. POST /api/lead/reveal is an
  // imperative, audited call whose result is session-local and must never be persisted
  // — a cache entry surviving a drawer close would turn "anonymized by default" into
  // "anonymized until first viewed", and would silently defeat Section F.
  /** GET /api/agent/readiness — polled globally for the "agent not ready" banner. */
  agentReadiness: ['agent-readiness'] as const,
  /**
   * GET /api/agent/readiness?campaign=<id> — the SAME endpoint narrowed to one
   * campaign's platforms. A distinct key on purpose: the scoped answer can be
   * `ready:false` while the unscoped one is `ready:true` (a youtube-only fleet facing
   * an instagram campaign), so caching them together would let whichever polled last
   * win. `useWriteMutations` seeds only the unscoped key, which stays correct.
   */
  agentReadinessFor: (campaignId: string) => ['agent-readiness', campaignId] as const,

  // ---- superadmin plane (separate /api/admin/* branch) ----
  /** GET /api/admin/whoami — the admin session + impersonation state. */
  adminWhoami: ['admin', 'whoami'] as const,
  /** GET /api/admin/fleet — polled worker snapshot. */
  adminFleet: ['admin', 'fleet'] as const,
  /** GET /api/admin/worker-enrolment-tokens. */
  adminEnrolmentTokens: ['admin', 'enrolment-tokens'] as const,
  /** GET /api/admin/control-flags. */
  adminControlFlags: ['admin', 'control-flags'] as const,
  /** GET /api/admin/orgs — cross-org index. */
  adminOrgs: ['admin', 'orgs'] as const,
  /** One org's campaign list. */
  adminOrgCampaigns: (orgId: number) => ['admin', 'orgs', orgId, 'campaigns'] as const,
  /** One org's leads page (keyed by every filter/sort/page like the org leads list). */
  adminOrgLeads: (orgId: number, page: number, pageSize: number) =>
    ['admin', 'orgs', orgId, 'leads', page, pageSize] as const,
  /** One org's recent runs — the picker for the narrative feed below (v27). */
  adminOrgRuns: (orgId: number) => ['admin', 'orgs', orgId, 'runs'] as const,
  /**
   * One run's FULL narrative feed (v27). Keyed by run id only, NOT by the `after`
   * cursor: the feed is paged by appending each poll's events to what the console
   * already holds, so a cursor in the key would give every poll its own cache entry
   * and lose the accumulated history. Superadmin plane — this is the un-redacted feed.
   */
  adminRunActivity: (runId: string) => ['admin', 'run-activity', runId] as const,
  /** GET /api/admin/audit. */
  adminAudit: ['admin', 'audit'] as const,
  /** GET /api/admin/execution-backend — the platform-wide run routing switch. */
  adminExecutionBackend: ['admin', 'execution-backend'] as const,
  /** GET /api/admin/model-comparison — the LLM fan-out on/off switch. */
  adminModelComparison: ['admin', 'model-comparison'] as const,
  /** GET /api/admin/model-comparison/stats — per-model aggregates + recent calls. */
  adminModelComparisonStats: ['admin', 'model-comparison', 'stats'] as const,
};
