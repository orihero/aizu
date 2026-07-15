import { appError, err, ok, type Result } from '@/shared/lib/result';
import type { PanelRepository } from '@/shared/api/panelRepository';
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
  ExecutionBackend,
  ExecutionBackendState,
  FleetWorker,
  ImpersonateInput,
  ModelComparisonSettings,
  ModelComparisonStatsPage,
} from '@/shared/schemas/admin';
import type {
  AddLeadNoteInput,
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
  Billing,
  Integration,
  InterviewRequest,
  InterviewResponse,
  LeadNote,
  LeadsPayload,
  LeadsQuery,
  InviteCreateInput,
  InviteInfo,
  InviteLink,
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
import { buildBilling, paginateLeads } from './fixtures';

const DEFAULT_ORG = { id: 1, name: 'Test Co', logo: null, description: null };
const DEFAULT_USER: AuthUser = {
  id: 1, email: 'tester@reelradar.test', role: 'owner', orgId: 1, org: DEFAULT_ORG,
};

interface TeamOp {
  readonly op: 'add' | 'invite' | 'revoke' | 'updateRole' | 'remove';
  readonly id?: string;
  readonly userId?: number;
  readonly input?: DirectAddInput | InviteCreateInput | UpdateRoleInput;
}

/**
 * In-memory PanelRepository for tests: serves a fixture state and
 * records writes; can be told to fail the next write.
 */
export class FakePanelRepository implements PanelRepository {
  readonly statusWrites: StatusWriteRequest[] = [];
  readonly bulkWrites: BulkStatusRequest[] = [];
  readonly noteAdds: AddLeadNoteInput[] = [];
  readonly noteDeletes: DeleteLeadNoteInput[] = [];
  readonly campaignCreates: CampaignInput[] = [];
  /** Every archive/un-archive request, in order. */
  readonly archiveRequests: ArchiveCampaignInput[] = [];
  /** Every schedule arm/clear request, in order. */
  readonly scheduleRequests: ScheduleCampaignInput[] = [];
  // ---- AI generate seam ----
  /** Every input the composer asked us to draft from, in order. */
  readonly generateRequests: GenerateCampaignInput[] = [];
  /** The draft to return on success. */
  generatedDraft: GeneratedCampaignDraft = {};
  /**
   * Forces the next generate to fail with a specific shape so the composer's
   * error map can be exercised: 503 (no AI key), 422 (unbuildable input), or a
   * network drop. null → succeed with `generatedDraft`.
   */
  generateFailure: 'keyMissing' | 'unbuildable' | 'network' | null = null;
  // ---- AI interview seam ----
  /** Every interview round the wizard asked us to run, in order. */
  readonly interviewRequests: InterviewRequest[] = [];
  /**
   * The interview replies to return, consumed in order (one per round). When the
   * queue empties, a done=true reply is returned so the wizard converges. Default
   * `interviewFailure` mirrors generate's failure shapes.
   */
  interviewResults: InterviewResponse[] = [];
  interviewFailure: 'keyMissing' | 'unbuildable' | 'network' | null = null;
  readonly runRequests: RunInput[] = [];
  /** How many times stopRun() was called (the Stop button). */
  stopRequests = 0;
  /** How many times pauseRun()/resumeRun() were called. */
  pauseRequests = 0;
  resumeRequests = 0;
  readonly settingsUpdates: WorkspaceSettingsInput[] = [];
  readonly orgUpdates: OrganizationInput[] = [];
  readonly teamOps: TeamOp[] = [];
  readonly integrationToggles: { id: string; connected: boolean }[] = [];
  readonly youtubeConnects: string[] = [];
  readonly redditConnects: RedditConnectInput[] = [];
  readonly telegramStarts: string[] = [];
  readonly telegramVerifies: TelegramVerifyInput[] = [];
  // ---- billing seam ----
  /** Every checkout the panel asked us to open, in order (tier + interval). */
  readonly checkoutCalls: CheckoutInput[] = [];
  /** How many times openBillingPortal() was called (the Manage-billing button). */
  portalCalls = 0;
  /** The checkout URL create returns; tests can override to assert the redirect. */
  checkoutUrl = 'https://sandbox.polar.sh/checkout/fake';
  /** The portal URL + whether the org has a Polar customer (false → Free, no account). */
  portalUrl = 'https://sandbox.polar.sh/portal/fake';
  hasBillingAccount = true;
  /** The BILLING summary fetchSettings serves. undefined → role-pruned (viewer). */
  billing: Billing | undefined = buildBilling();
  /** Drives the start step's 2FA hint so tests can exercise the password branch. */
  telegramNeedsPassword = false;
  readonly stateFetches: (string | null)[] = [];
  // Per-page fetch records, mirroring stateFetches for the migrated endpoints.
  dashboardFetches = 0;
  campaignsFetches = 0;
  reportsFetches = 0;
  settingsFetches = 0;
  readonly leadsFetches: LeadsQuery[] = [];
  failNextWrite = false;

  // ---- run activity seam ----
  /** Records every (runId, afterSeq) the feed polled, in order. */
  readonly runActivityFetches: { runId: string; afterSeq: number }[] = [];
  /**
   * Drives fetchRunActivity. A function lets a test return successive pages
   * keyed off the cursor the hook sends; a plain value returns the same page.
   * null → an http error (mirrors an unknown/cross-org run).
   */
  runActivity:
    | RunActivity
    | ((runId: string, afterSeq: number) => RunActivity)
    | null = null;

  // ---- auth seam ----
  readonly loginAttempts: Credentials[] = [];
  readonly signupAttempts: SignupCredentials[] = [];
  logoutCount = 0;
  /** Default authenticated so existing page tests bootstrap past the auth gate. */
  currentUser: AuthUser | null = DEFAULT_USER;
  /** When set, the next login/signup rejects with this message instead of succeeding. */
  failNextAuth: string | null = null;
  /** Returned by getInvite (null = no/invalid invite). */
  invite: InviteInfo | null = null;
  /** The link createInvite hands back; tests can override. */
  inviteLink: InviteLink = { token: 'fake-token', path: '/signup?invite=fake-token',
    role: 'viewer', email: null };

  // ---- superadmin plane seam ----
  /** The admin whoami resolves to this. null → not signed in (guard → login). */
  adminSession: AdminSession | null = null;
  /** Fleet snapshot fetchFleet serves. */
  fleet: FleetWorker[] = [];
  /** Control-flag list fetchControlFlags serves. */
  controlFlags: ControlFlag[] = [];
  /** Cross-org index fetchAdminOrgs serves. */
  adminOrgs: AdminOrg[] = [];
  /** Per-org campaign lists (orgId → campaigns). */
  adminOrgCampaigns: Record<number, AdminOrgCampaign[]> = {};
  /** Per-org leads pages (orgId → full page). */
  adminOrgLeads: Record<number, AdminOrgLeadsPage> = {};
  /** Audit rows fetchAudit serves. */
  auditEntries: AuditEntry[] = [];
  /** Chain-verify result verifyAudit serves. */
  auditVerifyResult: AuditVerify = { ok: true, count: 0, firstBadId: null };
  /** Job echo enqueueJob returns on success. */
  enqueuedJob: EnqueuedJob = {
    id: 'job-fake', campaignId: 'c-1', platform: 'instagram', status: 'queued',
    requiredAccountHandle: null, createdAt: 0,
  };
  // records
  readonly adminLoginAttempts: AdminLoginInput[] = [];
  adminLogoutCount = 0;
  readonly controlFlagWrites: ControlFlagSetInput[] = [];
  readonly workerRevokes: string[] = [];
  readonly enqueueRequests: EnqueueJobInput[] = [];
  readonly impersonateRequests: ImpersonateInput[] = [];
  endImpersonateCount = 0;
  /** The platform-wide execution backend fetchExecutionBackend serves + set records. */
  executionBackend: ExecutionBackend = 'in_process';
  readonly executionBackendWrites: ExecutionBackend[] = [];
  /** The model-comparison switch fetch/set fake serves + records. */
  modelComparisonSettings: ModelComparisonSettings = { enabled: false, models: [] };
  readonly modelComparisonWrites: boolean[] = [];
  modelComparisonStats: ModelComparisonStatsPage = { stats: [], recent: [] };
  /** When set, the next admin write/read rejects with this {message,status}. */
  failNextAdmin: { message: string; status: number } | null = null;

  constructor(private state: PanelState) {}

  signup(credentials: SignupCredentials): Promise<Result<AuthUser>> {
    this.signupAttempts.push(credentials);
    return this.resolveAuth(credentials.email);
  }

  login(credentials: Credentials): Promise<Result<AuthUser>> {
    this.loginAttempts.push(credentials);
    return this.resolveAuth(credentials.email);
  }

  getInvite(_token: string): Promise<Result<InviteInfo | null>> {
    return Promise.resolve(ok(this.invite));
  }

  logout(): Promise<Result<void>> {
    this.logoutCount += 1;
    this.currentUser = null;
    return Promise.resolve(ok(undefined));
  }

  getCurrentUser(): Promise<Result<AuthUser | null>> {
    return Promise.resolve(ok(this.currentUser));
  }

  private resolveAuth(email: string): Promise<Result<AuthUser>> {
    if (this.failNextAuth !== null) {
      const message = this.failNextAuth;
      this.failNextAuth = null;
      return Promise.resolve(err(appError('http', message, 401)));
    }
    const user: AuthUser = { id: 1, email, role: 'owner', orgId: 1, org: DEFAULT_ORG };
    this.currentUser = user;
    return Promise.resolve(ok(user));
  }

  fetchState(campaignId?: string | null): Promise<Result<PanelState>> {
    this.stateFetches.push(campaignId ?? null);
    return Promise.resolve(ok(this.state));
  }

  // Per-page reads derive from the single fixture state, so writes that mutate
  // this.state (status, notes) reflect through them just as they did via fetchState.
  fetchDashboard(): Promise<Result<DashboardPayload>> {
    this.dashboardFetches += 1;
    const s = this.state;
    return Promise.resolve(ok({
      DASHBOARD: s.DASHBOARD, MATCHES: s.MATCHES, HEALTH: s.HEALTH,
      ALERTS: s.ALERTS, RUN: s.RUN, CONFIG: s.CONFIG,
    }));
  }

  fetchCampaigns(): Promise<Result<CampaignsPayload>> {
    this.campaignsFetches += 1;
    const s = this.state;
    return Promise.resolve(ok({ CAMPAIGNS: s.CAMPAIGNS, SESSIONS: s.SESSIONS, RUN: s.RUN }));
  }

  fetchReports(): Promise<Result<ReportsPayload>> {
    this.reportsFetches += 1;
    return Promise.resolve(ok({ REPORTS: this.state.REPORTS, HEALTH: this.state.HEALTH }));
  }

  fetchSettings(): Promise<Result<SettingsPayload>> {
    this.settingsFetches += 1;
    const s = this.state;
    return Promise.resolve(ok({
      CONFIG: s.CONFIG, TEAM: s.TEAM, INVITES: s.INVITES, INTEGRATIONS: s.INTEGRATIONS,
      // BILLING is role-pruned server-side; the fixture provides an owner/admin view.
      ...(this.billing ? { BILLING: this.billing } : {}),
    }));
  }

  fetchLeads(query: LeadsQuery): Promise<Result<LeadsPayload>> {
    this.leadsFetches.push(query);
    return Promise.resolve(ok(paginateLeads(this.state.MATCHES, query)));
  }

  setMatchStatus(request: StatusWriteRequest): Promise<Result<void>> {
    if (this.takeFailure()) return this.simulatedFailure();
    this.statusWrites.push(request);
    this.state = {
      ...this.state,
      MATCHES: this.state.MATCHES.map((m) =>
        m.commentId === request.commentId ? { ...m, status: request.status } : m,
      ),
    };
    return Promise.resolve(ok(undefined));
  }

  bulkSetStatus(request: BulkStatusRequest): Promise<Result<void>> {
    if (this.takeFailure()) return this.simulatedFailure();
    this.bulkWrites.push(request);
    const ids = new Set(request.items.map((i) => i.commentId));
    this.state = {
      ...this.state,
      MATCHES: this.state.MATCHES.map((m) =>
        ids.has(m.commentId) ? { ...m, status: request.status } : m,
      ),
    };
    return Promise.resolve(ok(undefined));
  }

  addLeadNote(input: AddLeadNoteInput): Promise<Result<void>> {
    if (this.takeFailure()) return this.simulatedFailure();
    this.noteAdds.push(input);
    const note: LeadNote = {
      id: `note-${this.noteAdds.length}`,
      body: input.body,
      authorEmail: this.currentUser?.email ?? null,
      authorId: this.currentUser?.id ?? null,
      createdAt: 'now',
      createdAtTs: this.noteAdds.length,
    };
    this.state = {
      ...this.state,
      MATCHES: this.state.MATCHES.map((m) =>
        m.commentId === input.commentId ? { ...m, notes: [...m.notes, note] } : m,
      ),
    };
    return Promise.resolve(ok(undefined));
  }

  deleteLeadNote(input: DeleteLeadNoteInput): Promise<Result<void>> {
    if (this.takeFailure()) return this.simulatedFailure();
    // Owner-only delete, mirroring the server: a note authored by someone else
    // is rejected with 403 and left in place.
    const owned = this.state.MATCHES.some((m) =>
      m.notes.some((n) => n.id === input.noteId && n.authorId === (this.currentUser?.id ?? -1)),
    );
    if (!owned) {
      return Promise.resolve(err(appError('http', 'only the note\'s author may delete it', 403)));
    }
    this.noteDeletes.push(input);
    this.state = {
      ...this.state,
      MATCHES: this.state.MATCHES.map((m) => ({
        ...m,
        notes: m.notes.filter((n) => n.id !== input.noteId),
      })),
    };
    return Promise.resolve(ok(undefined));
  }

  createCampaign(input: CampaignInput): Promise<Result<void>> {
    if (this.takeFailure()) return this.simulatedFailure();
    this.campaignCreates.push(input);
    return Promise.resolve(ok(undefined));
  }

  archiveCampaign(input: ArchiveCampaignInput): Promise<Result<void>> {
    if (this.takeFailure()) return this.simulatedFailure();
    this.archiveRequests.push(input);
    return Promise.resolve(ok(undefined));
  }

  scheduleCampaign(input: ScheduleCampaignInput): Promise<Result<void>> {
    if (this.takeFailure()) return this.simulatedFailure();
    this.scheduleRequests.push(input);
    return Promise.resolve(ok(undefined));
  }

  generateCampaign(input: GenerateCampaignInput): Promise<Result<GeneratedCampaignDraft>> {
    this.generateRequests.push(input);
    switch (this.generateFailure) {
      case 'keyMissing':
        // Mirrors the bridge when OPENROUTER_API_KEY is absent.
        return Promise.resolve(err(appError('http', 'OPENROUTER_API_KEY is not configured', 503)));
      case 'unbuildable':
        return Promise.resolve(
          err(appError('http', 'could not draft a campaign from that input', 422)),
        );
      case 'network':
        return Promise.resolve(err(appError('network', 'bridge server unreachable')));
      default:
        return Promise.resolve(ok(this.generatedDraft));
    }
  }

  runInterview(input: InterviewRequest): Promise<Result<InterviewResponse>> {
    this.interviewRequests.push(input);
    switch (this.interviewFailure) {
      case 'keyMissing':
        return Promise.resolve(err(appError('http', 'OPENROUTER_API_KEY is not configured', 503)));
      case 'unbuildable':
        return Promise.resolve(err(appError('http', 'could not run the interview', 422)));
      case 'network':
        return Promise.resolve(err(appError('network', 'bridge server unreachable')));
      default: {
        const next = this.interviewResults.shift();
        return Promise.resolve(
          ok(
            next ?? {
              done: true,
              questions: [],
              productContext: input.productContext ?? '',
              round: input.round,
            },
          ),
        );
      }
    }
  }

  // Override to simulate a fleet-routed run that returns a live runId to poll.
  nextRunStart: RunStartResult = { runId: null, backend: null };

  runCampaign(input: RunInput): Promise<Result<RunStartResult>> {
    if (this.takeFailure()) return this.simulatedFailure();
    this.runRequests.push(input);
    return Promise.resolve(ok(this.nextRunStart));
  }

  stopRun(): Promise<Result<void>> {
    if (this.takeFailure()) return this.simulatedFailure();
    this.stopRequests += 1;
    return Promise.resolve(ok(undefined));
  }

  pauseRun(): Promise<Result<void>> {
    if (this.takeFailure()) return this.simulatedFailure();
    this.pauseRequests += 1;
    return Promise.resolve(ok(undefined));
  }

  resumeRun(): Promise<Result<void>> {
    if (this.takeFailure()) return this.simulatedFailure();
    this.resumeRequests += 1;
    return Promise.resolve(ok(undefined));
  }

  fetchRunActivity(runId: string, afterSeq = 0): Promise<Result<RunActivity>> {
    this.runActivityFetches.push({ runId, afterSeq });
    if (this.runActivity === null) {
      return Promise.resolve(err(appError('http', `unknown run ${runId}`, 404)));
    }
    const activity =
      typeof this.runActivity === 'function'
        ? this.runActivity(runId, afterSeq)
        : this.runActivity;
    return Promise.resolve(ok(activity));
  }

  updateSettings(input: WorkspaceSettingsInput): Promise<Result<void>> {
    if (this.takeFailure()) return this.simulatedFailure();
    this.settingsUpdates.push(input);
    return Promise.resolve(ok(undefined));
  }

  updateOrganization(input: OrganizationInput): Promise<Result<void>> {
    if (this.takeFailure()) return this.simulatedFailure();
    this.orgUpdates.push(input);
    return Promise.resolve(ok(undefined));
  }

  addTeamMember(input: DirectAddInput): Promise<Result<void>> {
    if (this.takeFailure()) return this.simulatedFailure();
    this.teamOps.push({ op: 'add', input });
    return Promise.resolve(ok(undefined));
  }

  createInvite(input: InviteCreateInput): Promise<Result<InviteLink>> {
    if (this.takeFailure()) return Promise.resolve(err(appError('http', 'simulated write failure', 500)));
    this.teamOps.push({ op: 'invite', input });
    return Promise.resolve(ok({ ...this.inviteLink, role: input.role }));
  }

  revokeInvite(id: string): Promise<Result<void>> {
    if (this.takeFailure()) return this.simulatedFailure();
    this.teamOps.push({ op: 'revoke', id });
    return Promise.resolve(ok(undefined));
  }

  updateMemberRole(input: UpdateRoleInput): Promise<Result<void>> {
    if (this.takeFailure()) return this.simulatedFailure();
    this.teamOps.push({ op: 'updateRole', userId: input.userId, input });
    return Promise.resolve(ok(undefined));
  }

  removeMember(userId: number): Promise<Result<void>> {
    if (this.takeFailure()) return this.simulatedFailure();
    this.teamOps.push({ op: 'remove', userId });
    return Promise.resolve(ok(undefined));
  }

  toggleIntegration(integration: Integration, connected: boolean): Promise<Result<void>> {
    if (this.takeFailure()) return this.simulatedFailure();
    this.integrationToggles.push({ id: integration.id, connected });
    return Promise.resolve(ok(undefined));
  }

  connectYouTube(apiKey: string): Promise<Result<void>> {
    if (this.takeFailure()) return this.simulatedFailure();
    this.youtubeConnects.push(apiKey);
    return Promise.resolve(ok(undefined));
  }

  connectReddit(input: RedditConnectInput): Promise<Result<void>> {
    if (this.takeFailure()) return this.simulatedFailure();
    this.redditConnects.push(input);
    return Promise.resolve(ok(undefined));
  }

  startTelegramLogin(phone: string): Promise<Result<TelegramLoginStart>> {
    if (this.takeFailure()) return Promise.resolve(err(appError('http', 'simulated write failure', 500)));
    this.telegramStarts.push(phone);
    return Promise.resolve(ok({ token: 'tg-wizard-token' }));
  }

  verifyTelegramLogin(input: TelegramVerifyInput): Promise<Result<TelegramVerifyResult>> {
    if (this.takeFailure()) return Promise.resolve(err(appError('http', 'simulated write failure', 500)));
    this.telegramVerifies.push(input);
    // Model 2FA: ask for a password once when enabled and none was supplied yet.
    const needsPassword = this.telegramNeedsPassword && !input.password;
    return Promise.resolve(ok({ needsPassword }));
  }

  openCheckout(input: CheckoutInput): Promise<Result<CheckoutSession>> {
    if (this.takeFailure()) return Promise.resolve(err(appError('http', 'simulated write failure', 500)));
    this.checkoutCalls.push(input);
    return Promise.resolve(ok({ checkoutUrl: this.checkoutUrl }));
  }

  openBillingPortal(): Promise<Result<BillingPortal>> {
    if (this.takeFailure()) return Promise.resolve(err(appError('http', 'simulated write failure', 500)));
    this.portalCalls += 1;
    return Promise.resolve(ok({ portalUrl: this.portalUrl, hasAccount: this.hasBillingAccount }));
  }

  // ---- superadmin plane ----

  private takeAdminFailure<T>(): Promise<Result<T>> | null {
    if (!this.failNextAdmin) return null;
    const { message, status } = this.failNextAdmin;
    this.failNextAdmin = null;
    return Promise.resolve(err(appError('http', message, status)));
  }

  /** Void-returning variant (no explicit `<void>` type argument at call sites). */
  private takeAdminFailureVoid(): Promise<Result<void>> | null {
    if (!this.failNextAdmin) return null;
    const { message, status } = this.failNextAdmin;
    this.failNextAdmin = null;
    return Promise.resolve(err(appError('http', message, status)));
  }

  adminLogin(input: AdminLoginInput): Promise<Result<{ id: number; email: string }>> {
    this.adminLoginAttempts.push(input);
    const failure = this.takeAdminFailure<{ id: number; email: string }>();
    if (failure) return failure;
    const admin = { id: 1, email: input.email };
    this.adminSession = {
      id: 1, email: input.email, impersonating: false,
      effectiveOrgId: null, effectiveUserId: null, impersonationReason: null,
    };
    return Promise.resolve(ok(admin));
  }

  adminLogout(): Promise<Result<void>> {
    this.adminLogoutCount += 1;
    this.adminSession = null;
    return Promise.resolve(ok(undefined));
  }

  adminWhoami(): Promise<Result<AdminSession | null>> {
    return Promise.resolve(ok(this.adminSession));
  }

  fetchFleet(): Promise<Result<FleetWorker[]>> {
    return this.takeAdminFailure<FleetWorker[]>() ?? Promise.resolve(ok(this.fleet));
  }

  fetchControlFlags(): Promise<Result<ControlFlag[]>> {
    return this.takeAdminFailure<ControlFlag[]>() ?? Promise.resolve(ok(this.controlFlags));
  }

  setControlFlag(input: ControlFlagSetInput): Promise<Result<void>> {
    const failure = this.takeAdminFailureVoid();
    if (failure) return failure;
    this.controlFlagWrites.push(input);
    return Promise.resolve(ok(undefined));
  }

  revokeWorker(workerId: string): Promise<Result<{ revoked: boolean }>> {
    const failure = this.takeAdminFailure<{ revoked: boolean }>();
    if (failure) return failure;
    this.workerRevokes.push(workerId);
    return Promise.resolve(ok({ revoked: true }));
  }

  enqueueJob(input: EnqueueJobInput): Promise<Result<EnqueuedJob>> {
    const failure = this.takeAdminFailure<EnqueuedJob>();
    if (failure) return failure;
    this.enqueueRequests.push(input);
    return Promise.resolve(ok(this.enqueuedJob));
  }

  fetchAdminOrgs(): Promise<Result<AdminOrg[]>> {
    return this.takeAdminFailure<AdminOrg[]>() ?? Promise.resolve(ok(this.adminOrgs));
  }

  fetchAdminOrgCampaigns(orgId: number): Promise<Result<AdminOrgCampaign[]>> {
    return this.takeAdminFailure<AdminOrgCampaign[]>()
      ?? Promise.resolve(ok(this.adminOrgCampaigns[orgId] ?? []));
  }

  fetchAdminOrgLeads(query: AdminOrgLeadsQuery): Promise<Result<AdminOrgLeadsPage>> {
    const failure = this.takeAdminFailure<AdminOrgLeadsPage>();
    if (failure) return failure;
    const page = this.adminOrgLeads[query.orgId]
      ?? { leads: [], page: query.page, pageSize: query.pageSize, total: 0 };
    return Promise.resolve(ok(page));
  }

  startImpersonation(input: ImpersonateInput): Promise<Result<void>> {
    const failure = this.takeAdminFailureVoid();
    if (failure) return failure;
    this.impersonateRequests.push(input);
    if (this.adminSession) {
      this.adminSession = {
        ...this.adminSession,
        impersonating: true,
        effectiveOrgId: input.orgId ?? this.adminSession.effectiveOrgId,
        effectiveUserId: input.userId ?? null,
        impersonationReason: input.reason,
      };
    }
    return Promise.resolve(ok(undefined));
  }

  endImpersonation(): Promise<Result<void>> {
    this.endImpersonateCount += 1;
    if (this.adminSession) {
      this.adminSession = {
        ...this.adminSession, impersonating: false,
        effectiveOrgId: null, effectiveUserId: null, impersonationReason: null,
      };
    }
    return Promise.resolve(ok(undefined));
  }

  fetchAudit(_limit?: number): Promise<Result<AuditEntry[]>> {
    return this.takeAdminFailure<AuditEntry[]>() ?? Promise.resolve(ok(this.auditEntries));
  }

  verifyAudit(): Promise<Result<AuditVerify>> {
    return this.takeAdminFailure<AuditVerify>() ?? Promise.resolve(ok(this.auditVerifyResult));
  }

  fetchExecutionBackend(): Promise<Result<ExecutionBackendState>> {
    return this.takeAdminFailure<ExecutionBackendState>()
      ?? Promise.resolve(ok({
        backend: this.executionBackend,
        options: ['in_process', 'distributed'],
      }));
  }

  setExecutionBackend(backend: ExecutionBackend): Promise<Result<void>> {
    const failure = this.takeAdminFailureVoid();
    if (failure) return failure;
    this.executionBackendWrites.push(backend);
    this.executionBackend = backend;
    return Promise.resolve(ok(undefined));
  }

  fetchModelComparisonSettings(): Promise<Result<ModelComparisonSettings>> {
    return this.takeAdminFailure<ModelComparisonSettings>()
      ?? Promise.resolve(ok(this.modelComparisonSettings));
  }

  setModelComparisonEnabled(enabled: boolean): Promise<Result<void>> {
    const failure = this.takeAdminFailureVoid();
    if (failure) return failure;
    this.modelComparisonWrites.push(enabled);
    this.modelComparisonSettings = { ...this.modelComparisonSettings, enabled };
    return Promise.resolve(ok(undefined));
  }

  fetchModelComparisonStats(): Promise<Result<ModelComparisonStatsPage>> {
    return this.takeAdminFailure<ModelComparisonStatsPage>()
      ?? Promise.resolve(ok(this.modelComparisonStats));
  }

  private takeFailure(): boolean {
    if (this.failNextWrite) {
      this.failNextWrite = false;
      return true;
    }
    return false;
  }

  private simulatedFailure<T = void>(): Promise<Result<T>> {
    return Promise.resolve(err(appError('http', 'simulated write failure', 500)));
  }
}
