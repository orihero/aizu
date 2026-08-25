import { z } from 'zod';

/**
 * Boundary schemas for the SEPARATE superadmin plane (`/api/admin/*`).
 *
 * This is intentionally its own branch — the org-scoped PanelState schemas must
 * NOT leak in, and vice versa (PRD §10 BOLA rule: superadmin is a distinct auth
 * plane, never an org role). Requests are camelCase; responses are mostly
 * camelCase, except the audit log and the control-flag `update_required` field
 * which come straight off the DB as snake_case — those are normalised to camel
 * here so every consumer downstream sees one convention.
 *
 * Backend contract: engine/aizu/server.py (admin routes) + admin_auth.py.
 */

/** `{ok,data,error}` envelope; `data` is the payload on success, null on error. */
const adminEnvelope = <T extends z.ZodTypeAny>(data: T) =>
  z.object({ ok: z.boolean(), data: data.nullable(), error: z.string().nullable() });

// ---------------------------------------------------------------------------
// Auth: login + whoami
// ---------------------------------------------------------------------------

/** The signed-in admin as reported by whoami — carries the impersonation state. */
export const adminSessionSchema = z.object({
  id: z.number(),
  email: z.string(),
  impersonating: z.boolean(),
  effectiveOrgId: z.number().nullable(),
  effectiveUserId: z.number().nullable(),
  impersonationReason: z.string().nullable(),
});
export type AdminSession = z.infer<typeof adminSessionSchema>;

export const adminWhoamiResponseSchema = adminEnvelope(
  z.object({ admin: adminSessionSchema }),
);

/** login returns only {id,email}; the session's impersonation state starts empty. */
export const adminLoginResponseSchema = adminEnvelope(
  z.object({ admin: z.object({ id: z.number(), email: z.string() }) }),
);

// ---------------------------------------------------------------------------
// Fleet
// ---------------------------------------------------------------------------

export const WORKER_STATUSES = ['online', 'stale', 'offline'] as const;
export const workerStatusSchema = z.enum(WORKER_STATUSES).catch('offline');

/** One capability triple a worker declares: [orgId|null, platform, accountHandle]. */
export const capabilitySchema = z.tuple([
  z.number().nullable(),
  z.string(),
  z.string(),
]);

/** The job a worker is executing right now (null when idle). */
export const currentJobSchema = z.object({
  jobId: z.string(),
  campaignId: z.string().nullable(),
  platform: z.string().nullable(),
  status: z.string(),
  runId: z.string().nullable(),
  leaseExpiresAt: z.number().nullable(),
});
export type CurrentJob = z.infer<typeof currentJobSchema>;

/**
 * One non-passing row of a worker's launch preflight (ledger F9/F10, engine
 * `worker/preflight.py`). `title` and `remedy` deliberately never ride the wire — they are
 * UI copy this panel resolves client-side from the id (see `features/admin/preflightCopy`),
 * which halves the body and keeps operator-facing text under our control rather than a
 * worker's.
 *
 * SECURITY: `detail` is WORKER-AUTHORED text rendered in the superadmin console. Render it
 * as text, never as markup (the E1/E2/F18 family — an off-cloud caller's string reaching a
 * privileged surface).
 */
export const preflightFailureSchema = z.object({
  id: z.string(),
  // Same reasoning as `status` below, and the same failure mode `failed[]`'s own
  // `.catch([])` guards: without a fallback ONE row carrying a severity this panel has
  // not learned yet (a newer sidecar's) fails the row, which fails the array, which the
  // outer `.catch([])` swallows whole — every other failing check silently vanishes from
  // the console. Falls back to 'fatal', the reading that never under-reports.
  severity: z.enum(['fatal', 'warn']).catch('fatal'),
  // `failed[]` mixes "checked, it is broken" (fail) with "could not check at all"
  // (unknown), and those need different operator copy — the commonest red state of all is
  // Chrome being down, which marks every `login.*` row unknown. An older sidecar that
  // omits this degrades to 'fail', the reading that never under-reports a problem.
  status: z.enum(['fail', 'unknown', 'pass', 'skip']).catch('fail'),
  detail: z.string().nullable().catch(null),
});
export type PreflightFailure = z.infer<typeof preflightFailureSchema>;

/**
 * The compact preflight summary a box carries on register/heartbeat. `null` means the
 * server has never stored one (a pre-v23 sidecar, or a box whose report was dropped for
 * being malformed/oversized) — it is "unknown", NEVER "healthy".
 */
export const preflightSummarySchema = z
  .object({
    ok: z.boolean(),
    blocking: z.boolean(),
    enforced: z.boolean(),
    ranAt: z.number().nullable().catch(null),
    failed: z.array(preflightFailureSchema).catch([]),
  })
  .nullable()
  .catch(null);
export type PreflightSummary = z.infer<typeof preflightSummarySchema>;

export const fleetWorkerSchema = z.object({
  id: z.string(),
  orgId: z.number().nullable(),
  displayName: z.string().nullable(),
  host: z.string().nullable(),
  os: z.string().nullable(),
  agentVersion: z.string().nullable(),
  maxSessions: z.number(),
  currentSessions: z.number(),
  capabilities: z.array(capabilitySchema).catch([]),
  registeredAt: z.number(),
  lastHeartbeatAt: z.number().nullable(),
  lastSeenAgeSec: z.number().nullable(),
  status: workerStatusSchema,
  revokedAt: z.number().nullable(),
  // Enrichment: what the box is running now. `.catch(null)` keeps an older/absent
  // server payload valid (the field is additive).
  currentJob: currentJobSchema.nullable().catch(null),
  // v23 launch preflight. This key MUST be declared: z.object() STRIPS unknown keys, so
  // omitting it is not "the field passes through untouched" — it is the field being
  // silently discarded at the last hop after the worker, the server and the store all
  // carried it correctly (the B4 trap, which has shipped inert twice).
  preflight: preflightSummarySchema,
});
export type FleetWorker = z.infer<typeof fleetWorkerSchema>;

export const fleetResponseSchema = adminEnvelope(
  z.object({ workers: z.array(fleetWorkerSchema) }),
);

// ---------------------------------------------------------------------------
// Control flags
// ---------------------------------------------------------------------------

export const CONTROL_FLAG_SCOPES = ['global', 'org', 'platform', 'worker'] as const;
export type ControlFlagScope = (typeof CONTROL_FLAG_SCOPES)[number];

/** DB row is snake_case on `update_required`; normalise it to `updateRequired`. */
export const controlFlagSchema = z
  .object({
    scope: z.enum(CONTROL_FLAG_SCOPES),
    scopeKey: z.string(),
    drain: z.boolean(),
    halt: z.boolean(),
    update_required: z.boolean(),
    reason: z.string().nullable(),
    setBy: z.string().nullable(),
    updatedAt: z.number(),
  })
  .transform((f) => ({
    scope: f.scope,
    scopeKey: f.scopeKey,
    drain: f.drain,
    halt: f.halt,
    updateRequired: f.update_required,
    reason: f.reason,
    setBy: f.setBy,
    updatedAt: f.updatedAt,
  }));
export type ControlFlag = z.infer<typeof controlFlagSchema>;

export const controlFlagsResponseSchema = adminEnvelope(
  z.object({ flags: z.array(controlFlagSchema) }),
);

/** set (clear:false) returns the upserted flag; clear:true returns {cleared}. */
export const controlFlagWriteResponseSchema = adminEnvelope(
  z.union([
    z.object({ flag: controlFlagSchema }),
    z.object({ cleared: z.boolean() }),
  ]),
);

export const revokeWorkerResponseSchema = adminEnvelope(
  z.object({ revoked: z.boolean() }),
);

// ---------------------------------------------------------------------------
// Worker enrolment tokens (v22 — per-worker, single-use, admin-minted tokens
// that carry a SERVER-ASSIGNED scope; BUILD-PLAN B8 fix, engine/aizu/server.py's
// ADMIN_WORKER_ENROLMENT_TOKENS_PATH / ADMIN_WORKER_ENROLMENT_TOKEN_REVOKE_PATH)
// ---------------------------------------------------------------------------

export const ENROLMENT_SCOPE_KINDS = ['org', 'pool'] as const;
export type EnrolmentScopeKind = (typeof ENROLMENT_SCOPE_KINDS)[number];

/** A minted enrolment token, never the plaintext or hash. */
export const enrolmentTokenSchema = z.object({
  id: z.string(),
  scopeKind: z.enum(ENROLMENT_SCOPE_KINDS),
  orgId: z.number().nullable(),
  label: z.string().nullable(),
  createdAt: z.number(),
  createdByAdminId: z.number().nullable(),
  expiresAt: z.number(),
  redeemedAt: z.number().nullable(),
  redeemedByWorkerId: z.string().nullable(),
  revokedAt: z.number().nullable(),
  revokedByAdminId: z.number().nullable(),
});
export type EnrolmentToken = z.infer<typeof enrolmentTokenSchema>;

export const fleetEnrolmentTokensResponseSchema = adminEnvelope(
  z.object({ tokens: z.array(enrolmentTokenSchema) }),
);

/** Mint returns the token record PLUS the plaintext — shown to the admin exactly
 * once; the plaintext never appears in any other response (list/redeem). */
const mintedEnrolmentTokenSchema = enrolmentTokenSchema.extend({ token: z.string() });
export type MintEnrolmentTokenResult = z.infer<typeof mintedEnrolmentTokenSchema>;
export const mintEnrolmentTokenResponseSchema = adminEnvelope(mintedEnrolmentTokenSchema);

export const revokeEnrolmentTokenResponseSchema = adminEnvelope(
  z.object({ revoked: z.boolean() }),
);

// ---------------------------------------------------------------------------
// Execution backend (v16 superadmin run-routing switch)
// ---------------------------------------------------------------------------

export const EXECUTION_BACKENDS = ['in_process', 'distributed'] as const;
export type ExecutionBackend = (typeof EXECUTION_BACKENDS)[number];
export const executionBackendSchema = z.enum(EXECUTION_BACKENDS).catch('in_process');

export const executionBackendResponseSchema = adminEnvelope(
  z.object({
    backend: executionBackendSchema,
    options: z.array(z.string()).catch([...EXECUTION_BACKENDS]),
  }),
);
export type ExecutionBackendState = NonNullable<
  z.infer<typeof executionBackendResponseSchema>['data']
>;

// ---------------------------------------------------------------------------
// Model comparison (v17 superadmin LLM fan-out switch + Model Performance page)
// ---------------------------------------------------------------------------

export const modelComparisonSettingsSchema = z.object({
  enabled: z.boolean(),
  /** Env-declared comparison models on the box that served this request — display-only. */
  models: z.array(z.string()).catch([]),
});
export type ModelComparisonSettings = z.infer<typeof modelComparisonSettingsSchema>;

export const modelComparisonSettingsResponseSchema = adminEnvelope(
  modelComparisonSettingsSchema,
);

export const modelComparisonModelStatsSchema = z.object({
  model: z.string(),
  isPrimary: z.boolean(),
  calls: z.number(),
  avgLatencyMs: z.number().nullable(),
  avgUsd: z.number().nullable(),
  avgScore: z.number().nullable(),
  agreementRate: z.number().nullable(),
  errors: z.number(),
  leadsFound: z.number(),
});
export type ModelComparisonModelStats = z.infer<typeof modelComparisonModelStatsSchema>;

export const modelComparisonCallSchema = z.object({
  campaign_id: z.string(),
  session_id: z.string().nullable(),
  platform: z.string().nullable(),
  stage: z.string(),
  model: z.string(),
  is_primary: z.number(),
  label: z.string().nullable(),
  score: z.number().nullable(),
  confidence: z.number().nullable(),
  agreed: z.number().nullable(),
  latency_ms: z.number().nullable(),
  usd: z.number().nullable(),
  error: z.string().nullable(),
  created_at: z.number(),
});
export type ModelComparisonCall = z.infer<typeof modelComparisonCallSchema>;

export const modelComparisonStatsResponseSchema = adminEnvelope(
  z.object({
    stats: z.array(modelComparisonModelStatsSchema),
    recent: z.array(modelComparisonCallSchema),
  }),
);
export type ModelComparisonStatsPage = NonNullable<
  z.infer<typeof modelComparisonStatsResponseSchema>['data']
>;

// ---------------------------------------------------------------------------
// Enqueue
// ---------------------------------------------------------------------------

/** The enqueued job echo — we only surface a handful of fields in the UI. */
export const enqueuedJobSchema = z.object({
  id: z.string(),
  campaignId: z.string(),
  platform: z.string(),
  status: z.string(),
  requiredAccountHandle: z.string().nullable(),
  createdAt: z.number(),
});
export type EnqueuedJob = z.infer<typeof enqueuedJobSchema>;

export const enqueueJobResponseSchema = adminEnvelope(
  z.object({ job: enqueuedJobSchema }),
);

// ---------------------------------------------------------------------------
// Cross-org index + drill-in
// ---------------------------------------------------------------------------

/** Org index row (DB snake_case → camel). */
export const adminOrgSchema = z
  .object({
    id: z.number(),
    name: z.string(),
    logo: z.string().nullable(),
    description: z.string().nullable(),
    created_at: z.number(),
    member_count: z.number(),
    campaign_count: z.number(),
  })
  .transform((o) => ({
    id: o.id,
    name: o.name,
    logo: o.logo,
    description: o.description,
    createdAt: o.created_at,
    memberCount: o.member_count,
    campaignCount: o.campaign_count,
  }));
export type AdminOrg = z.infer<typeof adminOrgSchema>;

export const adminOrgsResponseSchema = adminEnvelope(
  z.object({ orgs: z.array(adminOrgSchema) }),
);

/** Cross-org campaign row (already camelCase off build_campaigns_org). */
export const adminOrgCampaignSchema = z.object({
  id: z.string(),
  displayName: z.string().nullable(),
  platform: z.string(),
  status: z.string(),
  createdAt: z.number().nullable().catch(null),
  updatedAt: z.number().nullable().catch(null),
  archived: z.boolean().catch(false),
});
export type AdminOrgCampaign = z.infer<typeof adminOrgCampaignSchema>;

export const adminOrgCampaignsResponseSchema = adminEnvelope(
  z.object({ campaigns: z.array(adminOrgCampaignSchema) }),
);

/**
 * Cross-org lead row (camelCase off build_leads_org).
 *
 * This is the ONE surface that still carries a lead's real identity. v27 redaction
 * removed `username` and the comment `text` from every ORG-facing payload; here they
 * STAY, and the derived `intent` is added beside them — that pairing is how an operator
 * checks that the redaction is summarising honestly. Keep the three together.
 */
export const adminOrgLeadSchema = z.object({
  commentId: z.string(),
  campaignId: z.string(),
  platform: z.string(),
  username: z.string(),
  text: z.string(),
  // The customer-facing line derived from the two fields above. `''` for a pre-v27 row
  // captured before redaction existed — render a placeholder, never the raw text.
  intent: z.string().catch(''),
  capturedAt: z.number().nullable().catch(null),
  status: z.string(),
  score: z.number().nullable().catch(null),
  reason: z.string().nullable().catch(null),
  extracted: z.boolean().catch(false),
  tier: z.string().nullable().catch(null),
});
export type AdminOrgLead = z.infer<typeof adminOrgLeadSchema>;

export const adminOrgLeadsResponseSchema = adminEnvelope(
  z.object({
    leads: z.array(adminOrgLeadSchema),
    page: z.number(),
    pageSize: z.number(),
    total: z.number(),
  }),
);
export type AdminOrgLeadsPage = NonNullable<
  z.infer<typeof adminOrgLeadsResponseSchema>['data']
>;

// ---------------------------------------------------------------------------
// Run inspection (v27) — the narrative feed the ORG plane no longer serves
//
// Run logs left the customer app entirely: /api/run/activity answers an org with
// scalars and `events: []`. The full feed lives here, behind the real admin gate
// (IP-allowlist + admin session). Two endpoints: a picker
// (GET /api/admin/orgs/{id}/runs, org-scoped) and the feed itself
// (GET /api/admin/run/activity?runId=&after=, cross-tenant BY DESIGN — a superadmin
// inspects a run whoever owns it, so it carries no org scope at all).
//
// The event/counter/flag shapes are declared LOCALLY rather than imported from
// panelState: the two planes are deliberately separate branches (see the module
// header), and these rows are the un-redacted variant — an admin event carries the
// `message`, the raw `detail` blob and the `sessionId` an org row must never see.
// ---------------------------------------------------------------------------

/** One of an org's recent runs, newest first — the picker for the feed below. A run is
 * the set of `sessions` sharing a run_id, folded server-side; a fleet run that has not
 * acked yet has no session rows, so it is merged in from its live job (0 sessions, 0
 * leads, status running). `mode` is null for a run this process no longer remembers —
 * honestly unknown rather than a guess. Epoch SECONDS. */
export const adminOrgRunSchema = z.object({
  runId: z.string(),
  campaignId: z.string(),
  campaignName: z.string(),
  mode: z.string().nullable().catch(null),
  status: z.string(),
  platforms: z.array(z.string()).catch([]),
  startedAt: z.number().nullable().catch(null),
  finishedAt: z.number().nullable().catch(null),
  sessions: z.number().catch(0),
  leads: z.number().catch(0),
});
export type AdminOrgRun = z.infer<typeof adminOrgRunSchema>;

export const adminOrgRunsResponseSchema = adminEnvelope(
  z.object({ runs: z.array(adminOrgRunSchema) }),
);

/** Severity of one event. Unknown levels degrade to 'info' — one odd row must never
 * fail the array and blank the whole feed. */
export const adminRunEventLevelSchema = z
  .enum(['info', 'success', 'warn', 'error'])
  .catch('info');

/**
 * One narrative run event, in full. `id` is the global insertion id — the paging cursor
 * and the React key; `seq` RESETS per session and is display-only, so never page or key
 * on it. `detail` is a raw JSON STRING to parse lazily (and tolerate failing to parse).
 * `createdAt` is epoch SECONDS (float).
 *
 * SECURITY: `message` and `detail` are ENGINE-authored text carrying real identities
 * (a match detail is `{username, score, tier, reelId}`). Render them as text, never as
 * markup, and never plumb this type into an org-facing component.
 */
export const adminRunEventSchema = z.object({
  id: z.number(),
  seq: z.number(),
  campaignId: z.string().nullable(),
  sessionId: z.string().nullable().catch(null),
  phase: z.string(),
  level: adminRunEventLevelSchema,
  message: z.string(),
  detail: z.string().nullable().catch(null),
  createdAt: z.number(),
  platform: z.string().nullable().catch(null),
});
export type AdminRunEvent = z.infer<typeof adminRunEventSchema>;

/** Live tally for the run (summed across its sessions). Each counter degrades to 0. */
export const adminRunCountersSchema = z.object({
  reelsSeen: z.number().catch(0),
  relevancePasses: z.number().catch(0),
  commentsScored: z.number().catch(0),
  matches: z.number().catch(0),
  spendUsd: z.number().catch(0),
  likes: z.number().catch(0),
  follows: z.number().catch(0),
});

/** An open health/safety flag surfaced for the run. */
export const adminRunFlagSchema = z.object({
  kind: z.string(),
  severity: z.string(),
  detail: z.string().nullable(),
});

/**
 * One poll of the FULL feed. `events` are oldest-first and only those with id > the
 * `after` we sent; `cursor` is the max event id in this page (or the `after` echoed
 * back when empty). Unlike the org endpoint there is no fleet-job block and no
 * redaction: `finished` comes straight off the in-memory active run + the durable
 * session rows, and an unknown run answers an empty feed rather than 404 (there is no
 * tenant boundary left to protect, and a fleet run that has not heartbeated yet is
 * exactly the run an operator is trying to watch).
 */
export const adminRunActivitySchema = z.object({
  runId: z.string(),
  finished: z.boolean(),
  counters: adminRunCountersSchema,
  events: z.array(adminRunEventSchema),
  flags: z.array(adminRunFlagSchema).catch([]),
  cursor: z.number().catch(0),
});
export type AdminRunActivity = z.infer<typeof adminRunActivitySchema>;

export const adminRunActivityResponseSchema = adminEnvelope(adminRunActivitySchema);

// ---------------------------------------------------------------------------
// Audit log + chain verify
// ---------------------------------------------------------------------------

/** Audit rows come straight off the DB (snake_case); normalise to camel. */
export const auditEntrySchema = z
  .object({
    id: z.number(),
    prev_hash: z.string(),
    row_hash: z.string(),
    acting_admin_id: z.number().nullable(),
    action: z.string(),
    target_org_id: z.number().nullable(),
    target_user_id: z.number().nullable(),
    target_resource: z.string().nullable(),
    at: z.number(),
    ip: z.string(),
    user_agent: z.string().nullable(),
    reason: z.string().nullable(),
    impersonation_start: z.number().nullable(),
    impersonation_end: z.number().nullable(),
  })
  .transform((r) => ({
    id: r.id,
    prevHash: r.prev_hash,
    rowHash: r.row_hash,
    actingAdminId: r.acting_admin_id,
    action: r.action,
    targetOrgId: r.target_org_id,
    targetUserId: r.target_user_id,
    targetResource: r.target_resource,
    at: r.at,
    ip: r.ip,
    userAgent: r.user_agent,
    reason: r.reason,
    impersonationStart: r.impersonation_start,
    impersonationEnd: r.impersonation_end,
  }));
export type AuditEntry = z.infer<typeof auditEntrySchema>;

export const auditResponseSchema = adminEnvelope(
  z.object({ entries: z.array(auditEntrySchema) }),
);

export const auditVerifySchema = z.object({
  ok: z.boolean(),
  count: z.number(),
  firstBadId: z.number().nullable(),
});
export type AuditVerify = z.infer<typeof auditVerifySchema>;

export const auditVerifyResponseSchema = adminEnvelope(auditVerifySchema);

// ---------------------------------------------------------------------------
// Request inputs (constructed by us → plain TS interfaces, camelCase)
// ---------------------------------------------------------------------------

export interface AdminLoginInput {
  readonly email: string;
  readonly password: string;
  readonly totpCode: string;
}

export interface ImpersonateInput {
  readonly orgId?: number | null;
  readonly userId?: number | null;
  readonly reason: string;
}

export interface ControlFlagSetInput {
  readonly scope: ControlFlagScope;
  readonly scopeKey?: string;
  readonly clear?: boolean;
  readonly drain?: boolean | null;
  readonly halt?: boolean | null;
  readonly updateRequired?: boolean | null;
  readonly reason?: string | null;
}

export interface EnqueueJobInput {
  readonly campaignId: string;
  readonly platform: string;
  readonly orgId?: number | null;
  readonly requiredAccountHandle?: string | null;
  readonly engineMode?: 'harvest' | 'warming';
  readonly targetLeads?: number | null;
  readonly durationMinutes?: number | null;
  readonly soulText?: string | null;
}

export interface MintEnrolmentTokenInput {
  readonly scope: EnrolmentScopeKind;
  readonly orgId?: number;
  readonly label?: string;
  readonly ttlHours?: number;
}

/** GET /api/admin/run/activity — one run's full feed. `after` is the global event id
 * cursor (0 = from the start), NOT a `seq`. No org id: the endpoint is cross-tenant. */
export interface AdminRunActivityQuery {
  readonly runId: string;
  readonly after?: number;
}

export interface AdminOrgLeadsQuery {
  readonly orgId: number;
  readonly page: number;
  readonly pageSize: number;
  readonly q?: string;
  readonly status?: string;
  readonly platform?: string;
  readonly campaign?: string;
  readonly sort?: string;
  readonly dir?: 'asc' | 'desc';
}
