import { appError, err, ok, type Result } from '@/shared/lib/result';
import { z } from 'zod';
import {
  authResponseSchema,
  inviteLinkResponseSchema,
  inviteLookupResponseSchema,
} from '@/shared/schemas/auth';
import {
  generateCampaignResponseSchema,
  interviewResponseSchema,
  panelStateSchema,
  revealLeadResponseSchema,
  runActivityResponseSchema,
  statusWriteResponseSchema,
  telegramLoginStartResponseSchema,
  telegramVerifyResponseSchema,
  writeResponseSchema,
} from '@/shared/schemas/panelState';
import {
  agentLaunchFailureSchema,
  agentNotReadyResponseSchema,
  agentReadinessSchema,
  billingPortalResponseSchema,
  launchAgentLoginResponseSchema,
  runStartResponseSchema,
  campaignsPayloadSchema,
  checkoutSessionResponseSchema,
  dashboardPayloadSchema,
  leadsResponseSchema,
  reportsPayloadSchema,
  settingsPayloadSchema,
} from '@/shared/schemas/endpoints';
import {
  adminLoginResponseSchema,
  adminOrgCampaignsResponseSchema,
  adminOrgLeadsResponseSchema,
  adminOrgRunsResponseSchema,
  adminOrgsResponseSchema,
  adminRunActivityResponseSchema,
  adminWhoamiResponseSchema,
  auditResponseSchema,
  auditVerifyResponseSchema,
  controlFlagsResponseSchema,
  enqueueJobResponseSchema,
  executionBackendResponseSchema,
  fleetEnrolmentTokensResponseSchema,
  fleetResponseSchema,
  mintEnrolmentTokenResponseSchema,
  modelComparisonSettingsResponseSchema,
  modelComparisonStatsResponseSchema,
  revokeEnrolmentTokenResponseSchema,
  revokeWorkerResponseSchema,
  type AdminLoginInput,
  type AdminOrg,
  type AdminOrgCampaign,
  type AdminOrgLeadsPage,
  type AdminOrgLeadsQuery,
  type AdminOrgRun,
  type AdminRunActivity,
  type AdminRunActivityQuery,
  type AdminSession,
  type AuditEntry,
  type AuditVerify,
  type ControlFlag,
  type ControlFlagSetInput,
  type EnqueueJobInput,
  type EnqueuedJob,
  type EnrolmentToken,
  type ExecutionBackend,
  type ExecutionBackendState,
  type FleetWorker,
  type ImpersonateInput,
  type MintEnrolmentTokenInput,
  type MintEnrolmentTokenResult,
  type ModelComparisonSettings,
  type ModelComparisonStatsPage,
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
  RevealLeadInput,
  RevealedLead,
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
import type { PanelRepository } from './panelRepository';

const STATE_ENDPOINT = '/api/state';
const DASHBOARD_ENDPOINT = '/api/dashboard';
const CAMPAIGNS_ENDPOINT = '/api/campaigns';
const LEADS_ENDPOINT = '/api/leads';
const REPORTS_ENDPOINT = '/api/reports';
const STATUS_ENDPOINT = '/api/status';
const STATUS_BULK_ENDPOINT = '/api/status/bulk';
const LEAD_NOTE_ENDPOINT = '/api/lead/note';
const LEAD_REVEAL_ENDPOINT = '/api/lead/reveal';
const CAMPAIGN_ENDPOINT = '/api/campaign';
const CAMPAIGN_GENERATE_ENDPOINT = '/api/campaign/generate';
const CAMPAIGN_INTERVIEW_ENDPOINT = '/api/campaign/interview';
const CAMPAIGN_ARCHIVE_ENDPOINT = '/api/campaign/archive';
const CAMPAIGN_SCHEDULE_ENDPOINT = '/api/campaign/schedule';
const TEAM_ENDPOINT = '/api/team';
const INVITE_ENDPOINT = '/api/invite';
const ORG_ENDPOINT = '/api/org';
const SETTINGS_ENDPOINT = '/api/settings';
const INTEGRATION_ENDPOINT = '/api/integration';
const BILLING_CHECKOUT_ENDPOINT = '/api/billing/checkout';
const BILLING_PORTAL_ENDPOINT = '/api/billing/portal';
const RUN_ENDPOINT = '/api/run';
const RUN_STOP_ENDPOINT = '/api/run/stop';
const RUN_PAUSE_ENDPOINT = '/api/run/pause';
const RUN_RESUME_ENDPOINT = '/api/run/resume';
const RUN_ACTIVITY_ENDPOINT = '/api/run/activity';
const AGENT_READINESS_ENDPOINT = '/api/agent/readiness';
const AGENT_LAUNCH_LOGIN_ENDPOINT = '/api/agent/launch-login';
const SIGNUP_ENDPOINT = '/api/auth/signup';
const LOGIN_ENDPOINT = '/api/auth/login';
const LOGOUT_ENDPOINT = '/api/auth/logout';
const ME_ENDPOINT = '/api/auth/me';
// ---- superadmin plane (separate /api/admin/* branch, its own cookie) ----
const ADMIN_LOGIN_ENDPOINT = '/api/admin/login';
const ADMIN_LOGOUT_ENDPOINT = '/api/admin/logout';
const ADMIN_WHOAMI_ENDPOINT = '/api/admin/whoami';
const ADMIN_FLEET_ENDPOINT = '/api/admin/fleet';
const ADMIN_CONTROL_FLAGS_ENDPOINT = '/api/admin/control-flags';
const ADMIN_WORKER_REVOKE_ENDPOINT = '/api/admin/workers/revoke';
const ADMIN_WORKER_ENROLMENT_TOKENS_ENDPOINT = '/api/admin/worker-enrolment-tokens';
const ADMIN_WORKER_ENROLMENT_TOKEN_REVOKE_ENDPOINT = '/api/admin/worker-enrolment-tokens/revoke';
const ADMIN_ENQUEUE_ENDPOINT = '/api/admin/jobs/enqueue';
const ADMIN_ORGS_ENDPOINT = '/api/admin/orgs';
const ADMIN_IMPERSONATE_ENDPOINT = '/api/admin/impersonate';
const ADMIN_IMPERSONATE_END_ENDPOINT = '/api/admin/impersonate/end';
const ADMIN_AUDIT_ENDPOINT = '/api/admin/audit';
const ADMIN_AUDIT_VERIFY_ENDPOINT = '/api/admin/audit/verify';
const ADMIN_EXECUTION_BACKEND_ENDPOINT = '/api/admin/execution-backend';
const ADMIN_MODEL_COMPARISON_ENDPOINT = '/api/admin/model-comparison';
const ADMIN_MODEL_COMPARISON_STATS_ENDPOINT = '/api/admin/model-comparison/stats';
const ADMIN_RUN_ACTIVITY_ENDPOINT = '/api/admin/run/activity';

// The session lives in an HttpOnly cookie. The panel and bridge are same-origin
// (prod) or proxied same-origin (vite dev), so 'same-origin' sends the cookie
// without opting into credentialed cross-origin (which the bridge does not grant).
const CREDENTIALS: RequestCredentials = 'same-origin';

async function parseJsonSafely(response: Response): Promise<Result<unknown>> {
  try {
    return ok((await response.json()) as unknown);
  } catch {
    return err(appError('http', `unparseable response (HTTP ${response.status})`, response.status));
  }
}

/**
 * Talks to the engine bridge server. Never throws: network, HTTP and
 * shape failures all come back as typed Result errors, and every
 * payload is schema-validated before it crosses into the app.
 */
export class HttpPanelRepository implements PanelRepository {
  constructor(private readonly baseUrl: string = '') {}

  signup(credentials: SignupCredentials): Promise<Result<AuthUser>> {
    return this.authPost(SIGNUP_ENDPOINT, credentials);
  }

  login(credentials: Credentials): Promise<Result<AuthUser>> {
    return this.authPost(LOGIN_ENDPOINT, credentials);
  }

  async getInvite(token: string): Promise<Result<InviteInfo | null>> {
    let response: Response;
    try {
      response = await fetch(
        `${this.baseUrl}${INVITE_ENDPOINT}?token=${encodeURIComponent(token)}`,
        { headers: { Accept: 'application/json' }, credentials: CREDENTIALS },
      );
    } catch (cause) {
      return err(appError('network', `bridge server unreachable: ${describe(cause)}`));
    }
    // 404/400 → the invite is invalid or expired; surface as ok(null), not an error.
    if (response.status === 404 || response.status === 400) return ok(null);
    const json = await parseJsonSafely(response);
    if (!json.ok) return json;
    const parsed = inviteLookupResponseSchema.safeParse(json.value);
    if (!parsed.success || !parsed.data.ok || !parsed.data.data) return ok(null);
    return ok(parsed.data.data);
  }

  logout(): Promise<Result<void>> {
    // The bridge clears the cookie regardless; write() maps the {ok,data,error}
    // envelope and treats a non-ok response as an error.
    return this.write(LOGOUT_ENDPOINT, {});
  }

  async getCurrentUser(): Promise<Result<AuthUser | null>> {
    let response: Response;
    try {
      response = await fetch(this.baseUrl + ME_ENDPOINT, {
        headers: { Accept: 'application/json' },
        credentials: CREDENTIALS,
      });
    } catch (cause) {
      return err(appError('network', `bridge server unreachable: ${describe(cause)}`));
    }
    // 401 is the normal "not signed in" answer, not a failure — resolve to null.
    if (response.status === 401) return ok(null);
    const json = await parseJsonSafely(response);
    if (!json.ok) return json;
    const parsed = authResponseSchema.safeParse(json.value);
    if (!parsed.success) {
      return response.ok
        ? err(appError('validation', 'auth response shape mismatch'))
        : err(appError('http', `auth check failed (HTTP ${response.status})`, response.status));
    }
    if (!response.ok || !parsed.data.ok || !parsed.data.data) {
      return ok(null);
    }
    return ok(parsed.data.data.user);
  }

  /** Shared never-throw POST for the auth endpoints that return {ok,data:{user},error}. */
  private async authPost(
    endpoint: string,
    body: Credentials | SignupCredentials,
  ): Promise<Result<AuthUser>> {
    let response: Response;
    try {
      response = await fetch(this.baseUrl + endpoint, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body),
        credentials: CREDENTIALS,
      });
    } catch (cause) {
      return err(appError('network', `bridge server unreachable: ${describe(cause)}`));
    }
    const json = await parseJsonSafely(response);
    if (!json.ok) return json;
    const parsed = authResponseSchema.safeParse(json.value);
    if (!parsed.success) {
      return response.ok
        ? err(appError('validation', 'auth response shape mismatch'))
        : err(appError('http', `auth request failed (HTTP ${response.status})`, response.status));
    }
    if (!response.ok || !parsed.data.ok || !parsed.data.data) {
      return err(appError('http', parsed.data.error ?? `HTTP ${response.status}`, response.status));
    }
    return ok(parsed.data.data.user);
  }

  async fetchState(campaignId?: string | null): Promise<Result<PanelState>> {
    // A selected campaign scopes the whole payload; absent → engine default.
    const url = campaignId
      ? `${STATE_ENDPOINT}?campaign=${encodeURIComponent(campaignId)}`
      : STATE_ENDPOINT;
    return this.getRaw(url, panelStateSchema, 'state');
  }

  fetchDashboard(): Promise<Result<DashboardPayload>> {
    return this.getRaw(DASHBOARD_ENDPOINT, dashboardPayloadSchema, 'dashboard');
  }

  fetchCampaigns(): Promise<Result<CampaignsPayload>> {
    return this.getRaw(CAMPAIGNS_ENDPOINT, campaignsPayloadSchema, 'campaigns');
  }

  fetchReports(): Promise<Result<ReportsPayload>> {
    return this.getRaw(REPORTS_ENDPOINT, reportsPayloadSchema, 'reports');
  }

  fetchSettings(): Promise<Result<SettingsPayload>> {
    return this.getRaw(SETTINGS_ENDPOINT, settingsPayloadSchema, 'settings');
  }

  async fetchLeads(query: LeadsQuery): Promise<Result<LeadsPayload>> {
    // /api/leads is enveloped {ok,data,error} (pagination metadata has no top-level
    // record key), unlike the other reads which return a raw keys dict.
    const url = `${this.baseUrl}${LEADS_ENDPOINT}?${leadsQueryString(query)}`;
    let response: Response;
    try {
      response = await fetch(url, { headers: { Accept: 'application/json' }, credentials: CREDENTIALS });
    } catch (cause) {
      return err(appError('network', `bridge server unreachable: ${describe(cause)}`));
    }
    const json = await parseJsonSafely(response);
    if (!json.ok) return json;
    const parsed = leadsResponseSchema.safeParse(json.value);
    if (!parsed.success) {
      return response.ok
        ? err(appError('validation', 'leads payload shape mismatch'))
        : err(appError('http', `leads request failed (HTTP ${response.status})`, response.status));
    }
    if (!response.ok || !parsed.data.ok || parsed.data.data === null) {
      return err(appError('http', parsed.data.error ?? `HTTP ${response.status}`, response.status));
    }
    return ok(parsed.data.data);
  }

  /**
   * Shared never-throw GET for the raw-keys-dict reads (/api/state and the per-page
   * endpoints except leads). Validates the whole body against `schema`; network, HTTP
   * and shape failures all become typed Result errors. `label` names the surface in
   * error messages.
   */
  private async getRaw<S extends z.ZodTypeAny>(
    path: string, schema: S, label: string): Promise<Result<z.infer<S>>> {
    let response: Response;
    try {
      response = await fetch(this.baseUrl + path, {
        headers: { Accept: 'application/json' },
        credentials: CREDENTIALS,
      });
    } catch (cause) {
      return err(appError('network', `bridge server unreachable: ${describe(cause)}`));
    }
    if (!response.ok) {
      return err(appError('http', `${label} request failed (HTTP ${response.status})`, response.status));
    }
    const json = await parseJsonSafely(response);
    if (!json.ok) return json;
    const parsed = schema.safeParse(json.value);
    if (!parsed.success) {
      return err(appError('validation',
        `${label} payload shape mismatch: ${parsed.error.issues[0]?.message ?? 'unknown'}`));
    }
    return ok(parsed.data);
  }

  async setMatchStatus(request: StatusWriteRequest): Promise<Result<void>> {
    let response: Response;
    try {
      response = await fetch(this.baseUrl + STATUS_ENDPOINT, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(request),
        credentials: CREDENTIALS,
      });
    } catch (cause) {
      return err(appError('network', `bridge server unreachable: ${describe(cause)}`));
    }
    const json = await parseJsonSafely(response);
    if (!json.ok) return json;

    const parsed = statusWriteResponseSchema.safeParse(json.value);
    if (!parsed.success) {
      // A gateway/proxy error body won't match the envelope — report the
      // HTTP status (the useful signal) rather than a shape mismatch.
      return response.ok
        ? err(appError('validation', 'status response shape mismatch'))
        : err(appError('http', `status write failed (HTTP ${response.status})`, response.status));
    }
    if (!response.ok || !parsed.data.ok) {
      return err(appError('http', parsed.data.error ?? `HTTP ${response.status}`, response.status));
    }
    return ok(undefined);
  }

  bulkSetStatus(request: BulkStatusRequest): Promise<Result<void>> {
    return this.write(STATUS_BULK_ENDPOINT, request);
  }

  /**
   * One audited un-redaction. A plain POST that RESOLVES a value — never a cached
   * query: the answer is session-local, so it belongs in the calling component's
   * state and nowhere else (see `PanelRepository.revealLead`). Every call is a fresh
   * request on purpose, because every call is a fresh audit row.
   *
   * 403 (viewer) and 404 (unknown/foreign lead) come back as typed http errors with
   * their status, so the drawer can distinguish "you may not" from "it is gone".
   */
  revealLead(input: RevealLeadInput): Promise<Result<RevealedLead>> {
    return this.postForData<RevealedLead>(LEAD_REVEAL_ENDPOINT, input, revealLeadResponseSchema);
  }

  addLeadNote(input: AddLeadNoteInput): Promise<Result<void>> {
    return this.write(LEAD_NOTE_ENDPOINT, { op: 'create', ...input });
  }

  deleteLeadNote(input: DeleteLeadNoteInput): Promise<Result<void>> {
    // The server's note ids are integers; the panel carries them as strings.
    return this.write(LEAD_NOTE_ENDPOINT, { op: 'delete', noteId: Number(input.noteId) });
  }

  createCampaign(input: CampaignInput): Promise<Result<void>> {
    return this.write(CAMPAIGN_ENDPOINT, input);
  }

  archiveCampaign(input: ArchiveCampaignInput): Promise<Result<void>> {
    // 200 ok → ok(void); 404 (unknown/cross-org) / 400 → appError('http', msg, status).
    return this.write(CAMPAIGN_ARCHIVE_ENDPOINT, input);
  }

  scheduleCampaign(input: ScheduleCampaignInput): Promise<Result<void>> {
    return this.write(CAMPAIGN_SCHEDULE_ENDPOINT, input);
  }

  generateCampaign(input: GenerateCampaignInput): Promise<Result<GeneratedCampaignDraft>> {
    // postForData inherits the typed network/http/validation boundary; the
    // tolerant schema degrades ragged AI output to a partial prefill.
    return this.postForData(CAMPAIGN_GENERATE_ENDPOINT, input, generateCampaignResponseSchema);
  }

  runInterview(input: InterviewRequest): Promise<Result<InterviewResponse>> {
    // Same typed boundary as generate; the tolerant schema degrades a ragged
    // interview reply to "no questions" (→ the wizard finishes) rather than throwing.
    return this.postForData(CAMPAIGN_INTERVIEW_ENDPOINT, input, interviewResponseSchema);
  }

  async runCampaign(input: RunInput): Promise<Result<RunStartResult>> {
    let response: Response;
    try {
      response = await fetch(this.baseUrl + RUN_ENDPOINT, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(input),
        credentials: CREDENTIALS,
      });
    } catch (cause) {
      return err(appError('network', `bridge server unreachable: ${describe(cause)}`));
    }
    const json = await parseJsonSafely(response);
    if (!json.ok) return json;

    // The agent-not-ready gate answers 409 with its OWN shape (not the {ok,data,error}
    // envelope every other run-control write uses) so it can carry the full readiness
    // snapshot. Check for it before the generic parse below, and tag the error with
    // `code: 'agent_not_ready'` so the drawer can point admins at the fix banner rather
    // than showing a generic run-conflict message.
    if (response.status === 409) {
      const gate = agentNotReadyResponseSchema.safeParse(json.value);
      if (gate.success) {
        return err(appError('http', gate.data.detail, 409, gate.data.error));
      }
    }

    // postForData's usual envelope: 202 → ok(data) and other 409s (e.g. the single-run
    // lock)/400 → appError('http', msg, status). A fleet run's data carries
    // runId/backend; an in-process run's data has neither, so the tolerant schema
    // coerces both to null (the drawer then falls back to the RUN block). data is never
    // server-side null on a 202, so this never mis-reads as error.
    const parsed = runStartResponseSchema.safeParse(json.value);
    if (!parsed.success) {
      return response.ok
        ? err(appError('validation', 'response shape mismatch'))
        : err(appError('http', `request failed (HTTP ${response.status})`, response.status));
    }
    if (!response.ok || !parsed.data.ok || parsed.data.data === null) {
      return err(appError('http', parsed.data.error ?? `HTTP ${response.status}`, response.status));
    }
    return ok(parsed.data.data);
  }

  stopRun(): Promise<Result<void>> {
    // 200 ok → ok(void); 409 (nothing active) → appError('http', msg, 409).
    return this.write(RUN_STOP_ENDPOINT, {});
  }

  pauseRun(): Promise<Result<void>> {
    // 200 ok (idempotent re-pause too); 409 when nothing of yours is active.
    return this.write(RUN_PAUSE_ENDPOINT, {});
  }

  resumeRun(): Promise<Result<void>> {
    return this.write(RUN_RESUME_ENDPOINT, {});
  }

  async fetchRunActivity(runId: string, afterSeq = 0): Promise<Result<RunActivity>> {
    const url =
      `${this.baseUrl}${RUN_ACTIVITY_ENDPOINT}` +
      `?runId=${encodeURIComponent(runId)}&after=${encodeURIComponent(String(afterSeq))}`;
    let response: Response;
    try {
      response = await fetch(url, {
        headers: { Accept: 'application/json' },
        credentials: CREDENTIALS,
      });
    } catch (cause) {
      return err(appError('network', `bridge server unreachable: ${describe(cause)}`));
    }
    const json = await parseJsonSafely(response);
    if (!json.ok) return json;

    const parsed = runActivityResponseSchema.safeParse(json.value);
    if (!parsed.success) {
      // A gateway/proxy error body won't match the envelope — report the HTTP
      // status (404 unknown run, 401/403 access) rather than a shape mismatch.
      return response.ok
        ? err(appError('validation', 'run activity response shape mismatch'))
        : err(appError('http', `run activity failed (HTTP ${response.status})`, response.status));
    }
    if (!response.ok || !parsed.data.ok || parsed.data.data === null) {
      return err(appError('http', parsed.data.error ?? `HTTP ${response.status}`, response.status));
    }
    return ok(parsed.data.data);
  }

  getAgentReadiness(opts?: AgentReadinessOptions): Promise<Result<AgentReadiness>> {
    // URLSearchParams rather than string concatenation: campaignId is org-scoped user
    // data ("o1.q4-outbound") and goes in a query string, so it gets encoded, not
    // interpolated. The server ownership-checks the id before it narrows anything.
    const params = new URLSearchParams();
    if (opts?.refresh) params.set('refresh', '1');
    if (opts?.campaignId) params.set('campaign', opts.campaignId);
    const query = params.toString();
    const path = query ? `${AGENT_READINESS_ENDPOINT}?${query}` : AGENT_READINESS_ENDPOINT;
    return this.getRaw(path, agentReadinessSchema, 'agent readiness');
  }

  async launchAgentLogin(): Promise<Result<LaunchAgentLoginResult>> {
    let response: Response;
    try {
      response = await fetch(this.baseUrl + AGENT_LAUNCH_LOGIN_ENDPOINT, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({}),
        credentials: CREDENTIALS,
      });
    } catch (cause) {
      return err(appError('network', `bridge server unreachable: ${describe(cause)}`));
    }
    const json = await parseJsonSafely(response);
    if (!json.ok) return json;

    if (!response.ok) {
      // 500 carries {error, detail} when Chrome itself couldn't be started; a 403
      // (not owner/admin) has no such body — fall back to a generic HTTP message.
      const failure = agentLaunchFailureSchema.safeParse(json.value);
      const message = failure.success ? failure.data.detail : `launch failed (HTTP ${response.status})`;
      return err(appError('http', message, response.status));
    }
    const parsed = launchAgentLoginResponseSchema.safeParse(json.value);
    if (!parsed.success) {
      return err(appError('validation', 'launch-login response shape mismatch'));
    }
    return ok(parsed.data);
  }

  updateSettings(input: WorkspaceSettingsInput): Promise<Result<void>> {
    return this.write(SETTINGS_ENDPOINT, input);
  }

  updateOrganization(input: OrganizationInput): Promise<Result<void>> {
    return this.write(ORG_ENDPOINT, input);
  }

  addTeamMember(input: DirectAddInput): Promise<Result<void>> {
    return this.write(TEAM_ENDPOINT, { op: 'create', ...input });
  }

  createInvite(input: InviteCreateInput): Promise<Result<InviteLink>> {
    return this.postForData(INVITE_ENDPOINT, { op: 'create', ...input },
      inviteLinkResponseSchema);
  }

  revokeInvite(id: string): Promise<Result<void>> {
    return this.write(INVITE_ENDPOINT, { op: 'revoke', id });
  }

  updateMemberRole(input: UpdateRoleInput): Promise<Result<void>> {
    return this.write(TEAM_ENDPOINT, { op: 'updateRole', ...input });
  }

  removeMember(userId: number): Promise<Result<void>> {
    return this.write(TEAM_ENDPOINT, { op: 'remove', userId });
  }

  toggleIntegration(integration: Integration, connected: boolean): Promise<Result<void>> {
    return this.write(INTEGRATION_ENDPOINT, { platform: integration.platform, connected });
  }

  connectYouTube(apiKey: string): Promise<Result<void>> {
    return this.write(INTEGRATION_ENDPOINT, { platform: 'youtube', apiKey });
  }

  connectReddit(input: RedditConnectInput): Promise<Result<void>> {
    return this.write(INTEGRATION_ENDPOINT, { platform: 'reddit', ...input });
  }

  startTelegramLogin(phone: string): Promise<Result<TelegramLoginStart>> {
    return this.postForData(`${INTEGRATION_ENDPOINT}/telegram/start`, { phone },
      telegramLoginStartResponseSchema);
  }

  verifyTelegramLogin(input: TelegramVerifyInput): Promise<Result<TelegramVerifyResult>> {
    return this.postForData(`${INTEGRATION_ENDPOINT}/telegram/verify`, input,
      telegramVerifyResponseSchema);
  }

  openCheckout(input: CheckoutInput): Promise<Result<CheckoutSession>> {
    return this.postForData(BILLING_CHECKOUT_ENDPOINT, input, checkoutSessionResponseSchema);
  }

  openBillingPortal(): Promise<Result<BillingPortal>> {
    return this.postForData(BILLING_PORTAL_ENDPOINT, {}, billingPortalResponseSchema);
  }

  // ---- superadmin plane (separate /api/admin/* branch) --------------------

  async adminLogin(input: AdminLoginInput): Promise<Result<{ id: number; email: string }>> {
    const result = await this.postForData<{ admin: { id: number; email: string } }>(
      ADMIN_LOGIN_ENDPOINT, input, adminLoginResponseSchema);
    return result.ok ? ok(result.value.admin) : result;
  }

  adminLogout(): Promise<Result<void>> {
    return this.write(ADMIN_LOGOUT_ENDPOINT, {});
  }

  async adminWhoami(): Promise<Result<AdminSession | null>> {
    let response: Response;
    try {
      response = await fetch(this.baseUrl + ADMIN_WHOAMI_ENDPOINT, {
        headers: { Accept: 'application/json' },
        credentials: CREDENTIALS,
      });
    } catch (cause) {
      return err(appError('network', `bridge server unreachable: ${describe(cause)}`));
    }
    // 401 (no session) and 403 (IP not allowlisted) both mean "no admin here" —
    // resolve to null so the guard sends the visitor to the admin login, not an error.
    if (response.status === 401 || response.status === 403) return ok(null);
    const json = await parseJsonSafely(response);
    if (!json.ok) return json;
    const parsed = adminWhoamiResponseSchema.safeParse(json.value);
    if (!parsed.success) {
      return response.ok
        ? err(appError('validation', 'admin whoami shape mismatch'))
        : err(appError('http', `admin whoami failed (HTTP ${response.status})`, response.status));
    }
    if (!response.ok || !parsed.data.ok || !parsed.data.data) return ok(null);
    return ok(parsed.data.data.admin);
  }

  async fetchFleet(): Promise<Result<FleetWorker[]>> {
    const result = await this.adminGet<{ workers: FleetWorker[] }>(
      ADMIN_FLEET_ENDPOINT, fleetResponseSchema, 'fleet');
    return result.ok ? ok(result.value.workers) : result;
  }

  async fetchControlFlags(): Promise<Result<ControlFlag[]>> {
    const result = await this.adminGet<{ flags: ControlFlag[] }>(
      ADMIN_CONTROL_FLAGS_ENDPOINT, controlFlagsResponseSchema, 'control-flags');
    return result.ok ? ok(result.value.flags) : result;
  }

  setControlFlag(input: ControlFlagSetInput): Promise<Result<void>> {
    return this.write(ADMIN_CONTROL_FLAGS_ENDPOINT, input);
  }

  revokeWorker(workerId: string): Promise<Result<{ revoked: boolean }>> {
    return this.postForData(ADMIN_WORKER_REVOKE_ENDPOINT, { workerId }, revokeWorkerResponseSchema);
  }

  mintEnrolmentToken(input: MintEnrolmentTokenInput): Promise<Result<MintEnrolmentTokenResult>> {
    return this.postForData(
      ADMIN_WORKER_ENROLMENT_TOKENS_ENDPOINT, input, mintEnrolmentTokenResponseSchema);
  }

  async fetchEnrolmentTokens(): Promise<Result<EnrolmentToken[]>> {
    const result = await this.adminGet<{ tokens: EnrolmentToken[] }>(
      ADMIN_WORKER_ENROLMENT_TOKENS_ENDPOINT, fleetEnrolmentTokensResponseSchema,
      'enrolment-tokens');
    return result.ok ? ok(result.value.tokens) : result;
  }

  revokeEnrolmentToken(tokenId: string): Promise<Result<{ revoked: boolean }>> {
    return this.postForData(
      ADMIN_WORKER_ENROLMENT_TOKEN_REVOKE_ENDPOINT, { tokenId }, revokeEnrolmentTokenResponseSchema);
  }

  async enqueueJob(input: EnqueueJobInput): Promise<Result<EnqueuedJob>> {
    const result = await this.postForData<{ job: EnqueuedJob }>(
      ADMIN_ENQUEUE_ENDPOINT, input, enqueueJobResponseSchema);
    return result.ok ? ok(result.value.job) : result;
  }

  async fetchAdminOrgs(): Promise<Result<AdminOrg[]>> {
    const result = await this.adminGet<{ orgs: AdminOrg[] }>(
      ADMIN_ORGS_ENDPOINT, adminOrgsResponseSchema, 'admin-orgs');
    return result.ok ? ok(result.value.orgs) : result;
  }

  async fetchAdminOrgCampaigns(orgId: number): Promise<Result<AdminOrgCampaign[]>> {
    const path = `${ADMIN_ORGS_ENDPOINT}/${encodeURIComponent(String(orgId))}/campaigns`;
    const result = await this.adminGet<{ campaigns: AdminOrgCampaign[] }>(
      path, adminOrgCampaignsResponseSchema, 'admin-org-campaigns');
    return result.ok ? ok(result.value.campaigns) : result;
  }

  fetchAdminOrgLeads(query: AdminOrgLeadsQuery): Promise<Result<AdminOrgLeadsPage>> {
    const path =
      `${ADMIN_ORGS_ENDPOINT}/${encodeURIComponent(String(query.orgId))}/leads` +
      `?${adminLeadsQueryString(query)}`;
    return this.adminGet<AdminOrgLeadsPage>(path, adminOrgLeadsResponseSchema, 'admin-org-leads');
  }

  async fetchAdminOrgRuns(orgId: number): Promise<Result<AdminOrgRun[]>> {
    const path = `${ADMIN_ORGS_ENDPOINT}/${encodeURIComponent(String(orgId))}/runs`;
    const result = await this.adminGet<{ runs: AdminOrgRun[] }>(
      path, adminOrgRunsResponseSchema, 'admin-org-runs');
    return result.ok ? ok(result.value.runs) : result;
  }

  fetchAdminRunActivity(query: AdminRunActivityQuery): Promise<Result<AdminRunActivity>> {
    // `after` is the global event-id cursor (0 = from the start of the run), not a seq.
    const path =
      `${ADMIN_RUN_ACTIVITY_ENDPOINT}?runId=${encodeURIComponent(query.runId)}` +
      `&after=${encodeURIComponent(String(query.after ?? 0))}`;
    return this.adminGet<AdminRunActivity>(
      path, adminRunActivityResponseSchema, 'admin-run-activity');
  }

  startImpersonation(input: ImpersonateInput): Promise<Result<void>> {
    return this.write(ADMIN_IMPERSONATE_ENDPOINT, input);
  }

  endImpersonation(): Promise<Result<void>> {
    return this.write(ADMIN_IMPERSONATE_END_ENDPOINT, {});
  }

  async fetchAudit(limit?: number): Promise<Result<AuditEntry[]>> {
    const path = limit === undefined
      ? ADMIN_AUDIT_ENDPOINT
      : `${ADMIN_AUDIT_ENDPOINT}?limit=${encodeURIComponent(String(limit))}`;
    const result = await this.adminGet<{ entries: AuditEntry[] }>(
      path, auditResponseSchema, 'admin-audit');
    return result.ok ? ok(result.value.entries) : result;
  }

  verifyAudit(): Promise<Result<AuditVerify>> {
    return this.adminGet<AuditVerify>(ADMIN_AUDIT_VERIFY_ENDPOINT, auditVerifyResponseSchema,
      'admin-audit-verify');
  }

  fetchExecutionBackend(): Promise<Result<ExecutionBackendState>> {
    return this.adminGet<ExecutionBackendState>(
      ADMIN_EXECUTION_BACKEND_ENDPOINT, executionBackendResponseSchema, 'execution-backend');
  }

  setExecutionBackend(backend: ExecutionBackend): Promise<Result<void>> {
    return this.write(ADMIN_EXECUTION_BACKEND_ENDPOINT, { backend });
  }

  fetchModelComparisonSettings(): Promise<Result<ModelComparisonSettings>> {
    return this.adminGet<ModelComparisonSettings>(
      ADMIN_MODEL_COMPARISON_ENDPOINT, modelComparisonSettingsResponseSchema,
      'model-comparison');
  }

  setModelComparisonEnabled(enabled: boolean): Promise<Result<void>> {
    return this.write(ADMIN_MODEL_COMPARISON_ENDPOINT, { enabled });
  }

  fetchModelComparisonStats(): Promise<Result<ModelComparisonStatsPage>> {
    return this.adminGet<ModelComparisonStatsPage>(
      ADMIN_MODEL_COMPARISON_STATS_ENDPOINT, modelComparisonStatsResponseSchema,
      'model-comparison-stats');
  }

  /**
   * Shared never-throw GET for the enveloped ({ok,data,error}) admin reads.
   * Returns the validated `data` payload; network/HTTP/shape failures all become
   * typed Result errors. A gateway/proxy error body that doesn't match the
   * envelope is reported by its HTTP status (the useful signal), not a shape miss.
   */
  private async adminGet<T>(
    path: string, schema: z.ZodTypeAny, label: string): Promise<Result<T>> {
    let response: Response;
    try {
      response = await fetch(this.baseUrl + path, {
        headers: { Accept: 'application/json' },
        credentials: CREDENTIALS,
      });
    } catch (cause) {
      return err(appError('network', `bridge server unreachable: ${describe(cause)}`));
    }
    const json = await parseJsonSafely(response);
    if (!json.ok) return json;
    const parsed = schema.safeParse(json.value) as
      { success: true; data: { ok: boolean; data: T | null; error: string | null } }
      | { success: false };
    if (!parsed.success) {
      return response.ok
        ? err(appError('validation', `${label} response shape mismatch`))
        : err(appError('http', `${label} request failed (HTTP ${response.status})`, response.status));
    }
    if (!response.ok || !parsed.data.ok || parsed.data.data === null) {
      return err(appError('http', parsed.data.error ?? `HTTP ${response.status}`, response.status));
    }
    return ok(parsed.data.data);
  }

  /** Shared never-throw POST for the {ok,data,error}-envelope write endpoints. */
  private async write(endpoint: string, body: unknown): Promise<Result<void>> {
    let response: Response;
    try {
      response = await fetch(this.baseUrl + endpoint, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body),
        credentials: CREDENTIALS,
      });
    } catch (cause) {
      return err(appError('network', `bridge server unreachable: ${describe(cause)}`));
    }
    const json = await parseJsonSafely(response);
    if (!json.ok) return json;

    const parsed = writeResponseSchema.safeParse(json.value);
    if (!parsed.success) {
      return response.ok
        ? err(appError('validation', 'write response shape mismatch'))
        : err(appError('http', `write failed (HTTP ${response.status})`, response.status));
    }
    if (!response.ok || !parsed.data.ok) {
      return err(appError('http', parsed.data.error ?? `HTTP ${response.status}`, response.status));
    }
    return ok(undefined);
  }

  /** Like write(), but returns the validated `data` payload (e.g. the invite link). */
  private async postForData<T>(
    endpoint: string,
    body: unknown,
    schema: z.ZodTypeAny,
  ): Promise<Result<T>> {
    let response: Response;
    try {
      response = await fetch(this.baseUrl + endpoint, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body),
        credentials: CREDENTIALS,
      });
    } catch (cause) {
      return err(appError('network', `bridge server unreachable: ${describe(cause)}`));
    }
    const json = await parseJsonSafely(response);
    if (!json.ok) return json;
    const parsed = schema.safeParse(json.value) as
      { success: true; data: { ok: boolean; data: T | null; error: string | null } }
      | { success: false };
    if (!parsed.success) {
      return response.ok
        ? err(appError('validation', 'response shape mismatch'))
        : err(appError('http', `request failed (HTTP ${response.status})`, response.status));
    }
    if (!response.ok || !parsed.data.ok || parsed.data.data === null) {
      return err(appError('http', parsed.data.error ?? `HTTP ${response.status}`, response.status));
    }
    return ok(parsed.data.data);
  }
}

function describe(cause: unknown): string {
  return cause instanceof Error ? cause.message : String(cause);
}

/** Build the /api/leads query string, omitting blank filters so they don't narrow. */
function leadsQueryString(query: LeadsQuery): string {
  const params = new URLSearchParams({
    page: String(query.page),
    pageSize: String(query.pageSize),
  });
  if (query.q) params.set('q', query.q);
  if (query.status) params.set('status', query.status);
  if (query.platform) params.set('platform', query.platform);
  if (query.sort) params.set('sort', query.sort);
  if (query.dir) params.set('dir', query.dir);
  return params.toString();
}

/** Build the /api/admin/orgs/{id}/leads query string, omitting blank filters. */
function adminLeadsQueryString(query: AdminOrgLeadsQuery): string {
  const params = new URLSearchParams({
    page: String(query.page),
    pageSize: String(query.pageSize),
  });
  if (query.q) params.set('q', query.q);
  if (query.status) params.set('status', query.status);
  if (query.platform) params.set('platform', query.platform);
  if (query.campaign) params.set('campaign', query.campaign);
  if (query.sort) params.set('sort', query.sort);
  if (query.dir) params.set('dir', query.dir);
  return params.toString();
}
