/**
 * Static, deterministic seed data for demo-capture mode (`npm run dev:demo`).
 *
 * Everything here is fixed — no `Date.now()`, no `Math.random()` — so a
 * Playwright capture driven off this state is byte-for-byte reproducible run
 * to run. Built entirely from the existing test fixture builders
 * (`src/test/fixtures.ts`) so the shapes can never drift from what the real
 * repository/schemas expect; this module only supplies the *values*.
 *
 * The story this data has to tell: one live, six-platform campaign with 12
 * `interested` leads (~2/platform), a healthy funnel, and a small team.
 */
import type { CampaignDraft } from '@/features/campaigns/useCampaignForm';
import { blankChannel } from '@/features/campaigns/useCampaignForm';
import type {
  AuthUser,
  Billing,
  Campaign,
  ChannelEntry,
  InterviewResponse,
  Match,
  MatchStatus,
  PanelState,
  RunCounters,
  RunPhase,
  StatusChange,
  TeamMember,
} from '@/shared/types/domain';
import {
  buildBilling,
  buildCampaign,
  buildDashboardPeriod,
  buildIntegration,
  buildLeadNote,
  buildMatch,
  buildPanelState,
  buildReportsPeriod,
  buildTeamMember,
} from '@/test/fixtures';

/** The six platforms the demo campaign fans out to, in the engine's real
 *  sequential run order (CLAUDE.md: "multi-platform, sequential"). */
export const DEMO_PLATFORMS: readonly string[] = [
  'instagram', 'linkedin', 'x', 'youtube', 'reddit', 'telegram',
];

const DEMO_ORG = {
  id: 1,
  name: 'Acme Inc.',
  logo: null,
  description: 'Support-automation SaaS for teams drowning in Zendesk tickets.',
};

// ---- team ----

export const DEMO_TEAM: readonly TeamMember[] = [
  buildTeamMember({
    id: 't-1', userId: 1, name: 'Priya Shah', email: 'priya@acme.io',
    role: 'owner', initials: 'PS', createdAt: 1_753_500_000,
  }),
  buildTeamMember({
    id: 't-2', userId: 2, name: 'Marcus Lee', email: 'marcus@acme.io',
    role: 'admin', initials: 'ML', createdAt: 1_753_600_000,
  }),
  buildTeamMember({
    id: 't-3', userId: 3, name: 'Jordan Blake', email: 'jordan@acme.io',
    role: 'member', initials: 'JB', createdAt: 1_753_700_000,
  }),
  buildTeamMember({
    id: 't-4', userId: 4, name: 'Elena Cruz', email: 'elena@acme.io',
    role: 'viewer', initials: 'EC', createdAt: 1_753_800_000,
  }),
];

const OWNER_EMAIL = 'priya@acme.io';
const ADMIN_EMAIL = 'marcus@acme.io';
const MEMBER_EMAIL = 'jordan@acme.io';

/** The signed-in demo user — boots already authenticated as the org owner. */
export const DEMO_USER: AuthUser = {
  id: 1, email: OWNER_EMAIL, role: 'owner', orgId: 1, org: DEMO_ORG,
};

// ---- the live, six-platform campaign ----

const DEMO_CHANNELS: readonly ChannelEntry[] = [
  { platform: 'instagram', seedHashtags: ['supportautomation', 'saas'], seedAccounts: ['acme.io'], seedChannels: [] },
  { platform: 'linkedin', seedHashtags: ['saas', 'customersupport'], seedAccounts: ['company/acme'], seedChannels: [] },
  { platform: 'x', seedHashtags: ['saas', 'supportautomation'], seedAccounts: ['acme'], seedChannels: [] },
  { platform: 'youtube', seedHashtags: ['best help desk software'], seedAccounts: [], seedChannels: [] },
  { platform: 'reddit', seedHashtags: ['support automation'], seedAccounts: [], seedChannels: ['r/SaaS', 'r/CustomerService'] },
  { platform: 'telegram', seedHashtags: [], seedAccounts: [], seedChannels: ['@saas_chat', '@support_ops'] },
];

export const DEMO_CAMPAIGN_ID = 'cmp-001';

export const DEMO_CAMPAIGN: Campaign = buildCampaign({
  id: DEMO_CAMPAIGN_ID,
  name: 'Acme SaaS Lead Gen',
  status: 'live',
  threshold: 0.7,
  languages: ['en'],
  extractFields: ['phone', 'email', 'intent'],
  startedAt: 'Aug 1',
  brief: 'Surface purchase intent for Acme, a support-automation SaaS, across six platforms.',
  platform: 'instagram',
  platforms: [...DEMO_PLATFORMS],
  budgetCap: 60,
  goalTarget: 50,
  briefForm: {
    platform: 'instagram',
    goal: 'lead',
    threshold: 0.7,
    languageMix: ['en'],
    relevanceDef: 'Posts or comments from people evaluating or complaining about customer-support '
      + 'tooling, ticket volume, or Zendesk/Intercom alternatives.',
    matchDef: 'Explicit buying intent — asking about pricing, requesting a demo, comparing tools, '
      + 'or describing a support backlog they want fixed.',
    extractDef: 'phone, email, and the stated intent (pricing, demo, trial, buy, or inquire).',
    relevancePrompt: '',
    matchPrompt: '',
    visionPrompt: '',
    seedHashtags: ['supportautomation', 'saas'],
    seedAccounts: ['acme.io'],
    seedChannels: [],
    channels: [...DEMO_CHANNELS],
  },
  spent: 24.8,
  leads: 22,
  cpl: 1.13,
  // Everything this campaign found also reached the account — the healthy pairing.
  // The demo deliberately never shows the `not_delivered` state: it is an honest
  // warning about a real failure, not a feature to put on camera.
  leadsFound: 22,
  leadsDelivered: 22,
  delivery: 'delivered',
  spark: [1, 2, 3, 2, 4, 3, 5, 4, 6, 5, 7, 6, 8, 22],
  warmth: {
    score: 88,
    state: 'full',
    gateMin: 40,
    gateFull: 70,
    meetsGate: true,
    components: { age: 0.9, ramp: 0.9, network: 0.85, profile: 1, trust: 0.9 },
    trend: [60, 68, 74, 80, 84, 88],
    etaHours: null,
    checkedAt: 'Aug 4',
  },
  archivedAt: null,
  pausedReason: null,
  scheduleEnabled: false,
  fleetRunId: null,
});

/** A second, still-draft campaign for list realism — has a full brief so
 *  Activate/Edit both demo cleanly, but hasn't been warmed up yet. */
export const DEMO_DRAFT_CAMPAIGN: Campaign = buildCampaign({
  id: 'cmp-002',
  name: 'Acme Outbound — LinkedIn ABM',
  status: 'draft',
  threshold: 0.65,
  languages: ['en'],
  extractFields: ['email', 'intent'],
  startedAt: '—',
  brief: 'Account-based outreach signal on LinkedIn for target accounts evaluating support tooling.',
  platform: 'linkedin',
  platforms: ['linkedin'],
  budgetCap: 20,
  goalTarget: 40,
  briefForm: {
    platform: 'linkedin',
    goal: 'lead',
    threshold: 0.65,
    languageMix: ['en'],
    relevanceDef: 'Posts from people at target accounts discussing support operations or tooling.',
    matchDef: 'Explicit interest in evaluating or switching support-automation vendors.',
    extractDef: 'email and stated intent.',
    relevancePrompt: '',
    matchPrompt: '',
    visionPrompt: '',
    seedHashtags: ['customersupport', 'saas'],
    seedAccounts: ['company/acme'],
    seedChannels: [],
  },
  spent: 0,
  leads: 0,
  cpl: null,
  leadsFound: 0,
  leadsDelivered: 0,
  delivery: 'delivered',
  spark: [],
  warmth: {
    score: 32,
    state: 'warming',
    gateMin: 40,
    gateFull: 70,
    meetsGate: false,
    components: { age: 0.3, ramp: 0.2, network: 0.3, profile: 0.5, trust: 0.3 },
    trend: [10, 18, 24, 28, 32],
    etaHours: 36,
    checkedAt: 'Aug 4',
  },
  archivedAt: null,
  pausedReason: null,
  scheduleEnabled: false,
  fleetRunId: null,
});

// ---- leads (MATCHES) ----

/**
 * v27: a demo lead carries NO handle and NO comment body — the demo tenant is a
 * customer-facing surface, where a lead is anonymous until someone asks for the handle,
 * and the comment body is never shown at all.
 * `intent` is the whole story of a lead here: one plain-language line saying what the
 * person wants, with no name, no @handle and no phone/e-mail in it (those are
 * `extracted` fields, which the drawer shows as contact chips).
 */
interface LeadSeed {
  readonly id: string;
  readonly platform: string;
  readonly intent: string;
  readonly score: number;
  readonly reason: string;
  readonly status: MatchStatus;
  readonly extracted?: Readonly<Record<string, unknown>>;
  readonly time: string;
  readonly note?: string;
}

// One entry per lead, in the same platform order the run walks
// (instagram → linkedin → x → youtube → reddit → telegram). 12 interested
// (~2/platform, scores 0.72–0.94, the 0.91 instagram row is the hero lead), 8 new
// (mixed, some below the 0.7 threshold), 2 in_progress.
const LEAD_SEEDS: readonly LeadSeed[] = [
  // instagram
  {
    id: 'ig-dana', platform: 'instagram', status: 'interested', score: 0.91,
    intent: 'Wants a demo and Pro-plan pricing — drowning in Zendesk tickets and needs '
      + 'something that scales',
    reason: 'asks for a demo and pricing, names the exact pain (ticket volume)',
    extracted: { email: 'dana@northwindtech.com', phone: '+14155550142', intent: 'demo' },
    time: '08:12', note: 'Called, left a voicemail — following up tomorrow.',
  },
  {
    id: 'ig-marcus', platform: 'instagram', status: 'interested', score: 0.84,
    intent: 'Comparing support-automation tools; asking what onboarding looks like for a '
      + '40-person team',
    reason: 'actively evaluating tools, gives team size', extracted: { intent: 'inquire' }, time: '09:05',
  },
  {
    id: 'ig-jess', platform: 'instagram', status: 'new', score: 0.65,
    intent: 'Following the account for SaaS tips — no buying signal yet',
    reason: 'low buying-intent, general engagement', time: '09:40',
  },
  {
    id: 'ig-alex', platform: 'instagram', status: 'new', score: 0.62,
    intent: 'Support queue is out of control lately and they may start looking at tooling',
    reason: 'pain signal but no explicit ask yet', time: '10:02',
  },
  // linkedin
  {
    id: 'li-sarah', platform: 'linkedin', status: 'interested', score: 0.88,
    intent: 'Evaluating support-automation platforms for a 60-person CS team; asking whether '
      + 'it integrates with their existing Zendesk data',
    reason: 'specific integration question from a named buyer role', extracted: { email: 'sarah.chen@brightloop.io', intent: 'inquire' },
    time: '10:20',
  },
  {
    id: 'li-raj', platform: 'linkedin', status: 'interested', score: 0.79,
    intent: 'Wants a trial — their ticket backlog has tripled this quarter',
    reason: 'explicit trial request with quantified pain', extracted: { intent: 'trial' }, time: '10:47',
  },
  {
    id: 'li-mike', platform: 'linkedin', status: 'new', score: 0.71,
    intent: 'Curious how the product compares to Intercom',
    reason: 'comparison curiosity, early-stage', time: '11:10',
  },
  {
    id: 'li-helen', platform: 'linkedin', status: 'in_progress', score: 0.70,
    intent: 'Asking for pricing for a mid-size team and wants someone to reach out',
    reason: 'pricing ask — being worked by the team', time: '11:30',
  },
  // x
  {
    id: 'x-kate', platform: 'x', status: 'interested', score: 0.93,
    intent: 'Ready to switch off their current stack; asking whether the API supports webhook '
      + 'triggers for ticket escalation',
    reason: 'technical fit question plus explicit intent to switch', extracted: { email: 'kate@devopsco.dev', intent: 'buy' },
    time: '11:58',
  },
  {
    id: 'x-leo', platform: 'x', status: 'interested', score: 0.76,
    intent: 'Has been evaluating for weeks and wants to buy — waiting on budget sign-off',
    reason: 'buying intent pending internal approval', extracted: { intent: 'buy' }, time: '12:15',
  },
  {
    id: 'x-quinn', platform: 'x', status: 'new', score: 0.58,
    intent: 'Venting about an overloaded support inbox — no stated intent',
    reason: 'vague pain, no explicit intent', time: '12:40',
  },
  {
    id: 'x-bea', platform: 'x', status: 'new', score: 0.75,
    intent: 'Asking which pricing tier fits a 10-person startup',
    reason: 'pricing question, above threshold, awaiting triage', time: '13:02',
  },
  // youtube
  {
    id: 'yt-amy', platform: 'youtube', status: 'interested', score: 0.82,
    intent: 'Asking whether it integrates with their helpdesk stack; may feature it in an '
      + 'upcoming review',
    reason: 'integration question from a reviewer with reach', extracted: { email: 'amy@reviewhub.com', intent: 'inquire' },
    time: '13:25',
  },
  {
    id: 'yt-saas', platform: 'youtube', status: 'interested', score: 0.72,
    intent: 'Signed up for the trial after watching the walkthrough',
    reason: 'self-reported trial signup', extracted: { intent: 'trial' }, time: '13:50',
  },
  {
    id: 'yt-bob', platform: 'youtube', status: 'new', score: 0.68,
    intent: 'Liked the demo and is keeping the product on their radar',
    reason: 'positive but non-committal', time: '14:10',
  },
  // reddit
  {
    id: 'rd-pm', platform: 'reddit', status: 'interested', score: 0.94,
    intent: 'Migrating off Zendesk next quarter and wants a demo as soon as possible',
    reason: 'active migration timeline plus explicit demo ask', extracted: { email: 'pm.throwaway88@protonmail.com', intent: 'demo' },
    time: '14:35',
  },
  {
    id: 'rd-sam', platform: 'reddit', status: 'interested', score: 0.77,
    intent: 'A 15-person startup drowning in support tickets, asking whether the product is '
      + 'overkill for their size',
    reason: 'qualifying fit, real quantified pain', extracted: { intent: 'inquire' }, time: '14:58',
  },
  {
    id: 'rd-dev', platform: 'reddit', status: 'new', score: 0.73,
    intent: 'Asking how the scoring model handles multi-language support threads',
    reason: 'technical curiosity, above threshold', time: '15:20',
  },
  {
    id: 'rd-eval', platform: 'reddit', status: 'in_progress', score: 0.81,
    intent: 'Shortlisted the product in a three-vendor evaluation and wants a call booked',
    reason: 'active vendor evaluation — being worked', time: '15:45',
  },
  // telegram
  {
    id: 'tg-ops', platform: 'telegram', status: 'interested', score: 0.86,
    intent: 'Asking for team-plan pricing — an agency managing support for five client accounts',
    reason: 'explicit pricing ask, agency use case', extracted: { email: 'ops@ninjacollective.io', intent: 'pricing' },
    time: '16:05',
  },
  {
    id: 'tg-jen', platform: 'telegram', status: 'interested', score: 0.80,
    intent: 'Referred internally by their support lead; wants a demo for a 20-person team',
    reason: 'internal referral plus demo interest', extracted: { intent: 'demo' }, time: '16:30',
  },
  {
    id: 'tg-newbie', platform: 'telegram', status: 'new', score: 0.55,
    intent: 'Just joined the channel and is asking what the group is about',
    reason: 'no buying signal, off-topic', time: '16:50',
  },
];

const DEMO_DATE = 'Aug 4';
const TS_BASE = 1_754_300_000; // fixed, arbitrary — deterministic only
const TS_STEP_SEC = 300;

/** Deterministic status-change trail matching each lead's final status. */
function historyFor(status: MatchStatus, atTs: number): readonly StatusChange[] {
  if (status === 'new') return [];
  if (status === 'in_progress') {
    return [
      { fromStatus: 'new', toStatus: 'in_progress', by: ADMIN_EMAIL, at: DEMO_DATE, atTs: atTs - 300, note: null },
    ];
  }
  // interested
  return [
    { fromStatus: 'new', toStatus: 'in_progress', by: ADMIN_EMAIL, at: DEMO_DATE, atTs: atTs - 600, note: null },
    { fromStatus: 'in_progress', toStatus: 'interested', by: MEMBER_EMAIL, at: DEMO_DATE, atTs: atTs - 120, note: null },
  ];
}

export const DEMO_MATCHES: readonly Match[] = LEAD_SEEDS.map((seed, index) => {
  const ts = TS_BASE + index * TS_STEP_SEC;
  return buildMatch({
    // No explicit `id` — buildMatch derives the composite lead identity.
    commentId: seed.id,
    campaignId: DEMO_CAMPAIGN_ID,
    platform: seed.platform,
    sessionId: null,
    intent: seed.intent,
    lang: 'en',
    score: seed.score,
    reason: seed.reason,
    extracted: seed.extracted ?? {},
    status: seed.status,
    escalated: false,
    escalationCost: 0,
    capturedAt: { date: DEMO_DATE, time: seed.time, ts },
    statusBy: seed.status === 'new' ? null : ADMIN_EMAIL,
    statusAt: seed.status === 'new' ? null : DEMO_DATE,
    statusHistory: [...historyFor(seed.status, ts)],
    notes: seed.note
      ? [buildLeadNote({
        id: `note-${seed.id}`, body: seed.note, authorEmail: ADMIN_EMAIL, authorId: 2,
        createdAt: DEMO_DATE, createdAtTs: ts - 60,
      })]
      : [],
  });
});

// ---- integrations (all six connected) ----

export const DEMO_INTEGRATIONS = [
  buildIntegration({ id: 'instagram', platform: 'instagram', name: 'Instagram', connected: true, detail: 'Connected — warmed session active' }),
  buildIntegration({ id: 'linkedin', platform: 'linkedin', name: 'LinkedIn', connected: true, detail: 'Connected — warmed session active' }),
  buildIntegration({ id: 'x', platform: 'x', name: 'X', connected: true, detail: 'Connected — warmed session active' }),
  buildIntegration({ id: 'youtube', platform: 'youtube', name: 'Youtube', connected: true, detail: 'Connected via API key' }),
  buildIntegration({ id: 'reddit', platform: 'reddit', name: 'Reddit', connected: true, detail: 'Connected via app credentials' }),
  buildIntegration({ id: 'telegram', platform: 'telegram', name: 'Telegram', connected: true, detail: 'Connected — @acme_scout' }),
];

// ---- dashboard + reports ----

const PLATFORM_LEAD_COUNTS: readonly { readonly platform: string; readonly current: number; readonly previous: number }[] = [
  { platform: 'instagram', current: 4, previous: 3 },
  { platform: 'linkedin', current: 4, previous: 2 },
  { platform: 'x', current: 4, previous: 3 },
  { platform: 'youtube', current: 3, previous: 2 },
  { platform: 'reddit', current: 4, previous: 3 },
  { platform: 'telegram', current: 3, previous: 1 },
];

const TOTAL_LEADS = DEMO_MATCHES.length; // 22
const INTERESTED_LEADS = DEMO_MATCHES.filter((m) => m.status === 'interested').length; // 12

const DEMO_DASHBOARD_PERIOD = buildDashboardPeriod({
  leads: { value: TOTAL_LEADS, delta: '+38%', spark: [3, 5, 4, 7, 6, 9, 8, 10, 9, 12, 14, 18, 20, 22] },
  goal: { target: 50, current: TOTAL_LEADS, pct: Math.round((TOTAL_LEADS / 50) * 100) },
  cpl: { value: 1.13, history: [1.4, 1.3, 1.25, 1.2, 1.18, 1.15, 1.13] },
  conversion: { value: INTERESTED_LEADS / TOTAL_LEADS, delta: '+12%' },
  channels: PLATFORM_LEAD_COUNTS.map((c) => ({ platform: c.platform, current: c.current, previous: c.previous })),
  funnel: { reels: 480, relevant: 210, scored: 340, matches: TOTAL_LEADS },
  bestHour: Array.from({ length: 24 }, (_, h) => (h >= 8 && h <= 17 ? 4 : h >= 18 && h <= 20 ? 1 : 0)),
  activeCampaigns: 1,
  topCampaigns: [
    { id: DEMO_CAMPAIGN_ID, name: DEMO_CAMPAIGN.name, platform: 'instagram', status: 'live',
      leads: TOTAL_LEADS, cpl: 1.13,
      leadsFound: TOTAL_LEADS, leadsDelivered: TOTAL_LEADS, delivery: 'delivered' },
  ],
  // The ticker names what each lead WANTS. The server truncates `intent` to a
  // glance-width line; these are already written short, so they ride through as-is.
  ticker: [...DEMO_MATCHES]
    .sort((a, b) => b.capturedAt.ts - a.capturedAt.ts)
    .slice(0, 6)
    .map((m) => ({
      id: m.id, intent: m.intent, platform: m.platform, score: m.score,
      capturedAt: { date: m.capturedAt.date, time: m.capturedAt.time },
    })),
});

export const DEMO_DASHBOARD = {
  today: DEMO_DASHBOARD_PERIOD,
  week: DEMO_DASHBOARD_PERIOD,
  month: DEMO_DASHBOARD_PERIOD,
};

const DEMO_REPORTS_PERIOD = buildReportsPeriod({
  labels: ['Aug 1', 'Aug 2', 'Aug 3', 'Aug 4'],
  matchesByPlatform: PLATFORM_LEAD_COUNTS.map((c) => ({
    platform: c.platform,
    values: [Math.max(0, c.current - 3), Math.max(0, c.current - 2), Math.max(0, c.current - 1), c.current],
  })),
  cplTrend: [1.4, 1.3, 1.2, 1.13],
  spendByStage: [
    { name: 'match', value: 9.6 },
    { name: 'vision', value: 8.4 },
    { name: 'escalation', value: 6.8 },
  ],
  platformRanking: PLATFORM_LEAD_COUNTS.map((c) => ({ platform: c.platform, leads: c.current })),
  perCampaign: [
    { id: DEMO_CAMPAIGN_ID, name: DEMO_CAMPAIGN.name, status: 'live', leads: TOTAL_LEADS, cpl: 1.13, spend: 24.8,
      leadsFound: TOTAL_LEADS, leadsDelivered: TOTAL_LEADS, delivery: 'delivered' },
    { id: DEMO_DRAFT_CAMPAIGN.id, name: DEMO_DRAFT_CAMPAIGN.name, status: 'draft', leads: 0, cpl: null, spend: 0,
      leadsFound: 0, leadsDelivered: 0, delivery: 'delivered' },
  ],
});

export const DEMO_REPORTS = {
  today: DEMO_REPORTS_PERIOD,
  week: DEMO_REPORTS_PERIOD,
  month: DEMO_REPORTS_PERIOD,
};

// ---- billing (the plan the demo tenant is on) ----

/**
 * A LITE subscription, chosen so the v27 plan affordances are live on camera instead
 * of dead code:
 *   - 2 of 3 campaigns used → the campaigns page shows a real "2 of 3" meter AND the
 *     New Campaign button still works, so the wizard capture runs to completion and
 *     ends by flipping the counter to 3 of 3 (the at-cap state, earned rather than
 *     staged). A Free tenant (cap 1) would boot already at its cap with the wizard's
 *     entry point disabled.
 *   - 22 of 50 leads used → a partly-full usage meter, and `maxRunLeads: 50` gives the
 *     run drawer a real bound to name ("Lite plan: up to 50 leads per run").
 * `tiers` comes from the shared catalogue so the comparison grid stays in step with
 * `billing.TIERS`.
 */
export const DEMO_BILLING: Billing = buildBilling({
  tier: 'lite',
  interval: 'month',
  status: 'active',
  periodEnd: 1_756_900_000,
  cancelAtPeriodEnd: false,
  leadCap: 50,
  leadsUsed: TOTAL_LEADS,
  campaignCap: 3,
  campaignsUsed: 2,
  revealCap: 50,
  revealsUsed: 0,
  maxRunLeads: 50,
  usageRatio: TOTAL_LEADS / 50,
  nearLimit: false,
});

// ---- full panel state ----

export const DEMO_PANEL_STATE: PanelState = buildPanelState({
  CONFIG: {
    productName: 'AIZU',
    todayLabel: 'Aug 4, 2026',
    timezone: 'Asia/Tashkent (UTC+5)',
    matchThreshold: 0.7,
    skipRatioThreshold: 0.6,
    budgetCapUsd: 80,
    canaryLimitReels: 5,
    watchlistTtlDays: 10,
    pacing: {
      sessionsPerDay: '1–2',
      sessionLength: '15–30 min',
      reelsPerSession: '20–40',
      dwell: '3–30 s',
      betweenReels: '2–8 s',
      window: 'Daytime only',
    },
    role: 'owner',
    organization: DEMO_ORG,
  },
  CAMPAIGNS: [DEMO_CAMPAIGN, DEMO_DRAFT_CAMPAIGN],
  MATCHES: [...DEMO_MATCHES],
  ALERTS: [],
  DASHBOARD: DEMO_DASHBOARD,
  REPORTS: DEMO_REPORTS,
  TEAM: [...DEMO_TEAM],
  INVITES: [],
  INTEGRATIONS: DEMO_INTEGRATIONS,
  RUN: { active: null, recent: [] },
});

// ---- AI wizard seam: one interview round + the synthesized draft ----

const DEMO_PRODUCT_CONTEXT =
  'Acme is a support-automation SaaS that helps teams tame their Zendesk/Intercom ticket backlog '
  + 'with AI-assisted triage and macros.';

/** Round 1: a single `platforms`-type question with all six pre-suggested — the
 *  most visually complete question type CampaignInterview renders. Answering
 *  it and clicking Continue drives a round-2 call that the repo's default
 *  (empty-queue) behavior resolves as `done`, so synthesis proceeds. */
export const DEMO_INTERVIEW_ROUND1: InterviewResponse = {
  done: false,
  round: 1,
  productContext: DEMO_PRODUCT_CONTEXT,
  questions: [
    {
      id: 'platforms-1',
      type: 'platforms',
      prompt: 'Which platforms should Aizu watch for buying signals?',
      help: 'Recommended for a support-automation SaaS with both technical and business buyers.',
      suggested: [...DEMO_PLATFORMS],
    },
  ],
};

/** The AI-synthesized draft — a full CampaignReview prefill, mirroring the
 *  already-live campaign so the wizard's story pays off ("one brief, one
 *  run"). Keys are the flat FORM keys `useCampaignForm` reads. */
export const DEMO_GENERATED_DRAFT: CampaignDraft = {
  name: 'Acme SaaS Lead Gen',
  objective: 'lead',
  budgetCap: 7500,
  goalTarget: 200,
  platform: 'instagram',
  threshold: 0.7,
  languages: 'en',
  relevanceDef: 'Posts or comments from people evaluating or complaining about customer-support '
    + 'tooling, ticket volume, or Zendesk/Intercom alternatives.',
  matchDef: 'Explicit buying intent — asking about pricing, requesting a demo, comparing tools, or '
    + 'describing a support backlog they want fixed.',
  extractDef: 'phone, email, and the stated intent (pricing, demo, trial, buy, or inquire).',
  relevancePrompt: '',
  matchPrompt: '',
  visionPrompt: '',
  seedHashtags: 'supportautomation, saas',
  seedAccounts: 'acme.io',
  seedChannels: '',
  channels: [
    { ...blankChannel('instagram'), seedHashtags: 'supportautomation, saas', seedAccounts: 'acme.io' },
    { ...blankChannel('linkedin'), seedHashtags: 'saas, customersupport', seedAccounts: 'company/acme' },
    { ...blankChannel('x'), seedHashtags: 'saas, supportautomation', seedAccounts: 'acme' },
    { ...blankChannel('youtube'), seedHashtags: 'best help desk software' },
    { ...blankChannel('reddit'), seedHashtags: 'support automation', seedChannels: 'r/SaaS, r/CustomerService' },
    { ...blankChannel('telegram'), seedChannels: '@saas_chat, @support_ops' },
  ],
};

// ---- live run progress replay ----

export const DEMO_RUN_ID = 'run-demo-001';
export const DEMO_RUN_STARTED_AT = '2026-08-05T08:00:00Z';

/**
 * The lead target the scripted run was started with, so the drawer reads "N of 6
 * leads". Set to exactly what the script delivers: a capture that ends at "6 of 10"
 * reads as a run that gave up, and the demo has no business staging that. Well inside
 * `DEMO_BILLING.maxRunLeads` (50), which is the bound the run UI names.
 */
export const DEMO_RUN_TARGET_LEADS = 6;

/**
 * One poll's worth of run progress.
 *
 * v27: the replay is SCALARS, not a narrative log. The org-facing feed carries no
 * events at all (`/api/run/activity` answers an org with `events: []`), and the events
 * it used to carry were exactly the rows we now hide — a match event's detail is
 * `{username, score, tier, reelId}`. Replaying them into a customer-facing demo would
 * re-leak on camera precisely what the redaction removed from the product.
 */
export interface DemoRunTick {
  readonly counters: RunCounters;
  readonly phase: RunPhase;
  /** What the run has discovered so far. Monotonic, like every Section E scalar. */
  readonly leadsFound: number;
  readonly itemsScanned: number;
  readonly relevantFound: number;
  /** Epoch SECONDS — the liveness beat the stall banner reads, not a log line. */
  readonly lastEventAt: number;
}

const RUN_TS_BASE = 1_754_380_800; // fixed, arbitrary epoch seconds

/**
 * The scripted run: 14 progress snapshots walking all six platforms sequentially, in
 * the engine's real run order. Every scalar is monotonic (the customer must never
 * watch a number fall back mid-run), and each row is the state AS OF that poll.
 *
 * The tuple is `[phase, itemsScanned, relevantFound, commentsScored, leadsFound,
 * spendUsd, offsetSec]` — deliberately compact so the whole progression is readable as
 * a table. `phase` is already the customer-safe word the bridge folds internal phases
 * into (lifecycle→starting, feed_walk→searching, comments→qualifying); the internal
 * names never reach a customer, so they are not written down here either.
 */
type TickSeed = readonly [RunPhase, number, number, number, number, number, number];

const RUN_TICKS: readonly TickSeed[] = [
  // instagram — start, a scan tick, then the first qualified lead
  ['starting',   0,  0,  0, 0, 0,    0],
  ['searching',  8,  3,  5, 0, 0.02, 40],
  ['qualifying', 10, 4,  9, 1, 0.05, 75],
  // linkedin
  ['starting',   10, 4,  9, 1, 0.05, 110],
  ['searching',  16, 7, 14, 1, 0.09, 150],
  ['qualifying', 18, 8, 17, 2, 0.12, 185],
  // x
  ['starting',   18, 8, 17, 2, 0.12, 220],
  ['qualifying', 24, 11, 23, 3, 0.17, 260],
  // youtube
  ['starting',   24, 11, 23, 3, 0.17, 295],
  ['qualifying', 30, 14, 29, 4, 0.22, 335],
  // reddit
  ['starting',   30, 14, 29, 4, 0.22, 370],
  ['qualifying', 36, 17, 35, 5, 0.27, 410],
  // telegram
  ['starting',   36, 17, 35, 5, 0.27, 445],
  ['qualifying', 42, 20, 41, 6, 0.32, 485],
];

export const DEMO_RUN_SCRIPT: readonly DemoRunTick[] = RUN_TICKS.map(
  ([phase, itemsScanned, relevantFound, commentsScored, leadsFound, spendUsd, offset]) => ({
    phase,
    itemsScanned,
    relevantFound,
    leadsFound,
    lastEventAt: RUN_TS_BASE + offset,
    // `counters.matches` mirrors `leadsFound` here because an in-process run's rows
    // land immediately. On a real FLEET run the two diverge until the job acks — which
    // is the whole reason `leadsFound` is computed separately (Section E).
    counters: {
      reelsSeen: itemsScanned,
      relevancePasses: relevantFound,
      commentsScored,
      matches: leadsFound,
      spendUsd,
      likes: 0,
      follows: 0,
    },
  }),
);
