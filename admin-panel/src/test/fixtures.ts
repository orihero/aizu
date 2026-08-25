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
import type {
  AdminOrgLead,
  AdminOrgRun,
  AdminRunActivity,
  AdminRunEvent,
} from '@/shared/schemas/admin';
import { leadUidOf } from '@/shared/lib/leadId';

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

/**
 * A lead. `id` is DERIVED from the composite identity (campaignId, platform,
 * commentId) unless explicitly overridden, so a fixture that only varies
 * `commentId` — or only `campaignId` — still gets a distinct, realistic id. That
 * mirrors what the engine emits and what matchSchema recomputes at the boundary.
 *
 * v27: no `username`, no comment `text` — an org-facing lead carries neither, so a
 * fixture must not either. `intent` is the only lead prose the app renders; the raw
 * identity is reachable only through the audited reveal (see
 * `FakePanelRepository.revealLead`). A test that wants the "nothing derivable" case
 * overrides `intent: ''`, which is a REAL value the server sends.
 */
export function buildMatch(overrides: Partial<Match> = {}): Match {
  const match: Match = {
    id: '',
    commentId: 'c1',
    campaignId: 'cmp-001',
    platform: 'instagram',
    sessionId: 's-001',
    // Derived from the post + the comment, with identity and contact digits stripped
    // out — those live in `extracted`, not in a line every viewer sees.
    intent: 'Wants pricing for the Pro plan and left a phone number',
    lang: 'en',
    score: 0.91,
    reason: 'asks price with phone number',
    extracted: { phone: '+14155550142', intent: 'pricing' },
    status: 'new',
    escalated: false,
    escalationCost: 0,
    capturedAt: { date: 'Jun 10', time: '11:12', ts: 1_718_017_920 },
    statusBy: null,
    statusAt: null,
    statusHistory: [],
    notes: [],
    ...overrides,
  };
  return { ...match, id: overrides.id ?? leadUidOf(match) };
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
    // v27: `expiresInDays`/`newSinceLastPoll` are watchlist-derived and the engine
    // writes a watchlist row ONLY for a post that produced a lead — so they mark
    // WHICH scanned post a lead came from and no longer reach an org caller. The
    // default fixture is the ORG wire; a superadmin-shaped reel overrides them in.
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
    // E.7: `spent` and `leads` share this card and have opposite failure asymmetries,
    // so the delivery trio travels with them. The pair defaults to NULL — the shape a
    // bridge that cannot report a gap sends — which is what makes the card's documented
    // fallback (`c.leadsFound ?? c.leads`) the path a test exercises unless it opts out.
    // Override all three together (`leadsFound: 15, leadsDelivered: 12, delivery:
    // 'not_delivered'`), never the numbers alone: the panel reads `delivery` for the
    // verdict rather than re-deriving the comparison.
    leadsFound: null,
    leadsDelivered: null,
    delivery: 'delivered',
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
      // Delivery trio defaults as on the card it mirrors: the pair null (fall back to
      // `leads`), the verdict 'delivered'. See buildCampaign.
      { id: 'cmp-001', name: 'Acme SaaS Lead Gen', platform: 'instagram', status: 'live', leads: 12, cpl: 0.35,
        leadsFound: null, leadsDelivered: null, delivery: 'delivered' },
    ],
    // v27: the ticker names what the lead WANTS, not who they are.
    ticker: [
      { id: 'c1', intent: 'Wants pricing for the Pro plan', platform: 'instagram', score: 0.91, capturedAt: { date: 'Jun 10', time: '11:12' } },
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
      { id: 'cmp-001', name: 'Acme SaaS Lead Gen', status: 'live', leads: 12, cpl: 0.35, spend: 4.2,
        leadsFound: null, leadsDelivered: null, delivery: 'delivered' },
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
    reason: null,
    attempts: 1,
    maxAttempts: 3,
    ...overrides,
  };
}

/**
 * One poll of the org-facing activity feed.
 *
 * v27: `events` is EMPTY and `eventsRedacted` is true, because that is the only shape
 * an org caller can ever receive — the narrative log is a superadmin surface now
 * (`buildAdminRunActivity`). A customer-app test that seeds events here would be
 * asserting against a payload the bridge cannot produce.
 *
 * The scalars default to a healthy live run mid-flight: 3 of a 10-lead target found
 * and all 3 delivered. `leadsFound`/`leadsDelivered`/`delivery` move together — the
 * dead-letter case is `{ finished: true, leadsFound: 15, leadsDelivered: 0,
 * delivery: 'not_delivered' }`, and `leadsDelivered: null` is the pre-E.5 bridge that
 * never reported the number at all (UNKNOWN, which is not zero).
 */
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
    events: [],
    eventsRedacted: true,
    phase: 'qualifying',
    leadsFound: 3,
    leadsDelivered: 3,
    delivery: 'delivered',
    itemsScanned: 12,
    relevantFound: 5,
    lastEventAt: 1_718_800_000,
    targetLeads: 10,
    flags: [],
    cursor: 0,
    fleetJob: null,
    ...overrides,
  };
}

/* ---- superadmin plane (v27): the run feed + the leads that keep their identity ----
 *
 * These are the ONLY fixtures that carry a handle or a comment body, and they carry
 * them on purpose: `/api/admin/*` is the plane the redaction deliberately exempts.
 * Never feed one of these into an org-facing component — the types are separate
 * branches (`schemas/admin.ts` vs `schemas/panelState.ts`) precisely so that a
 * mistake here cannot compile. */

/** One row of the superadmin run picker (GET /api/admin/orgs/{id}/runs). Epoch
 *  SECONDS; `mode` null is a run this process no longer remembers. */
export function buildAdminOrgRun(overrides: Partial<AdminOrgRun> = {}): AdminOrgRun {
  return {
    runId: 'run-001',
    campaignId: 'cmp-001',
    campaignName: 'Acme SaaS Lead Gen',
    mode: 'live',
    status: 'done',
    platforms: ['instagram'],
    startedAt: 1_718_800_000,
    finishedAt: 1_718_801_800,
    sessions: 1,
    leads: 3,
    ...overrides,
  };
}

/** One narrative event as the superadmin feed serves it — `message` and the raw
 *  `detail` blob included, identities and all (`detail` is a JSON STRING). */
export function buildAdminRunEvent(overrides: Partial<AdminRunEvent> = {}): AdminRunEvent {
  return {
    id: 1,
    seq: 1,
    campaignId: 'cmp-001',
    sessionId: 's-001',
    phase: 'comments',
    level: 'success',
    message: 'Match: @dana_t (score 0.91)',
    detail: '{"username":"dana_t","score":0.91,"tier":"match","reelId":"r1"}',
    createdAt: 1_718_800_075,
    platform: 'instagram',
    ...overrides,
  };
}

/** One poll of the FULL feed. Unlike `buildRunActivity` this one HAS events —
 *  that asymmetry is the whole point of the split. */
export function buildAdminRunActivity(
  overrides: Partial<AdminRunActivity> = {},
): AdminRunActivity {
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
    events: [buildAdminRunEvent()],
    flags: [],
    cursor: 1,
    ...overrides,
  };
}

/** One superadmin lead row: `username` + `text` beside the derived `intent`, which
 *  is the pairing an operator uses to check the redaction summarises honestly. */
export function buildAdminOrgLead(overrides: Partial<AdminOrgLead> = {}): AdminOrgLead {
  return {
    commentId: 'c1',
    campaignId: 'cmp-001',
    platform: 'instagram',
    username: 'dana_t',
    text: 'How much is the Pro plan? +1 415 555 0142',
    intent: 'Wants pricing for the Pro plan and left a phone number',
    capturedAt: 1_718_017_920,
    status: 'new',
    score: 0.91,
    reason: 'asks price with phone number',
    extracted: true,
    tier: 'match',
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

/** The full tier catalogue as the backend's TIERS map surfaces it (5 tiers).
 *  `campaignCap: null` is UNLIMITED, not "unset" — mirrors `billing.TIERS`. */
const BILLING_TIERS = [
  { tier: 'free', displayName: 'Free', leadCap: 10, campaignCap: 1, selfServe: false,
    prices: { month: 0, year: 0 } },
  { tier: 'lite', displayName: 'Lite', leadCap: 50, campaignCap: 3, selfServe: true,
    prices: { month: 9.99, year: 99 } },
  { tier: 'starter', displayName: 'Starter', leadCap: 250, campaignCap: null, selfServe: true,
    prices: { month: 24.99, year: 249 } },
  { tier: 'pro', displayName: 'Pro', leadCap: 2000, campaignCap: null, selfServe: true,
    prices: { month: 149, year: 1490 } },
  { tier: 'scale', displayName: 'Scale', leadCap: 0, campaignCap: null, selfServe: false,
    prices: { month: null, year: null } },
] as const;

/**
 * A billing summary as `/api/settings.BILLING` ships it. Defaults to a Free org
 * (the implicit default) with a partial usage meter.
 *
 * `campaignsUsed: 0` against a cap of 1 keeps the default org UNDER its campaign cap,
 * so a test opts INTO the at-cap state (`buildBilling({ campaignsUsed: 1 })`) rather
 * than every unrelated campaigns-page test rendering a disabled New Campaign button.
 * It is deliberately not derived from the CAMPAIGNS fixture: the gate reads this
 * number, and a test asserting the gate must be able to set it directly.
 */
export function buildBilling(overrides: Partial<Billing> = {}): Billing {
  return {
    tier: 'free',
    interval: null,
    status: 'active',
    periodEnd: null,
    cancelAtPeriodEnd: false,
    leadCap: 10,
    leadsUsed: 3,
    campaignCap: 1,
    campaignsUsed: 0,
    // The Free tier's reveal allowance, well under its cap for the same reason
    // `campaignsUsed` is: a test opts INTO the exhausted state rather than every
    // unrelated billing test rendering an at-limit meter.
    revealCap: 10,
    revealsUsed: 2,
    // The Free tier's period allowance, which is also the largest target one run may
    // ask for. A SOFT bound (E.6) — copy reads "up to 10 leads per run".
    maxRunLeads: 10,
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
// v27: `username` is gone as a sort key — mirrors the server's `_LEAD_SORT_KEYS`,
// which now sorts on the same `intent` the column shows.
const _LEAD_SORTERS: Record<string, (m: Match) => number | string> = {
  capturedAt: sortByCaptured,
  score: (m) => m.score,
  intent: (m) => m.intent.toLowerCase(),
  platform: (m) => m.platform,
  status: (m) => m.status,
};

/**
 * Everything the free-text lead search may look at, lowercased — the mirror of the
 * server's `panel_org._lead_haystack`. It searches what a customer can actually SEE
 * (the derived intent, the classifier's reason, and the `extracted` field VALUES a
 * lead was captured with), because username/text are no longer on the row and
 * searching them would silently match nothing.
 */
function leadHaystack(m: Match): string {
  // Only the primitive values: a nested object would stringify to '[object Object]',
  // which matches nothing an operator would ever type and only pollutes the haystack.
  const extracted = Object.values(m.extracted)
    .filter((v): v is string | number | boolean => (
      typeof v === 'string' || typeof v === 'number' || typeof v === 'boolean'
    ))
    .map((v) => String(v));
  return [m.intent, m.reason, ...extracted].join(' ').toLowerCase();
}

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
    rows = rows.filter((m) => leadHaystack(m).includes(needle));
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
