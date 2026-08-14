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
  Campaign,
  ChannelEntry,
  InterviewResponse,
  Match,
  MatchStatus,
  PanelState,
  RunCounters,
  RunEvent,
  StatusChange,
  TeamMember,
} from '@/shared/types/domain';
import {
  buildCampaign,
  buildDashboardPeriod,
  buildIntegration,
  buildLeadNote,
  buildMatch,
  buildPanelState,
  buildReportsPeriod,
  buildRunEvent,
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

interface LeadSeed {
  readonly id: string;
  readonly platform: string;
  readonly username: string;
  readonly text: string;
  readonly score: number;
  readonly reason: string;
  readonly status: MatchStatus;
  readonly extracted?: Readonly<Record<string, unknown>>;
  readonly time: string;
  readonly note?: string;
}

// One entry per lead, in the same platform order the run walks
// (instagram → linkedin → x → youtube → reddit → telegram). 12 interested
// (~2/platform, scores 0.72–0.94, dana_t is the 0.91 hero lead), 8 new
// (mixed, some below the 0.7 threshold), 2 in_progress.
const LEAD_SEEDS: readonly LeadSeed[] = [
  // instagram
  {
    id: 'ig-dana', platform: 'instagram', username: 'dana_t', status: 'interested', score: 0.91,
    text: "How much is the Pro plan? We're drowning in Zendesk tickets and need something that "
      + 'scales. Can I get a demo?',
    reason: 'asks for a demo and pricing, names the exact pain (ticket volume)',
    extracted: { email: 'dana@northwindtech.com', phone: '+14155550142', intent: 'demo' },
    time: '08:12', note: 'Called, left a voicemail — following up tomorrow.',
  },
  {
    id: 'ig-marcus', platform: 'instagram', username: 'marcus_ops', status: 'interested', score: 0.84,
    text: "We've been comparing a few support-automation tools, Acme looks solid. What's onboarding "
      + 'like for a 40-person team?',
    reason: 'actively evaluating tools, gives team size', extracted: { intent: 'inquire' }, time: '09:05',
  },
  {
    id: 'ig-jess', platform: 'instagram', username: 'jess_growth', status: 'new', score: 0.65,
    text: 'Following for the SaaS tips 🔥', reason: 'low buying-intent, general engagement', time: '09:40',
  },
  {
    id: 'ig-alex', platform: 'instagram', username: 'alex_scaling', status: 'new', score: 0.62,
    text: 'Our support queue is out of control lately, might need to look into this.',
    reason: 'pain signal but no explicit ask yet', time: '10:02',
  },
  // linkedin
  {
    id: 'li-sarah', platform: 'linkedin', username: 'sarah.chen', status: 'interested', score: 0.88,
    text: "We're evaluating support-automation platforms for our 60-person CS team. Does Acme "
      + 'integrate with our existing Zendesk data?',
    reason: 'specific integration question from a named buyer role', extracted: { email: 'sarah.chen@brightloop.io', intent: 'inquire' },
    time: '10:20',
  },
  {
    id: 'li-raj', platform: 'linkedin', username: 'raj_patel', status: 'interested', score: 0.79,
    text: 'Would love a trial — our ticket backlog has tripled this quarter.',
    reason: 'explicit trial request with quantified pain', extracted: { intent: 'trial' }, time: '10:47',
  },
  {
    id: 'li-mike', platform: 'linkedin', username: 'mike.torres', status: 'new', score: 0.71,
    text: 'Interesting post, curious how this compares to Intercom.',
    reason: 'comparison curiosity, early-stage', time: '11:10',
  },
  {
    id: 'li-helen', platform: 'linkedin', username: 'hr_helen', status: 'in_progress', score: 0.70,
    text: 'Can someone reach out about pricing for a mid-size team?',
    reason: 'pricing ask — being worked by the team', time: '11:30',
  },
  // x
  {
    id: 'x-kate', platform: 'x', username: 'devops_kate', status: 'interested', score: 0.93,
    text: '@AcmeHQ does your API support webhook triggers for ticket escalation? Ready to switch off '
      + 'our current stack.',
    reason: 'technical fit question plus explicit intent to switch', extracted: { email: 'kate@devopsco.dev', intent: 'buy' },
    time: '11:58',
  },
  {
    id: 'x-leo', platform: 'x', username: 'founder_leo', status: 'interested', score: 0.76,
    text: 'Been eyeing Acme for weeks, just need budget sign-off.',
    reason: 'buying intent pending internal approval', extracted: { intent: 'buy' }, time: '12:15',
  },
  {
    id: 'x-quinn', platform: 'x', username: 'quick_take_q', status: 'new', score: 0.58,
    text: 'lol our support inbox is a warzone', reason: 'vague pain, no explicit intent', time: '12:40',
  },
  {
    id: 'x-bea', platform: 'x', username: 'biz_dev_bea', status: 'new', score: 0.75,
    text: "What's the pricing tier for a 10-person startup?",
    reason: 'pricing question, above threshold, awaiting triage', time: '13:02',
  },
  // youtube
  {
    id: 'yt-amy', platform: 'youtube', username: 'techreviewer_amy', status: 'interested', score: 0.82,
    text: 'Great walkthrough — does this integrate with our helpdesk stack? Might feature it in my '
      + 'next review.',
    reason: 'integration question from a reviewer with reach', extracted: { email: 'amy@reviewhub.com', intent: 'inquire' },
    time: '13:25',
  },
  {
    id: 'yt-saas', platform: 'youtube', username: 'saas_curious', status: 'interested', score: 0.72,
    text: "Signed up for the trial after watching this, hope it's as good as it looks.",
    reason: 'self-reported trial signup', extracted: { intent: 'trial' }, time: '13:50',
  },
  {
    id: 'yt-bob', platform: 'youtube', username: 'reviewer_bob', status: 'new', score: 0.68,
    text: 'Solid demo, will keep this on my radar.', reason: 'positive but non-committal', time: '14:10',
  },
  // reddit
  {
    id: 'rd-pm', platform: 'reddit', username: 'pm_throwaway', status: 'interested', score: 0.94,
    text: "Our team is switching off Zendesk next quarter — has anyone actually used Acme in "
      + 'production? Need a demo ASAP.',
    reason: 'active migration timeline plus explicit demo ask', extracted: { email: 'pm.throwaway88@protonmail.com', intent: 'demo' },
    time: '14:35',
  },
  {
    id: 'rd-sam', platform: 'reddit', username: 'startup_sam', status: 'interested', score: 0.77,
    text: "We're a 15-person startup drowning in support tickets, is Acme overkill for us?",
    reason: 'qualifying fit, real quantified pain', extracted: { intent: 'inquire' }, time: '14:58',
  },
  {
    id: 'rd-dev', platform: 'reddit', username: 'curious_dev', status: 'new', score: 0.73,
    text: 'How does the scoring model handle multi-language support threads?',
    reason: 'technical curiosity, above threshold', time: '15:20',
  },
  {
    id: 'rd-eval', platform: 'reddit', username: 'eval_team', status: 'in_progress', score: 0.81,
    text: 'Our eval team is comparing 3 vendors, Acme is on the shortlist. Can we get a call booked?',
    reason: 'active vendor evaluation — being worked', time: '15:45',
  },
  // telegram
  {
    id: 'tg-ops', platform: 'telegram', username: 'ops_ninja', status: 'interested', score: 0.86,
    text: '@acme_scout can you send pricing for the team plan? We manage support for 5 client accounts.',
    reason: 'explicit pricing ask, agency use case', extracted: { email: 'ops@ninjacollective.io', intent: 'pricing' },
    time: '16:05',
  },
  {
    id: 'tg-jen', platform: 'telegram', username: 'teamlead_jen', status: 'interested', score: 0.80,
    text: 'Our support lead mentioned Acme in standup, worth a demo for our 20-person team.',
    reason: 'internal referral plus demo interest', extracted: { intent: 'demo' }, time: '16:30',
  },
  {
    id: 'tg-newbie', platform: 'telegram', username: 'newbie_ops', status: 'new', score: 0.55,
    text: "just joined this channel, what's this group about?", reason: 'no buying signal, off-topic',
    time: '16:50',
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
    username: seed.username,
    lang: 'en',
    text: seed.text,
    score: seed.score,
    reason: seed.reason,
    extracted: seed.extracted ?? {},
    status: seed.status,
    escalated: false,
    escalationCost: 0,
    capturedAt: { date: DEMO_DATE, time: seed.time, ts },
    reelId: `${seed.platform}-${seed.id}`,
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
    { id: DEMO_CAMPAIGN_ID, name: DEMO_CAMPAIGN.name, platform: 'instagram', status: 'live', leads: TOTAL_LEADS, cpl: 1.13 },
  ],
  ticker: [...DEMO_MATCHES]
    .sort((a, b) => b.capturedAt.ts - a.capturedAt.ts)
    .slice(0, 6)
    .map((m) => ({
      id: m.id, username: m.username, platform: m.platform, score: m.score,
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
    { id: DEMO_CAMPAIGN_ID, name: DEMO_CAMPAIGN.name, status: 'live', leads: TOTAL_LEADS, cpl: 1.13, spend: 24.8 },
    { id: DEMO_DRAFT_CAMPAIGN.id, name: DEMO_DRAFT_CAMPAIGN.name, status: 'draft', leads: 0, cpl: null, spend: 0 },
  ],
});

export const DEMO_REPORTS = {
  today: DEMO_REPORTS_PERIOD,
  week: DEMO_REPORTS_PERIOD,
  month: DEMO_REPORTS_PERIOD,
};

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

// ---- live run activity replay ----

export const DEMO_RUN_ID = 'run-demo-001';
export const DEMO_RUN_STARTED_AT = '2026-08-05T08:00:00Z';

interface ScriptedEvent {
  readonly event: RunEvent;
  readonly counters: RunCounters;
}

const RUN_TS_BASE = 1_754_380_800; // fixed, arbitrary epoch seconds

function scanMessage(platform: string, reelsSeen: number, relevant: number): string {
  const noun = platform === 'x' || platform === 'linkedin' ? 'posts seen' : 'reels seen';
  return `Scanning — ${reelsSeen} ${noun}, ${relevant} relevant`;
}

function matchMessage(platform: string, username: string, score: number): string {
  const handle = platform === 'reddit' ? `u/${username}` : platform === 'linkedin' ? username : `@${username}`;
  return `Match: ${handle} (score ${score.toFixed(2)})`;
}

/** ~14 events walking all six platforms sequentially — start, a scan tick, and
 *  a scored match for the first two platforms (to show the fuller cadence),
 *  then start + match for the rest. Counters tick up monotonically; each
 *  event carries the counters snapshot AS OF that event, matching the real
 *  engine's `_emit(phase, level, message, detail)` shape and message copy
 *  (`engine/aizu/engines/{platform}/session.py`). */
const RUN_SCRIPT: readonly ScriptedEvent[] = [
  { event: { id: 1, seq: 1, campaignId: DEMO_CAMPAIGN_ID, phase: 'lifecycle', level: 'info', message: 'Run started — campaign cmp-001 (instagram)', detail: '{"campaignId":"cmp-001"}', createdAt: RUN_TS_BASE, platform: 'instagram' },
    counters: { reelsSeen: 0, relevancePasses: 0, commentsScored: 0, matches: 0, spendUsd: 0, likes: 0, follows: 0 } },
  { event: { id: 2, seq: 2, campaignId: DEMO_CAMPAIGN_ID, phase: 'feed_walk', level: 'info', message: scanMessage('instagram', 8, 3), detail: '{"reelsSeen":8,"relevancePasses":3}', createdAt: RUN_TS_BASE + 40, platform: 'instagram' },
    counters: { reelsSeen: 8, relevancePasses: 3, commentsScored: 5, matches: 0, spendUsd: 0.02, likes: 0, follows: 0 } },
  { event: { id: 3, seq: 3, campaignId: DEMO_CAMPAIGN_ID, phase: 'comments', level: 'success', message: matchMessage('instagram', 'dana_t', 0.91), detail: '{"username":"dana_t","score":0.91}', createdAt: RUN_TS_BASE + 75, platform: 'instagram' },
    counters: { reelsSeen: 10, relevancePasses: 4, commentsScored: 9, matches: 1, spendUsd: 0.05, likes: 0, follows: 0 } },
  { event: { id: 4, seq: 1, campaignId: DEMO_CAMPAIGN_ID, phase: 'lifecycle', level: 'info', message: 'Run started — campaign cmp-001 (linkedin)', detail: '{"campaignId":"cmp-001"}', createdAt: RUN_TS_BASE + 110, platform: 'linkedin' },
    counters: { reelsSeen: 10, relevancePasses: 4, commentsScored: 9, matches: 1, spendUsd: 0.05, likes: 0, follows: 0 } },
  { event: { id: 5, seq: 2, campaignId: DEMO_CAMPAIGN_ID, phase: 'feed_walk', level: 'info', message: scanMessage('linkedin', 6, 3), detail: '{"reelsSeen":6,"relevancePasses":3}', createdAt: RUN_TS_BASE + 150, platform: 'linkedin' },
    counters: { reelsSeen: 16, relevancePasses: 7, commentsScored: 14, matches: 1, spendUsd: 0.09, likes: 0, follows: 0 } },
  { event: { id: 6, seq: 3, campaignId: DEMO_CAMPAIGN_ID, phase: 'comments', level: 'success', message: matchMessage('linkedin', 'sarah.chen', 0.88), detail: '{"username":"sarah.chen","score":0.88}', createdAt: RUN_TS_BASE + 185, platform: 'linkedin' },
    counters: { reelsSeen: 18, relevancePasses: 8, commentsScored: 17, matches: 2, spendUsd: 0.12, likes: 0, follows: 0 } },
  { event: { id: 7, seq: 1, campaignId: DEMO_CAMPAIGN_ID, phase: 'lifecycle', level: 'info', message: 'Run started — campaign cmp-001 (x)', detail: '{"campaignId":"cmp-001"}', createdAt: RUN_TS_BASE + 220, platform: 'x' },
    counters: { reelsSeen: 18, relevancePasses: 8, commentsScored: 17, matches: 2, spendUsd: 0.12, likes: 0, follows: 0 } },
  { event: { id: 8, seq: 2, campaignId: DEMO_CAMPAIGN_ID, phase: 'comments', level: 'success', message: matchMessage('x', 'devops_kate', 0.93), detail: '{"username":"devops_kate","score":0.93}', createdAt: RUN_TS_BASE + 260, platform: 'x' },
    counters: { reelsSeen: 24, relevancePasses: 11, commentsScored: 23, matches: 3, spendUsd: 0.17, likes: 0, follows: 0 } },
  { event: { id: 9, seq: 1, campaignId: DEMO_CAMPAIGN_ID, phase: 'lifecycle', level: 'info', message: 'Run started — campaign cmp-001 (youtube)', detail: '{"campaignId":"cmp-001"}', createdAt: RUN_TS_BASE + 295, platform: 'youtube' },
    counters: { reelsSeen: 24, relevancePasses: 11, commentsScored: 23, matches: 3, spendUsd: 0.17, likes: 0, follows: 0 } },
  { event: { id: 10, seq: 2, campaignId: DEMO_CAMPAIGN_ID, phase: 'comments', level: 'success', message: matchMessage('youtube', 'techreviewer_amy', 0.82), detail: '{"username":"techreviewer_amy","score":0.82}', createdAt: RUN_TS_BASE + 335, platform: 'youtube' },
    counters: { reelsSeen: 30, relevancePasses: 14, commentsScored: 29, matches: 4, spendUsd: 0.22, likes: 0, follows: 0 } },
  { event: { id: 11, seq: 1, campaignId: DEMO_CAMPAIGN_ID, phase: 'lifecycle', level: 'info', message: 'Run started — campaign cmp-001 (reddit)', detail: '{"campaignId":"cmp-001"}', createdAt: RUN_TS_BASE + 370, platform: 'reddit' },
    counters: { reelsSeen: 30, relevancePasses: 14, commentsScored: 29, matches: 4, spendUsd: 0.22, likes: 0, follows: 0 } },
  { event: { id: 12, seq: 2, campaignId: DEMO_CAMPAIGN_ID, phase: 'comments', level: 'success', message: matchMessage('reddit', 'pm_throwaway', 0.94), detail: '{"username":"pm_throwaway","score":0.94}', createdAt: RUN_TS_BASE + 410, platform: 'reddit' },
    counters: { reelsSeen: 36, relevancePasses: 17, commentsScored: 35, matches: 5, spendUsd: 0.27, likes: 0, follows: 0 } },
  { event: { id: 13, seq: 1, campaignId: DEMO_CAMPAIGN_ID, phase: 'lifecycle', level: 'info', message: 'Run started — campaign cmp-001 (telegram)', detail: '{"campaignId":"cmp-001"}', createdAt: RUN_TS_BASE + 445, platform: 'telegram' },
    counters: { reelsSeen: 36, relevancePasses: 17, commentsScored: 35, matches: 5, spendUsd: 0.27, likes: 0, follows: 0 } },
  { event: { id: 14, seq: 2, campaignId: DEMO_CAMPAIGN_ID, phase: 'comments', level: 'success', message: matchMessage('telegram', 'ops_ninja', 0.86), detail: '{"username":"ops_ninja","score":0.86}', createdAt: RUN_TS_BASE + 485, platform: 'telegram' },
    counters: { reelsSeen: 42, relevancePasses: 20, commentsScored: 41, matches: 6, spendUsd: 0.32, likes: 0, follows: 0 } },
];

export const DEMO_RUN_SCRIPT: readonly ScriptedEvent[] = RUN_SCRIPT.map(({ event, counters }) => ({
  event: buildRunEvent(event), counters,
}));
