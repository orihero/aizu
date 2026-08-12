import type { Result } from '@/shared/lib/result';
import type {
  AdminLoginInput,
  AdminOrg,
  AdminOrgCampaign,
  AdminOrgLeadsPage,
  AdminOrgLeadsQuery,
  AdminSession,
  AuditEntry,
  AuditVerify,
  ControlFlag,
  ControlFlagSetInput,
  EnqueueJobInput,
  EnqueuedJob,
  EnrolmentToken,
  ExecutionBackend,
  ExecutionBackendState,
  FleetWorker,
  ImpersonateInput,
  MintEnrolmentTokenInput,
  MintEnrolmentTokenResult,
  ModelComparisonSettings,
  ModelComparisonStatsPage,
} from '@/shared/schemas/admin';
import type {
  AddLeadNoteInput,
  AgentReadiness,
  AgentReadinessOptions,
  ArchiveCampaignInput,
  AuthUser,
  BillingPortal,
  BulkStatusRequest,
  CampaignInput,
  CampaignsPayload,
  CheckoutInput,
  CheckoutSession,
  Credentials,
  DashboardPayload,
  DeleteLeadNoteInput,
  DirectAddInput,
  GenerateCampaignInput,
  GeneratedCampaignDraft,
  Integration,
  InterviewRequest,
  InterviewResponse,
  InviteCreateInput,
  InviteInfo,
  InviteLink,
  LaunchAgentLoginResult,
  LeadsPayload,
  LeadsQuery,
  OrganizationInput,
  PanelState,
  RedditConnectInput,
  ReportsPayload,
  RunActivity,
  RunInput,
  RunStartResult,
  ScheduleCampaignInput,
  SettingsPayload,
  SignupCredentials,
  StatusWriteRequest,
  TelegramLoginStart,
  TelegramVerifyInput,
  TelegramVerifyResult,
  UpdateRoleInput,
  WorkspaceSettingsInput,
} from '@/shared/types/domain';

/**
 * Data-access contract for the panel (dependency inversion: features
 * depend on this interface, never on fetch or a concrete transport).
 * Implementations: HttpPanelRepository (bridge server), FakePanelRepository (tests).
 */
export interface PanelRepository {
  // ---- auth (email + password; the bridge gates every other surface) ----
  /** Create a company (or join one via inviteToken); sets the session cookie. */
  signup(credentials: SignupCredentials): Promise<Result<AuthUser>>;
  /** Authenticate; on success the bridge sets the session cookie. */
  login(credentials: Credentials): Promise<Result<AuthUser>>;
  /** Clear the current session (best-effort; always clears the client cookie). */
  logout(): Promise<Result<void>>;
  /** Resolve the current session to a user, or `ok(null)` when not signed in. */
  getCurrentUser(): Promise<Result<AuthUser | null>>;
  /** Public: look up an invite by token (org branding + role), or ok(null) if invalid. */
  getInvite(token: string): Promise<Result<InviteInfo | null>>;

  /**
   * Fetch the panel state, optionally scoped to one campaign (default: file campaign).
   * @deprecated Superseded by the per-page fetchers below; removed once all pages migrate.
   */
  fetchState(campaignId?: string | null): Promise<Result<PanelState>>;

  // ---- per-page reads (org-wide; no campaign scoping) ----
  /** GET /api/dashboard — bento + ticker matches + health/alerts + run block. */
  fetchDashboard(): Promise<Result<DashboardPayload>>;
  /** GET /api/campaigns — every campaign card + pooled sessions + run block. */
  fetchCampaigns(): Promise<Result<CampaignsPayload>>;
  /** GET /api/leads — org-wide leads, server-side filtered/sorted/paginated. */
  fetchLeads(query: LeadsQuery): Promise<Result<LeadsPayload>>;
  /** GET /api/reports — org-wide time series + per-campaign rollup. */
  fetchReports(): Promise<Result<ReportsPayload>>;
  /** GET /api/settings — workspace config + team + invites + integrations. */
  fetchSettings(): Promise<Result<SettingsPayload>>;
  setMatchStatus(request: StatusWriteRequest): Promise<Result<void>>;
  // v3 write surfaces — the panel is read-write beyond match status.
  bulkSetStatus(request: BulkStatusRequest): Promise<Result<void>>;
  // v6 lead notes — any lead-editor adds; only the author deletes (server-enforced).
  addLeadNote(input: AddLeadNoteInput): Promise<Result<void>>;
  deleteLeadNote(input: DeleteLeadNoteInput): Promise<Result<void>>;
  createCampaign(input: CampaignInput): Promise<Result<void>>;
  /** Archive or un-archive a campaign (reversible hide; archive-while-live also
   *  stops the run and parks it at paused, server-side). */
  archiveCampaign(input: ArchiveCampaignInput): Promise<Result<void>>;
  /** Arm/replace (enabled) or clear (disabled) a campaign's recurring schedule.
   *  The server computes the next fire. */
  scheduleCampaign(input: ScheduleCampaignInput): Promise<Result<void>>;
  /** Draft a campaign with AI from a description / URL / screenshot. Writes
   *  nothing server-side — returns a partial form prefill to review and save. */
  generateCampaign(input: GenerateCampaignInput): Promise<Result<GeneratedCampaignDraft>>;
  /** Run one round of the conversational campaign interview. Writes nothing —
   *  returns the next questions (or done) + the serialized product context to
   *  echo back next round. */
  runInterview(input: InterviewRequest): Promise<Result<InterviewResponse>>;
  // Spawns the engine for one campaign or every live campaign (control plane).
  // Resolves with the run-start result: a fleet-routed live run carries a `runId` the
  // caller can poll for the live feed (in-process runs leave it null).
  runCampaign(input: RunInput): Promise<Result<RunStartResult>>;
  /** Stop the in-flight run early; 409 (→ error) when nothing of yours is active. */
  stopRun(): Promise<Result<void>>;
  /** Cooperatively pause the in-flight run (resumable). Idempotent; 409 when idle. */
  pauseRun(): Promise<Result<void>>;
  /** Resume a paused in-flight run. Idempotent; 409 when idle. */
  resumeRun(): Promise<Result<void>>;
  /** Poll a run's live activity feed for events with seq > afterSeq (default 0). */
  fetchRunActivity(runId: string, afterSeq?: number): Promise<Result<RunActivity>>;
  /** GET /api/agent/readiness — whether the Instagram warmed-browser agent (CDP +
   * logged-in session) can run a live campaign right now. `refresh: true` forces a
   * live probe past the server's ≤60s cache. */
  getAgentReadiness(opts?: AgentReadinessOptions): Promise<Result<AgentReadiness>>;
  /** POST /api/agent/launch-login — spawn/attach Chrome and open a login tab
   * (RBAC action `fix_agent`: owner+admin only). Returns whether a browser was
   * launched plus the freshly re-checked readiness. */
  launchAgentLogin(): Promise<Result<LaunchAgentLoginResult>>;
  updateSettings(input: WorkspaceSettingsInput): Promise<Result<void>>;
  /** Edit the company profile (owner/admin). */
  updateOrganization(input: OrganizationInput): Promise<Result<void>>;
  // ---- team management over real accounts (v7) ----
  /** Direct-add a teammate with a password set by the inviter. */
  addTeamMember(input: DirectAddInput): Promise<Result<void>>;
  /** Create a shareable invite link; the raw token is returned once. */
  createInvite(input: InviteCreateInput): Promise<Result<InviteLink>>;
  /** Revoke a pending invite by id. */
  revokeInvite(id: string): Promise<Result<void>>;
  /** Change a teammate's role. */
  updateMemberRole(input: UpdateRoleInput): Promise<Result<void>>;
  /** Remove a teammate. */
  removeMember(userId: number): Promise<Result<void>>;
  toggleIntegration(integration: Integration, connected: boolean): Promise<Result<void>>;
  /** Connect YouTube with a customer-supplied Data API key (validated server-side). */
  connectYouTube(apiKey: string): Promise<Result<void>>;
  /** Connect Reddit with the app's client_credentials trio (validated server-side). */
  connectReddit(input: RedditConnectInput): Promise<Result<void>>;
  /** Begin the Telegram MTProto login: send a code to `phone`, return a wizard token. */
  startTelegramLogin(phone: string): Promise<Result<TelegramLoginStart>>;
  /** Finish the Telegram login with the SMS/app code (+ 2FA password if required).
   * Resolves with `needsPassword: true` when 2FA is on and no password was sent. */
  verifyTelegramLogin(input: TelegramVerifyInput): Promise<Result<TelegramVerifyResult>>;
  // ---- billing (v13; Aizu's single Polar account, org = customer) ----
  /** Open a Polar hosted-checkout for a self-serve tier (lite/starter/pro) at the
   * chosen interval; returns the URL to redirect the browser to. */
  openCheckout(input: CheckoutInput): Promise<Result<CheckoutSession>>;
  /** Open the Polar customer portal (manage/upgrade/cancel/invoices). Returns a URL,
   * or `hasAccount: false` for a Free org with no Polar customer yet. */
  openBillingPortal(): Promise<Result<BillingPortal>>;

  // ---- superadmin plane (v15; SEPARATE /api/admin/* branch, its own cookie) ----
  // These never carry org scope. The whole plane sits behind the admin session
  // gate (IP-allowlist + rr_admin_session cookie) — org RBAC does not apply.
  /** POST /api/admin/login — password + TOTP; sets the rr_admin_session cookie. */
  adminLogin(input: AdminLoginInput): Promise<Result<{ id: number; email: string }>>;
  /** POST /api/admin/logout — clears the admin cookie (best-effort). */
  adminLogout(): Promise<Result<void>>;
  /** GET /api/admin/whoami — resolve the admin session + impersonation, or ok(null). */
  adminWhoami(): Promise<Result<AdminSession | null>>;
  /** GET /api/admin/fleet — the worker fleet snapshot (derived presence). */
  fetchFleet(): Promise<Result<FleetWorker[]>>;
  /** GET /api/admin/control-flags — every set drain/halt/update flag, per scope. */
  fetchControlFlags(): Promise<Result<ControlFlag[]>>;
  /** POST /api/admin/control-flags — upsert (clear:false) or delete (clear:true) a flag. */
  setControlFlag(input: ControlFlagSetInput): Promise<Result<void>>;
  /** POST /api/admin/workers/revoke — revoke a worker's token (fails it next request). */
  revokeWorker(workerId: string): Promise<Result<{ revoked: boolean }>>;
  /** POST /api/admin/worker-enrolment-tokens — mint a per-worker enrolment token; the
   * plaintext token is returned ONLY in this response. */
  mintEnrolmentToken(input: MintEnrolmentTokenInput): Promise<Result<MintEnrolmentTokenResult>>;
  /** GET /api/admin/worker-enrolment-tokens — list enrolment tokens (never the
   * plaintext or hash). */
  fetchEnrolmentTokens(): Promise<Result<EnrolmentToken[]>>;
  /** POST /api/admin/worker-enrolment-tokens/revoke — cancel an unredeemed token. */
  revokeEnrolmentToken(tokenId: string): Promise<Result<{ revoked: boolean }>>;
  /** POST /api/admin/jobs/enqueue — queue a job for a capable worker. */
  enqueueJob(input: EnqueueJobInput): Promise<Result<EnqueuedJob>>;
  /** GET /api/admin/orgs — cross-org index (member + campaign counts). */
  fetchAdminOrgs(): Promise<Result<AdminOrg[]>>;
  /** GET /api/admin/orgs/{id}/campaigns — read-only campaign list for one org. */
  fetchAdminOrgCampaigns(orgId: number): Promise<Result<AdminOrgCampaign[]>>;
  /** GET /api/admin/orgs/{id}/leads — read-only paginated leads for one org. */
  fetchAdminOrgLeads(query: AdminOrgLeadsQuery): Promise<Result<AdminOrgLeadsPage>>;
  /** POST /api/admin/impersonate — start acting as an org/user (audited, reason req'd). */
  startImpersonation(input: ImpersonateInput): Promise<Result<void>>;
  /** POST /api/admin/impersonate/end — stop impersonating. */
  endImpersonation(): Promise<Result<void>>;
  /** GET /api/admin/audit — the hash-chained admin audit log (newest first). */
  fetchAudit(limit?: number): Promise<Result<AuditEntry[]>>;
  /** GET /api/admin/audit/verify — walk the chain; report the first tampered row. */
  verifyAudit(): Promise<Result<AuditVerify>>;
  /** GET /api/admin/execution-backend — the platform-wide run routing + options. */
  fetchExecutionBackend(): Promise<Result<ExecutionBackendState>>;
  /** POST /api/admin/execution-backend — switch where EVERY run executes. */
  setExecutionBackend(backend: ExecutionBackend): Promise<Result<void>>;
  /** GET /api/admin/model-comparison — the LLM fan-out switch + env-configured models. */
  fetchModelComparisonSettings(): Promise<Result<ModelComparisonSettings>>;
  /** POST /api/admin/model-comparison — turn the fan-out on/off. */
  setModelComparisonEnabled(enabled: boolean): Promise<Result<void>>;
  /** GET /api/admin/model-comparison/stats — per-model aggregates + recent calls. */
  fetchModelComparisonStats(): Promise<Result<ModelComparisonStatsPage>>;
}
