import type { z } from 'zod';
import type { Role } from '@/shared/auth/roles';
// Type-only import (erased at build → no runtime cycle with the form module).
import type { CampaignDraft } from '@/features/campaigns/useCampaignForm';
import type {
  authUserSchema,
  inviteLinkSchema,
  inviteLookupSchema,
} from '@/shared/schemas/auth';
import type {
  alertSchema,
  inviteSchema,
  campaignBriefFormSchema,
  campaignSchema,
  channelEntrySchema,
  channelDatumSchema,
  dashboardPeriodSchema,
  dashboardSchema,
  escalationEntrySchema,
  funnelSchema,
  healthSchema,
  integrationSchema,
  billingSchema,
  billingTierSchema,
  leadNoteSchema,
  matchSchema,
  matchStatusSchema,
  statusChangeSchema,
  panelConfigSchema,
  panelStateSchema,
  platformSummarySchema,
  reelSchema,
  warmthSchema,
  reportsPeriodSchema,
  reportsSchema,
  activeRunSchema,
  runActivitySchema,
  runBlockSchema,
  runCountersSchema,
  runEventLevelSchema,
  runEventSchema,
  runFlagSchema,
  fleetJobSchema,
  runModeSchema,
  runRecordSchema,
  runScopeSchema,
  sessionSchema,
  soulSchema,
  teamMemberSchema,
  tickerEntrySchema,
  topCampaignSchema,
} from '@/shared/schemas/panelState';
import type {
  agentReadinessSchema,
  campaignsPayloadSchema,
  dashboardPayloadSchema,
  launchAgentLoginResponseSchema,
  leadStatsSchema,
  leadsPayloadSchema,
  reportsPayloadSchema,
  settingsPayloadSchema,
} from '@/shared/schemas/endpoints';

export type MatchStatus = z.infer<typeof matchStatusSchema>;
export type StatusChange = z.infer<typeof statusChangeSchema>;
export type LeadNote = z.infer<typeof leadNoteSchema>;

/** The signed-in panel user (id, email, role, org). */
export type AuthUser = z.infer<typeof authUserSchema>;

/** Email + password sent to the login endpoint. */
export interface Credentials {
  readonly email: string;
  readonly password: string;
}

/**
 * Signup is one of two flows: with `inviteToken` you JOIN an existing company
 * (company fields ignored); without one you CREATE a company (name required).
 */
export interface SignupCredentials {
  readonly email: string;
  readonly password: string;
  readonly companyName?: string;
  readonly companyLogo?: string;
  readonly companyDescription?: string;
  readonly inviteToken?: string;
}

/** Public invite-landing info (GET /api/invite?token=…). */
export type InviteInfo = z.infer<typeof inviteLookupSchema>;
/** The shareable invite link returned once by POST /api/invite (create). */
export type InviteLink = z.infer<typeof inviteLinkSchema>;

export type PanelConfig = z.infer<typeof panelConfigSchema>;
export type Campaign = z.infer<typeof campaignSchema>;
export type Warmth = z.infer<typeof warmthSchema>;
export type CampaignBriefForm = z.infer<typeof campaignBriefFormSchema>;
/** One channel on the WIRE (seed arrays). The form layer uses ChannelFormEntry
 * (comma strings) — see useCampaignForm. */
export type ChannelEntry = z.infer<typeof channelEntrySchema>;
export type Session = z.infer<typeof sessionSchema>;
export type Reel = z.infer<typeof reelSchema>;
export type Match = z.infer<typeof matchSchema>;
export type EscalationEntry = z.infer<typeof escalationEntrySchema>;
export type Alert = z.infer<typeof alertSchema>;
export type Health = z.infer<typeof healthSchema>;
export type Soul = z.infer<typeof soulSchema>;
export type PlatformSummary = z.infer<typeof platformSummarySchema>;
export type PanelState = z.infer<typeof panelStateSchema>;

// v3 inferred types
export type Dashboard = z.infer<typeof dashboardSchema>;
export type DashboardPeriod = z.infer<typeof dashboardPeriodSchema>;
export type ChannelDatum = z.infer<typeof channelDatumSchema>;
export type Funnel = z.infer<typeof funnelSchema>;
export type TopCampaign = z.infer<typeof topCampaignSchema>;
export type TickerEntry = z.infer<typeof tickerEntrySchema>;
export type Reports = z.infer<typeof reportsSchema>;
export type ReportsPeriod = z.infer<typeof reportsPeriodSchema>;
export type TeamMember = z.infer<typeof teamMemberSchema>;
export type Invite = z.infer<typeof inviteSchema>;
export type Integration = z.infer<typeof integrationSchema>;
export type Billing = z.infer<typeof billingSchema>;
export type BillingTier = z.infer<typeof billingTierSchema>;

/** Interval for a paid subscription. Cap is interval-independent — only price differs. */
export type BillingInterval = 'month' | 'year';
/** Client input for a self-serve checkout (Scale is sales-led, never checked out). */
export interface CheckoutInput {
  readonly tier: string;
  readonly interval: BillingInterval;
}
/** POST /api/billing/checkout result — the hosted-checkout URL to redirect to. */
export interface CheckoutSession {
  readonly checkoutUrl: string;
}
/** POST /api/billing/portal result — portal URL + whether the org has a Polar customer. */
export interface BillingPortal {
  readonly portalUrl: string;
  readonly hasAccount: boolean;
}

// Per-page endpoint payloads (org-wide). Each is a slice of the old PanelState shape.
export type DashboardPayload = z.infer<typeof dashboardPayloadSchema>;
export type CampaignsPayload = z.infer<typeof campaignsPayloadSchema>;
export type LeadStats = z.infer<typeof leadStatsSchema>;
export type LeadsPayload = z.infer<typeof leadsPayloadSchema>;
export type ReportsPayload = z.infer<typeof reportsPayloadSchema>;
export type SettingsPayload = z.infer<typeof settingsPayloadSchema>;

/** GET /api/agent/readiness — whether the Instagram warmed-browser agent can run. */
export type AgentReadiness = z.infer<typeof agentReadinessSchema>;
/** POST /api/agent/launch-login result: whether a browser was (re)launched, plus the
 * freshly re-checked readiness (no separate refetch needed after a launch attempt). */
export type LaunchAgentLoginResult = z.infer<typeof launchAgentLoginResponseSchema>;
/** Options for getAgentReadiness: `refresh` forces a live probe past the ≤60s cache. */
export interface AgentReadinessOptions {
  readonly refresh?: boolean;
}

/** Server-side leads query: filters/sort live on the server (org-wide + paginated). */
export interface LeadsQuery {
  readonly page: number;
  readonly pageSize: number;
  readonly q?: string;
  readonly status?: MatchStatus;
  readonly platform?: string;
  /** A campaign id to scope the whole page to, or absent for all campaigns. */
  readonly campaign?: string;
  readonly sort?: 'capturedAt' | 'score' | 'username' | 'platform' | 'status';
  readonly dir?: 'asc' | 'desc';
}

// Run control plane — enums are the single source of truth for the vocabulary.
export type RunMode = z.infer<typeof runModeSchema>;
export type RunScope = z.infer<typeof runScopeSchema>;
export type ActiveRun = z.infer<typeof activeRunSchema>;
export type RunRecord = z.infer<typeof runRecordSchema>;
export type RunBlock = z.infer<typeof runBlockSchema>;

// Live run activity feed (GET /api/run/activity).
export type RunEventLevel = z.infer<typeof runEventLevelSchema>;
export type RunEvent = z.infer<typeof runEventSchema>;
export type RunCounters = z.infer<typeof runCountersSchema>;
export type RunFlag = z.infer<typeof runFlagSchema>;
export type FleetJob = z.infer<typeof fleetJobSchema>;
export type RunActivity = z.infer<typeof runActivitySchema>;

export type DashboardPeriodKey = 'today' | 'week' | 'month';

/* ---- per-org connection flows (Telegram MTProto login wizard) ---- */

/** What POST /api/integration/telegram/start returns: a short-lived wizard token
 * (the pending login lives server-side). */
export interface TelegramLoginStart {
  readonly token: string;
}

/** Body for POST /api/integration/telegram/verify: the code from the start step,
 * plus the 2FA password when the account has one. */
export interface TelegramVerifyInput {
  readonly token: string;
  readonly code: string;
  readonly password?: string;
}

/** Verify outcome: `needsPassword` means 2FA is on — resubmit with the password.
 * Otherwise the login succeeded and the session is stored. */
export interface TelegramVerifyResult {
  readonly needsPassword: boolean;
}

/** Body for the Reddit connect (POST /api/integration with platform=reddit): the
 * app's client_credentials trio. Validated server-side by minting an app token. */
export interface RedditConnectInput {
  readonly clientId: string;
  readonly clientSecret: string;
  readonly userAgent: string;
}

export interface StatusWriteRequest {
  readonly campaignId: string;
  readonly commentId: string;
  readonly status: MatchStatus;
  // The match's source platform, so the bridge marks the right row when a
  // campaign spans platforms. Optional; the server defaults to instagram.
  readonly platform?: string;
  // Reason note — required by the server when moving into a terminal status
  // (closed/couldnt_connect/archived); recorded on the status-change audit row.
  readonly note?: string;
}

/* ---- v6 lead-note write shapes (POST /api/lead/note) ---- */

export interface AddLeadNoteInput {
  readonly campaignId: string;
  readonly commentId: string;
  readonly platform?: string;
  readonly body: string;
}

export interface DeleteLeadNoteInput {
  readonly noteId: string;
}

/* ---- v3 write-input shapes (sent to the bridge write endpoints) ---- */

export interface BulkStatusItem {
  readonly commentId: string;
  readonly platform?: string;
}

export interface BulkStatusRequest {
  readonly campaignId: string;
  readonly status: MatchStatus;
  readonly items: readonly BulkStatusItem[];
  // One shared reason for the whole bulk action — required by the server when the
  // target is a terminal status (closed/couldnt_connect/archived). Recorded on
  // every affected lead's status-change audit row.
  readonly note?: string;
}

export interface CampaignInput {
  readonly campaignId: string;
  readonly displayName?: string;
  readonly status?: string;
  readonly budgetCap?: number;
  readonly goalTarget?: number;
  // The matching brief (camelCase). When present, the bridge persists it and the
  // campaign becomes runnable via `aizu run --campaign <id>`.
  readonly brief?: CampaignBriefForm;
}

/** Sent to POST /api/campaign/archive — archive (true) or un-archive (false). */
export interface ArchiveCampaignInput {
  readonly campaignId: string;
  readonly archived: boolean;
}

/** A fixed-cadence schedule. dow (0=Mon..6=Sun) is required only for 'weekly'. */
export type ScheduleKind = 'daily' | 'weekdays' | 'weekly';

/** Sent to POST /api/campaign/schedule. enabled=false clears the schedule (the
 * cadence fields are then ignored). The server computes the next fire. */
export interface ScheduleCampaignInput {
  readonly campaignId: string;
  readonly enabled: boolean;
  readonly kind?: ScheduleKind;
  readonly dow?: number;
  readonly hour?: number;
  readonly minute?: number;
  readonly tz?: string;
  readonly targetLeads?: number;
  readonly durationMinutes?: number;
}

/**
 * Sent to POST /api/campaign/generate. All optional; the caller must supply at
 * least one. The bridge turns a description, a landing-page URL and/or a product
 * screenshot into a drafted campaign.
 */
export interface GenerateCampaignInput {
  readonly url?: string;
  /** Product screenshot as a `data:` base64 URL (from the dropzone). */
  readonly imageB64?: string;
  readonly text?: string;
  /** Optional existing campaign id to refine, rather than draft a fresh one. */
  readonly campaignIdHint?: string;
  /** Serialized product context from the interview (skips a refetch). */
  readonly productContext?: string;
  /** The clarifying-interview transcript — answers refine the synthesized brief. */
  readonly interview?: readonly InterviewAnswer[];
  /** The platforms the client chose in the interview (→ primary + channels[]). */
  readonly platforms?: readonly string[];
}

/** The shape of one clarifying-interview question (POST /api/campaign/interview). */
export type InterviewQuestionType = 'single' | 'multi' | 'text' | 'platforms';

export interface InterviewOption {
  readonly value: string;
  readonly label: string;
  readonly hint?: string;
}

export interface InterviewQuestion {
  readonly id: string;
  readonly type: InterviewQuestionType;
  readonly prompt: string;
  readonly help?: string;
  /** For single/multi: the choices to render. */
  readonly options?: readonly InterviewOption[];
  /** For platforms: the AI's recommended platform slugs (user can add more). */
  readonly suggested?: readonly string[];
  /** For text: an example answer. */
  readonly placeholder?: string;
  /** For single/multi: render an "Other" free-text alongside the options. */
  readonly allowCustom?: boolean;
}

/** One answered question in the running transcript echoed back each round. */
export interface InterviewAnswer {
  readonly question: string;
  readonly answer: string;
}

/**
 * Sent to POST /api/campaign/interview. The first round carries url/imageB64/text;
 * later rounds carry the echoed `productContext` plus the running `interview`
 * transcript and the incremented `round`.
 */
export interface InterviewRequest {
  readonly url?: string;
  readonly imageB64?: string;
  readonly text?: string;
  readonly productContext?: string;
  readonly interview?: readonly InterviewAnswer[];
  readonly round: number;
}

/** One round's reply: the next questions (or done) + the serialized context. */
export interface InterviewResponse {
  readonly done: boolean;
  readonly questions: readonly InterviewQuestion[];
  readonly productContext: string;
  readonly round: number;
}

/**
 * The AI-drafted prefill returned by generate — a FLAT, partial CampaignFormState
 * (FORM keys, arrays comma-joined) that spreads straight into the create form.
 * Aliased to CampaignDraft so the form hook and the contract stay one type.
 */
export type GeneratedCampaignDraft = CampaignDraft;

/**
 * Sent to POST /api/run. Exactly one of `campaignId` / `all` identifies the
 * scope; `mode` defaults safe-side to 'dry' at the call site.
 */
export interface RunInput {
  readonly campaignId?: string;
  readonly all?: boolean;
  readonly mode: RunMode;
  // Live lead target: the engine keeps running back-to-back discovery sessions
  // until it has found this many leads (matched comments), then stops.
  readonly targetLeadCount?: number;
  // Optional wall-clock safety cap in minutes: stop once it elapses even if the
  // lead target hasn't been reached. Omit for no cap (target/9pm window only).
  readonly durationMinutes?: number;
}

/** What POST /api/run returns once accepted. `runId` is set for a fleet-routed live run
 * (so the drawer can poll its live activity feed); in-process runs surface via the RUN
 * block instead and leave it null. */
export interface RunStartResult {
  readonly runId: string | null;
  readonly backend: string | null;
}

export interface WorkspaceSettingsInput {
  readonly settings: Readonly<Record<string, unknown>>;
}

/** Direct-add a teammate with a password set by the inviter (POST /api/team create). */
export interface DirectAddInput {
  readonly email: string;
  readonly password: string;
  readonly role: Role;
}

/** Create a shareable invite link (POST /api/invite create). */
export interface InviteCreateInput {
  readonly role: Role;
  readonly email?: string;
}

/** Change a teammate's role (POST /api/team updateRole). */
export interface UpdateRoleInput {
  readonly userId: number;
  readonly role: Role;
}

/** Edit the company profile (POST /api/org). */
export interface OrganizationInput {
  readonly name?: string;
  readonly logo?: string;
  readonly description?: string;
}
