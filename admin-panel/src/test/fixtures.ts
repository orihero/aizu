import type {
  ActiveRun,
  AgentReadiness,
  Alert,
  Billing,
  Campaign,
  CampaignsPayload,
  DashboardPayload,
  DashboardPeriod,
  Integration,
  LeadNote,
  LeadsPayload,
  LeadsQuery,
  Match,
  PanelState,
  Reel,
  ReportsPayload,
  ReportsPeriod,
  FleetJob,
  RunActivity,
  RunBlock,
  RunEvent,
  RunRecord,
  SettingsPayload,
  Session,
  StatusChange,
  TeamMember,
  Invite,
} from '@/shared/types/domain';

export function buildSession(overrides: Partial<Session> = {}): Session {
  return {
    id: 's-001',
    campaignId: 'campaign-uno',
    runId: 'run-001',
    platform: 'instagram',
    date: 'Jun 10',
    start: '11:05',
    durationMin: 18,
    reelsSeen: 20,
    alreadySeen: 2,
    relevant: 6,
    commentsScored: 38,
    matches: 2,
    escalations: 4,
    spendUsd: 0.14,
    flag: '',
    skipRatio: 0.1,
    watermark: 'QVFD001Kc',
    ...overrides,
  };
}

export function buildMatch(overrides: Partial<Match> = {}): Match {
  return {
    id: 'c1',
    commentId: 'c1',
    campaignId: 'cmp-001',
    platform: 'instagram',
    sessionId: 's-001',
    username: 'dana_t',
    lang: 'en',
    text: 'How much is the Pro plan? +1 415 555 0142',
    score: 0.91,
    reason: 'asks price with phone number',
    extracted: { phone: '+14155550142', intent: 'pricing' },
    status: 'new',
    escalated: false,
    escalationCost: 0,
    capturedAt: { date: 'Jun 10', time: '11:12', ts: 1_718_017_920 },
    reelId: 'r1',
    statusBy: null,
    statusAt: null,
    statusHistory: [],
    notes: [],
    ...overrides,
  };
}

export function buildStatusChange(overrides: Partial<StatusChange> = {}): StatusChange {
  return {
    fromStatus: 'new',
    toStatus: 'in_progress',
    by: 'tester@aizu.test',
    at: 'Jun 11',
    atTs: 1_718_104_320,
    note: null,
    ...overrides,
  };
}

export function buildLeadNote(overrides: Partial<LeadNote> = {}): LeadNote {
  return {
    id: 'n1',
    body: 'Called, left a voicemail.',
    authorEmail: 'tester@aizu.test',
    authorId: 1,
    createdAt: 'Jun 11',
    createdAtTs: 1_718_104_500,
    ...overrides,
  };
}

export function buildReel(overrides: Partial<Reel> = {}): Reel {
  return {
    id: 'r1',
    author: 'acme.io',
    authorFull: 'Acme Inc.',
    caption: 'Acme — plan your sprints.',
    ocrText: 'Pro from $12/seat',
    thumbSeed: 'r1',
    addedAt: 'Jun 8',
    lastPoll: 'Jun 10',
    expiresInDays: 5,
    newSinceLastPoll: 3,
    pollHistory: [],
    ...overrides,
  };
}

export function buildAlert(overrides: Partial<Alert> = {}): Alert {
  return {
    time: 'Jun 10 11:00',
    tier: 'soft',
    title: 'Feed Health',
    desc: 'skip ratio above threshold',
    ...overrides,
  };
}

export function buildCampaign(overrides: Partial<Campaign> = {}): Campaign {
  return {
    id: 'cmp-001',
    name: 'Acme SaaS Lead Gen',
    goalType: 'lead',
    status: 'live',
    threshold: 0.7,
    languages: ['en'],
    extractFields: ['phone', 'email', 'intent'],
    startedAt: 'Jun 8',
    brief: 'Surface purchase intent for the Acme SaaS product.',
    platform: 'instagram',
    budgetCap: 20,
    goalTarget: null,
    briefForm: null,
    spent: 4.2,
    leads: 12,
    cpl: 0.35,
    spark: [1, 2, 0, 3, 2, 4, 1, 2, 3, 1, 0, 2, 3, 4],
    warmth: {
      score: 82,
      state: 'full',
      gateMin: 40,
      gateFull: 70,
      meetsGate: true,
      components: { age: 0.8, ramp: 0.9, network: 0.7, profile: 1, trust: 0.9 },
      trend: [],
      etaHours: null,
      checkedAt: 'Jun 29',
    },
    archivedAt: null,
    pausedReason: null,
    scheduleEnabled: false,
    scheduleKind: '',
    scheduleDow: null,
    scheduleHour: null,
    scheduleMinute: null,
    scheduleTz: 'Asia/Tashkent',
    nextRunAt: null,
    fleetRunId: null,
    ...overrides,
  };
}

export function buildTeamMember(overrides: Partial<TeamMember> = {}): TeamMember {
  return {
    id: '1',
    userId: 1,
    name: 'Jane Doe',
    email: 'jane@acme.com',
    role: 'admin',
    initials: 'JD',
    status: 'active',
    createdAt: null,
    ...overrides,
  };
}

export function buildInvite(overrides: Partial<Invite> = {}): Invite {
  return {
    id: 'invite-hash-1',
    email: 'pending@acme.com',
    role: 'member',
    status: 'pending',
    createdAt: null,
    expiresAt: null,
    ...overrides,
  };
}

export function buildIntegration(overrides: Partial<Integration> = {}): Integration {
  return {
    id: 'instagram',
    platform: 'instagram',
    name: 'Instagram',
    connected: true,
    detail: 'Connected',
    source: 'derived',
    ...overrides,
  };
}

export function buildDashboardPeriod(overrides: Partial<DashboardPeriod> = {}): DashboardPeriod {
  return {
    leads: { value: 12, delta: '+20%', spark: [1, 2, 0, 3, 2, 4, 1, 2, 3, 1, 0, 2, 3, 4] },
    goal: { target: 50, current: 12, pct: 24 },
    cpl: { value: 0.35, history: [0.4, 0.3, 0.5, 0.2, 0.35, 0.3, 0.4, 0.35] },
    conversion: { value: 0.06, delta: '+5%' },
    channels: [{ platform: 'instagram', current: 12, previous: 10 }],
    funnel: { reels: 120, relevant: 40, scored: 200, matches: 12 },
    bestHour: Array.from({ length: 24 }, (_, h) => (h >= 9 && h <= 18 ? 3 : 0)),
    activeCampaigns: 1,
    topCampaigns: [
      { id: 'cmp-001', name: 'Acme SaaS Lead Gen', platform: 'instagram', status: 'live', leads: 12, cpl: 0.35 },
    ],
    ticker: [
      { id: 'c1', username: 'aziz_t', platform: 'instagram', score: 0.91, capturedAt: { date: 'Jun 10', time: '11:12' } },
    ],
    ...overrides,
  };
}

export function buildReportsPeriod(overrides: Partial<ReportsPeriod> = {}): ReportsPeriod {
  return {
    labels: ['Jun 1', 'Jun 2', 'Jun 3'],
    matchesByPlatform: [{ platform: 'instagram', values: [2, 4, 6] }],
    cplTrend: [0.4, 0.3, 0.35],
    spendByStage: [
      { name: 'match', value: 0.05 },
      { name: 'vision', value: 0.09 },
    ],
    platformRanking: [{ platform: 'instagram', leads: 12 }],
    perCampaign: [
      { id: 'cmp-001', name: 'Acme SaaS Lead Gen', status: 'live', leads: 12, cpl: 0.35, spend: 4.2 },
    ],
    ...overrides,
  };
}

export function buildActiveRun(overrides: Partial<ActiveRun> = {}): ActiveRun {
  return {
    id: 'run-001',
    scope: 'campaign',
    campaignId: 'cmp-001',
    mode: 'dry',
    startedAt: '2026-06-18T11:00:00Z',
    paused: false,
    launchSource: 'manual',
    ...overrides,
  };
}

export function buildRunEvent(overrides: Partial<RunEvent> = {}): RunEvent {
  return {
    id: 1,
    seq: 1,
    campaignId: 'cmp-001',
    phase: 'lifecycle',
    level: 'info',
    message: 'Run started — campaign cmp-001 (instagram)',
    detail: '{"campaignId":"cmp-001"}',
    createdAt: 1_718_800_000.123,
    platform: 'instagram',
    ...overrides,
  };
}

export function buildFleetJob(overrides: Partial<FleetJob> = {}): FleetJob {
  return {
    jobId: 'job-001',
    status: 'running',
    lastEventAt: 1_718_800_000,
    leaseExpiresAt: 1_718_800_060,
    ...overrides,
  };
}

export function buildRunActivity(overrides: Partial<RunActivity> = {}): RunActivity {
  return {
    runId: 'run-001',
    finished: false,
    counters: {
      reelsSeen: 12,
      relevancePasses: 5,
      commentsScored: 40,
      matches: 3,
      spendUsd: 0.0123,
      likes: 2,
      follows: 1,
    },
    events: [buildRunEvent()],
    flags: [],
    cursor: 1,
    fleetJob: null,
    ...overrides,
  };
}

export function buildRunRecord(overrides: Partial<RunRecord> = {}): RunRecord {
  return {
    id: 'run-001',
    scope: 'campaign',
    campaignId: 'cmp-001',
    mode: 'dry',
    startedAt: '2026-06-18T11:00:00Z',
    finishedAt: '2026-06-18T11:00:05Z',
    outcome: 'ok',
    summary: 'ran 1 ok',
    launchSource: 'manual',
    ...overrides,
  };
}

export function buildRunBlock(overrides: Partial<RunBlock> = {}): RunBlock {
  return {
    active: null,
    recent: [],
    ...overrides,
  };
}

/** GET /api/agent/readiness — defaults to the healthy state (CDP up, Instagram
 * logged in) so tests opt INTO the not-ready cases explicitly. */
export function buildAgentReadiness(overrides: Partial<AgentReadiness> = {}): AgentReadiness {
  return {
    ready: true,
    cdp: 'ok',
    instagram: 'logged_in',
    checkedAt: 1_718_800_000,
    detail: null,
    cdpUrl: 'http://127.0.0.1:9222',
    ...overrides,
  };
}

export function buildPanelState(overrides: Partial<PanelState> = {}): PanelState {
  return {
    CONFIG: {
      productName: 'AIZU',
      todayLabel: 'Jun 11, 2026',
      timezone: 'Asia/Tashkent (UTC+5)',
      matchThreshold: 0.7,
      skipRatioThreshold: 0.6,
      budgetCapUsd: 20,
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
      organization: { id: 1, name: 'Test Co', logo: null, description: null },
    },
    CAMPAIGNS: [buildCampaign()],
    SESSIONS: [buildSession()],
    REELS: [buildReel()],
    MATCHES: [buildMatch()],
    PLATFORMS: [{ platform: 'instagram', matches: 1, sessions: 1 }],
    ESCALATION_LOG: [],
    ALERTS: [],
    HEALTH: {
      overall: 'operational',
      login: { state: 'valid', detail: 'Cookie session' },
      checkpoint: { state: 'clear', detail: 'No open challenge' },
      canary: { emptyStreak: 0, limit: 5, lastJson: '—', detail: 'nominal' },
      actionBlock: { state: 'none', detail: 'read-only by design' },
      feed: {
        skipRatio: 0.1,
        threshold: 0.6,
        flagged: false,
        lastFlag: '—',
        lastResteer: '—',
        detail: 'Feed nominal',
      },
    },
    SOUL: { file: 'soul.md', rules: ['Read-only: never like, follow, comment, or DM'] },
    DASHBOARD: {
      today: buildDashboardPeriod(),
      week: buildDashboardPeriod(),
      month: buildDashboardPeriod(),
    },
    REPORTS: {
      today: buildReportsPeriod(),
      week: buildReportsPeriod(),
      month: buildReportsPeriod(),
    },
    TEAM: [buildTeamMember()],
    INVITES: [],
    INTEGRATIONS: [
      buildIntegration(),
      buildIntegration({ id: 'youtube', platform: 'youtube', name: 'Youtube', connected: false, detail: 'Not connected' }),
      buildIntegration({ id: 'telegram', platform: 'telegram', name: 'Telegram', connected: false, detail: 'Not connected' }),
    ],
    RUN: buildRunBlock(),
    ...overrides,
  };
}

// ---- per-page endpoint payloads (derived from buildPanelState so the slices can't
//      drift from the monolithic fixture) ----

export function buildDashboardPayload(overrides: Partial<DashboardPayload> = {}): DashboardPayload {
  const s = buildPanelState();
  return {
    DASHBOARD: s.DASHBOARD, MATCHES: s.MATCHES, HEALTH: s.HEALTH,
    ALERTS: s.ALERTS, RUN: s.RUN, CONFIG: s.CONFIG, ...overrides,
  };
}

export function buildCampaignsPayload(overrides: Partial<CampaignsPayload> = {}): CampaignsPayload {
  const s = buildPanelState();
  return { CAMPAIGNS: s.CAMPAIGNS, SESSIONS: s.SESSIONS, RUN: s.RUN, ...overrides };
}

export function buildReportsPayload(overrides: Partial<ReportsPayload> = {}): ReportsPayload {
  const s = buildPanelState();
  return { REPORTS: s.REPORTS, HEALTH: s.HEALTH, ...overrides };
}

/** The full tier catalogue as the backend's TIERS map surfaces it (5 tiers). */
const BILLING_TIERS = [
  { tier: 'free', displayName: 'Free', leadCap: 10, selfServe: false,
    prices: { month: 0, year: 0 } },
  { tier: 'lite', displayName: 'Lite', leadCap: 50, selfServe: true,
    prices: { month: 9.99, year: 99 } },
  { tier: 'starter', displayName: 'Starter', leadCap: 250, selfServe: true,
    prices: { month: 24.99, year: 249 } },
  { tier: 'pro', displayName: 'Pro', leadCap: 2000, selfServe: true,
    prices: { month: 149, year: 1490 } },
  { tier: 'scale', displayName: 'Scale', leadCap: 0, selfServe: false,
    prices: { month: null, year: null } },
] as const;

/** A billing summary as `/api/settings.BILLING` ships it. Defaults to a Free org
 * (the implicit default) with a partial usage meter. */
export function buildBilling(overrides: Partial<Billing> = {}): Billing {
  return {
    tier: 'free',
    interval: null,
    status: 'active',
    periodEnd: null,
    cancelAtPeriodEnd: false,
    leadCap: 10,
    leadsUsed: 3,
    usageRatio: 0.3,
    nearLimit: false,
    tiers: BILLING_TIERS.map((t) => ({ ...t, prices: { ...t.prices } })),
    ...overrides,
  };
}

export function buildSettingsPayload(overrides: Partial<SettingsPayload> = {}): SettingsPayload {
  const s = buildPanelState();
  return {
    CONFIG: s.CONFIG, TEAM: s.TEAM, INVITES: s.INVITES,
    INTEGRATIONS: s.INTEGRATIONS, BILLING: buildBilling(), ...overrides,
  };
}

const _LEAD_STATUS_KEYS = [
  'new', 'in_progress', 'interested', 'closed', 'couldnt_connect', 'archived',
] as const;

const sortByCaptured = (m: Match): number | string => m.capturedAt.ts;
const _LEAD_SORTERS: Record<string, (m: Match) => number | string> = {
  capturedAt: sortByCaptured,
  score: (m) => m.score,
  username: (m) => m.username.toLowerCase(),
  platform: (m) => m.platform,
  status: (m) => m.status,
};

/**
 * Apply a LeadsQuery to a match list exactly as the server does: status/platform/q
 * filters, then sort, then page-slice. Shared by the leads fixture and the fake repo so
 * both mirror /api/leads. Stats are computed over the UNfiltered set (stable tiles).
 */
export function paginateLeads(allMatches: readonly Match[], query: LeadsQuery): LeadsPayload {
  // Campaign scopes the whole page (list + tiles); facets narrow within it.
  const scoped = query.campaign
    ? allMatches.filter((m) => m.campaignId === query.campaign)
    : allMatches;
  let rows = [...scoped];
  if (query.status) rows = rows.filter((m) => m.status === query.status);
  // "All" hides archived (removed); the explicit Archived filter reveals them.
  else rows = rows.filter((m) => m.status !== 'archived');
  if (query.platform) rows = rows.filter((m) => m.platform === query.platform);
  if (query.q) {
    const needle = query.q.toLowerCase();
    rows = rows.filter(
      (m) => m.username.toLowerCase().includes(needle)
        || m.text.toLowerCase().includes(needle),
    );
  }
  const sorter = _LEAD_SORTERS[query.sort ?? 'capturedAt'] ?? sortByCaptured;
  rows.sort((a, b) => {
    const av = sorter(a);
    const bv = sorter(b);
    const cmp = av < bv ? -1 : av > bv ? 1 : 0;
    return query.dir === 'asc' ? cmp : -cmp;
  });
  // Tiles count the ACTIVE (non-archived) leads in scope — archived is "removed".
  const active = scoped.filter((m) => m.status !== 'archived');
  const counts: Record<string, number> = Object.fromEntries(
    _LEAD_STATUS_KEYS.map((k) => [k, 0]));
  let escalated = 0;
  for (const m of active) {
    counts[m.status] = (counts[m.status] ?? 0) + 1;
    if (m.escalated) escalated += 1;
  }
  const won = (counts.interested ?? 0) + (counts.closed ?? 0);
  const campaigns = [...new Set(allMatches.map((m) => m.campaignId))]
    .sort()
    .map((id) => ({ id, name: id }));
  const start = (query.page - 1) * query.pageSize;
  return {
    items: rows.slice(start, start + query.pageSize),
    total: rows.length,
    page: query.page,
    pageSize: query.pageSize,
    stats: {
      total: active.length, counts, won, escalated,
      labeled: active.filter((m) => m.status !== 'new').length,
    },
    platforms: [...new Set(allMatches.map((m) => m.platform))].sort(),
    campaigns,
    CONFIG: buildPanelState().CONFIG,
  };
}

export function buildLeadsPayload(overrides: Partial<LeadsPayload> = {}): LeadsPayload {
  const base = paginateLeads(buildPanelState().MATCHES, { page: 1, pageSize: 50 });
  return { ...base, ...overrides };
}
