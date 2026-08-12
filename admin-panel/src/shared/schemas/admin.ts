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

/** Cross-org lead row (camelCase off build_leads_org). */
export const adminOrgLeadSchema = z.object({
  commentId: z.string(),
  campaignId: z.string(),
  platform: z.string(),
  username: z.string(),
  text: z.string(),
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
