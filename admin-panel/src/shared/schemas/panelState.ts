import { z } from 'zod';
import { roleSchema } from './auth';
import { leadUidOf } from '@/shared/lib/leadId';

/**
 * Boundary schemas for the bridge server's /api/state payload.
 * Everything entering the app is validated here — components and
 * selectors only ever see these inferred types (never raw JSON).
 */

// The v6 lead Kanban pipeline. `.catch('new')` keeps a stale/unknown status from
// a lagging payload parseable (coerced to 'new') rather than throwing the row out.
export const matchStatusSchema = z
  .enum(['new', 'in_progress', 'interested', 'closed', 'couldnt_connect', 'archived'])
  .catch('new');

export const sessionFlagSchema = z.enum(['', 'resumed', 'halted', 'live']);

export const alertTierSchema = z.enum(['halt', 'soft', 'info']);

const pacingSchema = z.object({
  sessionsPerDay: z.string(),
  sessionLength: z.string(),
  reelsPerSession: z.string(),
  dwell: z.string(),
  betweenReels: z.string(),
  window: z.string(),
});

/** The signed-in company (v7). Present on every authed payload; null pre-v7. */
export const organizationSchema = z
  .object({
    id: z.number().nullable(),
    name: z.string().nullable(),
    logo: z.string().nullable(),
    description: z.string().nullable(),
  })
  .nullable();

export const panelConfigSchema = z.object({
  productName: z.string(),
  todayLabel: z.string(),
  timezone: z.string(),
  matchThreshold: z.number(),
  skipRatioThreshold: z.number(),
  budgetCapUsd: z.number(),
  canaryLimitReels: z.number(),
  watchlistTtlDays: z.number(),
  pacing: pacingSchema,
  // v7: the company identity + the caller's role (drives UI chrome + gating).
  organization: organizationSchema.catch(null),
  role: roleSchema.catch('viewer'),
});

/** Cost-per-lead is null when there are no confirmed leads yet (no divide-by-zero). */
const cplSchema = z.number().nullable().catch(null);

/**
 * One platform a campaign discovers on (multi-platform fan-out, C1). The wire
 * shape uses seed ARRAYS (the form layer converts to/from comma strings). Seed
 * arrays `.catch([])` so a single malformed seed degrades that field to empty
 * rather than dropping the whole channels list; `includeHomeFeed` is optional —
 * the engine derives a seed-aware default when it is absent.
 */
export const channelEntrySchema = z.object({
  platform: z.string(),
  seedHashtags: z.array(z.string()).catch([]),
  seedAccounts: z.array(z.string()).catch([]),
  seedChannels: z.array(z.string()).catch([]),
  includeHomeFeed: z.boolean().optional(),
});

/** The editable matching brief (camelCase). null when a draft has no brief yet. */
export const campaignBriefFormSchema = z.object({
  platform: z.string(),
  goal: z.string(),
  threshold: z.number(),
  languageMix: z.array(z.string()),
  relevanceDef: z.string(),
  matchDef: z.string(),
  extractDef: z.string(),
  // Tuned classifier system prompts (advanced, optional). Always coerced to a
  // string so older payloads that omit them still validate and round-trip blank.
  relevancePrompt: z.string().catch(''),
  matchPrompt: z.string().catch(''),
  visionPrompt: z.string().catch(''),
  seedHashtags: z.array(z.string()),
  seedAccounts: z.array(z.string()),
  seedChannels: z.array(z.string()),
  // Whether discovery also walks the account's algorithmic home feed. Optional:
  // older payloads omit it, and the engine derives a seed-aware default.
  includeHomeFeed: z.boolean().optional(),
  // Multi-platform fan-out: per-platform channels. Optional + `.catch(undefined)`
  // so an old payload (no channels) or a malformed list degrades to undefined
  // (single-platform) instead of throwing the whole brief away.
  channels: z.array(channelEntrySchema).optional().catch(undefined),
});

/**
 * The AI-drafted campaign prefill (POST /api/campaign/generate → data). Every
 * field is optional and string fields `.catch('')` so ragged model output
 * degrades to a partial prefill rather than throwing the whole draft away — the
 * user reviews and completes it in the form regardless. Keys mirror
 * CampaignFormState (FORM keys, arrays comma-joined to strings) so the result
 * spreads straight into the form with zero translation.
 */
export const generatedCampaignDraftSchema = z
  .object({
    name: z.string().catch(''),
    objective: z.string().catch(''),
    platform: z.string().catch(''),
    // Numbers stay numbers; a malformed value drops the field (form keeps its default).
    threshold: z.number().optional().catch(undefined),
    budgetCap: z.number().optional().catch(undefined),
    goalTarget: z.number().optional().catch(undefined),
    languages: z.string().catch(''),
    relevanceDef: z.string().catch(''),
    matchDef: z.string().catch(''),
    extractDef: z.string().catch(''),
    relevancePrompt: z.string().catch(''),
    matchPrompt: z.string().catch(''),
    visionPrompt: z.string().catch(''),
    seedHashtags: z.string().catch(''),
    seedAccounts: z.string().catch(''),
    seedChannels: z.string().catch(''),
    // Multi-platform fan-out the interview chose. FORM shape (seeds comma-joined
    // STRINGS, matching ChannelFormEntry) so it spreads straight into the form.
    // `.catch(undefined)` so a ragged list degrades to single-platform.
    channels: z
      .array(
        z.object({
          platform: z.string(),
          seedHashtags: z.string().catch(''),
          seedAccounts: z.string().catch(''),
          seedChannels: z.string().catch(''),
        }),
      )
      .optional()
      .catch(undefined),
  })
  .partial();

export const generateCampaignResponseSchema = z.object({
  ok: z.boolean(),
  data: generatedCampaignDraftSchema.nullable(),
  error: z.string().nullable(),
});

/**
 * One clarifying-interview question (POST /api/campaign/interview → data.questions).
 * The model output is ragged, so required text fields `.catch('')` and the optional
 * arrays `.catch(undefined)` — a partly-bad question degrades to empty fields (the
 * consuming hook drops any with a blank id/prompt) rather than throwing the whole
 * round away. Mirrors the server's assemble_interview coercion (defense in depth).
 */
export const interviewQuestionSchema = z.object({
  id: z.string().catch(''),
  type: z.enum(['single', 'multi', 'text', 'platforms']).catch('text'),
  prompt: z.string().catch(''),
  help: z.string().optional(),
  options: z
    .array(z.object({ value: z.string(), label: z.string().catch(''), hint: z.string().optional() }))
    .optional()
    .catch(undefined),
  suggested: z.array(z.string()).optional().catch(undefined),
  placeholder: z.string().optional(),
  allowCustom: z.boolean().optional(),
});

export const interviewResponseSchema = z.object({
  ok: z.boolean(),
  data: z
    .object({
      done: z.boolean().catch(false),
      // A wholly malformed list degrades to no questions (→ wizard finishes), never throws.
      questions: z.array(interviewQuestionSchema).catch([]),
      productContext: z.string().catch(''),
      round: z.number().catch(1),
    })
    .nullable(),
  error: z.string().nullable(),
});

// Account-warmth verdict travelling WITH each campaign (warming PRD §7.2/§7.3).
// Every sub-field .catch()es so pre-warmth payloads (and partial stubs) still
// validate to a neutral, non-blocking 50/'warming'. The server is authoritative;
// the client re-derives score>=gateMin only as a trust check.
const warmthComponentsDefault = { age: 0, ramp: 0, network: 0, profile: 0, trust: 0 };
const warmthNeutral = {
  score: 50,
  state: 'warming' as const,
  gateMin: 40,
  gateFull: 70,
  meetsGate: true,
  components: warmthComponentsDefault,
  trend: [] as number[],
  etaHours: null,
  checkedAt: '—',
};
export const warmthSchema = z
  .object({
    score: z.number().min(0).max(100).catch(50),
    state: z.enum(['warming', 'ready', 'full', 'throttled']).catch('warming'),
    gateMin: z.number().catch(40),
    gateFull: z.number().catch(70),
    meetsGate: z.boolean().catch(true),
    components: z
      .object({
        age: z.number().catch(0),
        ramp: z.number().catch(0),
        network: z.number().catch(0),
        profile: z.number().catch(0),
        trust: z.number().catch(0),
      })
      .catch(warmthComponentsDefault),
    trend: z.array(z.number()).catch([]),
    etaHours: z.number().nullable().catch(null),
    checkedAt: z.string().catch('—'),
  })
  .catch(warmthNeutral);

/**
 * E.5/E.7 — how a run's harvest reconciles with what actually reached the account.
 * The bridge computes it in ONE place (`panel.delivery_state`) so a run drawer and a
 * campaign card can never disagree about what "not delivered" means:
 *   - `delivered`      leadsFound <= leadsDelivered. Nothing to explain.
 *   - `pending`        a gap on a run still in flight. NOT a fault: a fleet run's rows
 *                      only land at ack, so every live fleet run reads this way.
 *   - `not_delivered`  a FINISHED run whose gap never closed — it found leads that
 *                      never reached the customer (the dead-letter path). Show the two
 *                      numbers as what they are, and label the spend beside them as
 *                      spend on an incomplete run. Never hide or zero that spend, and
 *                      never synthesise a CPL from `leadsFound` — a cost per lead the
 *                      customer cannot open is a fiction.
 *
 * `.catch('delivered')` for a pre-v27 bridge: one that cannot report a gap cannot have
 * one to report, and defaulting the other way would stamp the warning on every run.
 */
export const deliverySchema = z
  .enum(['delivered', 'pending', 'not_delivered'])
  .catch('delivered');

export const campaignSchema = z.object({
  id: z.string(),
  name: z.string(),
  // These default so a lightweight CAMPAIGNS "stub" (member's leads switcher feed)
  // and the empty-org state still validate against the full campaign shape.
  goalType: z.string().catch(''),
  status: z.string(),
  threshold: z.number().catch(0),
  languages: z.array(z.string()).catch([]),
  extractFields: z.array(z.string()).catch([]),
  startedAt: z.string().catch('—'),
  brief: z.string().catch(''),
  // v3 ops fields (older payloads omit them → safe defaults).
  platform: z.string().catch('instagram'),
  // C6: every platform this campaign fans out to (each channel, else [platform]).
  // `.optional().catch(undefined)` — NOT `.catch([])`: undefined means "server sent
  // no platforms key" so the card falls back to [platform]; [] means an empty list.
  platforms: z.array(z.string()).optional().catch(undefined),
  budgetCap: z.number().catch(0),
  // Monthly lead target; null when the campaign has no meta override yet.
  goalTarget: z.number().nullable().catch(null),
  // The editable brief for the campaign form; null for a meta-only draft.
  briefForm: campaignBriefFormSchema.nullable().catch(null),
  spent: z.number().catch(0),
  leads: z.number().catch(0),
  cpl: cplSchema,
  // E.7: `spent` and `leads` sit on the same card and have OPPOSITE failure
  // asymmetries — a nacked run banks its spend but strands its leads. This trio keeps
  // the pair honest. `null` on either number means a pre-v27 bridge did not report it,
  // so fall back to `leads` (`c.leadsFound ?? c.leads`) rather than rendering 0.
  leadsFound: z.number().nullable().catch(null),
  leadsDelivered: z.number().nullable().catch(null),
  delivery: deliverySchema,
  spark: z.array(z.number()).catch([]),
  // Account warmth (warming PRD §7.2). Missing on pre-warmth payloads → neutral.
  warmth: warmthSchema,
  // v12 lifecycle (Phase 1). archivedAt non-null = archived (reversible hide);
  // pausedReason distinguishes an operator pause ('user') from a system halt
  // ('auto'). Older payloads omit them → safe defaults.
  archivedAt: z.string().nullable().catch(null),
  pausedReason: z.string().nullable().catch(null),
  // v12 fixed-cadence schedule (Phase 3). scheduleEnabled gates the rest; dow is
  // 0=Mon..6=Sun (weekly only); nextRunAt is the server-computed next fire (ISO).
  scheduleEnabled: z.boolean().catch(false),
  scheduleKind: z.string().catch(''),
  scheduleDow: z.number().nullable().catch(null),
  scheduleHour: z.number().nullable().catch(null),
  scheduleMinute: z.number().nullable().catch(null),
  scheduleTz: z.string().catch('Asia/Tashkent'),
  nextRunAt: z.string().nullable().catch(null),
  // FIX 2: the run_id of this campaign's most-recent live fleet job (status in
  // {queued,leased,running}), else null. DB-derived so it survives a refresh —
  // lets the RunDrawer show the fleet run without relying on transient mutation
  // data. Null for in-process runs / campaigns with no live fleet job.
  fleetRunId: z.string().nullable().catch(null),
});

export const sessionSchema = z.object({
  id: z.string(),
  // Owning campaign — lets the org-wide /api/campaigns session pool be filtered back to
  // one campaign on the edit page. Older payloads omit it → empty (matches no filter).
  campaignId: z.string().catch(''),
  // The RunManager run this session belongs to (deep-link into its activity feed).
  // Null for pre-v10 sessions / CLI runs without run-id correlation → no drill-down.
  runId: z.string().nullable().catch(null),
  // Which platform this run targeted. Older payloads omit it → default instagram.
  platform: z.string().catch('instagram'),
  date: z.string(),
  start: z.string(),
  durationMin: z.number(),
  reelsSeen: z.number(),
  alreadySeen: z.number(),
  relevant: z.number(),
  commentsScored: z.number(),
  matches: z.number(),
  escalations: z.number(),
  spendUsd: z.number(),
  flag: sessionFlagSchema.catch(''),
  skipRatio: z.number(),
  watermark: z.string(),
});

export const reelSchema = z.object({
  id: z.string(),
  author: z.string(),
  authorFull: z.string(),
  caption: z.string(),
  ocrText: z.string(),
  thumbSeed: z.string(),
  addedAt: z.string(),
  lastPoll: z.string(),
  // v27: BOTH are watchlist-derived, and the engine only ever writes a watchlist
  // row for a post that produced a lead — so `expiresInDays: 10` and
  // `newSinceLastPoll: 1` each mark WHICH scanned post a lead came from, which is
  // the re-join the audited reveal exists to prevent. `panel._build_reels` now
  // omits them for every org caller and ships them only to the superadmin plane.
  // Optional, not `.catch(0)`: a defaulted 0 would be indistinguishable from a
  // real "no new matches" and would quietly re-open the question for any future
  // reader. Absent means NOT DISCLOSED. (Required until v27, so an older bridge
  // still parses — and the whole array is `.catch([])`, which without this would
  // have blanked REELS wholesale the moment the engine dropped the keys.)
  expiresInDays: z.number().optional(),
  newSinceLastPoll: z.number().optional(),
  pollHistory: z.array(z.number()),
});

/** One immutable entry in a lead's status-change audit trail. */
export const statusChangeSchema = z.object({
  fromStatus: matchStatusSchema.nullable().catch(null),
  toStatus: matchStatusSchema,
  by: z.string().nullable(), // actor email (no usernames in this system)
  at: z.string(),
  atTs: z.number().catch(0),
  note: z.string().nullable().catch(null), // reason for a forced/terminal move
});

/** A free-form note on a lead. Any user adds; only the author deletes. */
export const leadNoteSchema = z.object({
  id: z.string(),
  body: z.string(),
  authorEmail: z.string().nullable().catch(null),
  authorId: z.number().nullable().catch(null),
  createdAt: z.string(),
  createdAtTs: z.number().catch(0),
});

/**
 * A lead. `id` is the composite identity `(campaignId, platform, commentId)` — the
 * engine's own `matches` primary key — and is RECOMPUTED at this boundary rather than
 * trusted from the wire, so a single client-side definition guarantees the invariant
 * the whole app leans on: one id ⇔ one lead record. `commentId` stays on the record as
 * the raw platform comment id (display, export, and the write payload); it is NOT
 * unique on its own, so never key a lookup, a React key, or a selection on it.
 *
 * v27 redaction: there is NO `username`, NO comment `text` and NO `reelId` on an
 * org-facing lead — the bridge stopped sending all three (`panel._build_matches`), and
 * this schema stripping them is the second line: `z.object` drops unknown keys, so an
 * older/rogue payload that still carries them cannot reach a component through this
 * boundary. The customer-facing prose is `intent`.
 *
 * `reelId` left with the handle for the same reason it existed: it is the POST POINTER.
 * `reelUrl(platform, reelId)` turns it into a public URL where the comment and the
 * handle are both on screen, so a lead row carrying one is the redaction undone in a
 * single click — by a component, by an export, or by anyone reading the network tab.
 * The only post pointer in the customer plane now arrives on `RevealedLead`, from the
 * audited per-lead reveal (`PanelRepository.revealLead`), which is deliberately NOT
 * part of this type — see Section F.
 */
export const matchSchema = z.object({
  id: z.string(),
  commentId: z.string(),
  campaignId: z.string(),
  // Source platform of the comment. Older payloads omit it → default instagram.
  platform: z.string().catch('instagram'),
  sessionId: z.string().nullable(),
  // The one-line summary of what this person wants, derived at capture time by the
  // engine's `derive_intent`. `''` is a REAL value the server sends (a pre-v27 lead,
  // or nothing derivable honestly) — the UI renders a neutral placeholder for it and
  // never falls back to an identifier. `.catch('')` also covers a bridge older than
  // v27 that omits the key entirely, so a lagging server degrades the cell, not the page.
  intent: z.string().catch(''),
  lang: z.string().nullable(),
  score: z.number(),
  reason: z.string(),
  extracted: z.record(z.unknown()),
  status: matchStatusSchema,
  escalated: z.boolean(),
  escalationCost: z.number(),
  // `ts` is the raw capture epoch for sorting; older payloads omit it → 0.
  capturedAt: z.object({ date: z.string(), time: z.string(), ts: z.number().catch(0) }),
  statusBy: z.string().nullable(),
  statusAt: z.string().nullable(),
  // v6 Kanban: audit trail + free-form notes. Default-empty so older/lagging
  // payloads (and the current tests' fixtures) stay valid.
  statusHistory: z.array(statusChangeSchema).catch([]),
  notes: z.array(leadNoteSchema).catch([]),
}).transform((m) => ({ ...m, id: leadUidOf(m) }));

export const platformSummarySchema = z.object({
  platform: z.string(),
  matches: z.number(),
  sessions: z.number(),
});

export const escalationEntrySchema = z.object({
  time: z.string(),
  sessionId: z.string(),
  stage: z.string(),
  model: z.string(),
  tokens: z.number(),
  cost: z.number(),
  outcome: z.string(),
});

export const alertSchema = z.object({
  time: z.string(),
  tier: alertTierSchema.catch('info'),
  title: z.string(),
  desc: z.string(),
});

const healthIndicatorSchema = z.object({
  state: z.string(),
  detail: z.string(),
});

export const healthSchema = z.object({
  overall: z.string(),
  login: healthIndicatorSchema,
  checkpoint: healthIndicatorSchema,
  canary: z.object({
    emptyStreak: z.number(),
    limit: z.number(),
    lastJson: z.string(),
    detail: z.string(),
  }),
  actionBlock: healthIndicatorSchema,
  feed: z.object({
    skipRatio: z.number(),
    threshold: z.number(),
    flagged: z.boolean(),
    lastFlag: z.string(),
    lastResteer: z.string(),
    detail: z.string(),
  }),
});

export const soulSchema = z.object({
  file: z.string(),
  rules: z.array(z.string()),
});

/* ---- v3 dashboard / reports / settings surfaces ---- */

export const channelDatumSchema = z.object({
  platform: z.string(),
  current: z.number(),
  previous: z.number(),
});

export const funnelSchema = z.object({
  reels: z.number(),
  relevant: z.number(),
  scored: z.number(),
  matches: z.number(),
});

/** One row of the dashboard's top-campaign mini list. It puts `leads` next to `cpl`
 * (a spend-derived number), so it carries the delivery trio off the card it mirrors —
 * see `deliverySchema`. The period TILES deliberately do not: those are windowed while
 * the found estimate is lifetime, and mixing them would invent a ratio true of neither. */
export const topCampaignSchema = z.object({
  id: z.string(),
  name: z.string(),
  platform: z.string(),
  status: z.string(),
  leads: z.number(),
  cpl: cplSchema,
  leadsFound: z.number().nullable().catch(null),
  leadsDelivered: z.number().nullable().catch(null),
  delivery: deliverySchema,
});

/** One dashboard live-ticker row. v27: the ticker names what the lead WANTS, not who
 * they are — the server already truncates `intent` to a glance-width line. */
export const tickerEntrySchema = z.object({
  id: z.string(),
  intent: z.string().catch(''),
  platform: z.string(),
  score: z.number(),
  capturedAt: z.object({ date: z.string(), time: z.string() }),
});

export const dashboardPeriodSchema = z.object({
  leads: z.object({ value: z.number(), delta: z.string(), spark: z.array(z.number()) }),
  goal: z.object({ target: z.number(), current: z.number(), pct: z.number() }),
  cpl: z.object({ value: cplSchema, history: z.array(z.number()) }),
  conversion: z.object({ value: z.number(), delta: z.string() }),
  channels: z.array(channelDatumSchema),
  funnel: funnelSchema,
  bestHour: z.array(z.number()),
  activeCampaigns: z.number(),
  topCampaigns: z.array(topCampaignSchema),
  ticker: z.array(tickerEntrySchema),
});

const EMPTY_DASHBOARD_PERIOD = {
  leads: { value: 0, delta: '0%', spark: [] },
  goal: { target: 0, current: 0, pct: 0 },
  cpl: { value: null, history: [] },
  conversion: { value: 0, delta: '0%' },
  channels: [],
  funnel: { reels: 0, relevant: 0, scored: 0, matches: 0 },
  bestHour: [],
  activeCampaigns: 0,
  topCampaigns: [],
  ticker: [],
};

export const dashboardSchema = z
  .object({
    today: dashboardPeriodSchema,
    week: dashboardPeriodSchema,
    month: dashboardPeriodSchema,
  })
  .catch({
    today: EMPTY_DASHBOARD_PERIOD,
    week: EMPTY_DASHBOARD_PERIOD,
    month: EMPTY_DASHBOARD_PERIOD,
  });

export const reportsPeriodSchema = z.object({
  labels: z.array(z.string()),
  matchesByPlatform: z.array(z.object({ platform: z.string(), values: z.array(z.number()) })),
  cplTrend: z.array(z.number()),
  spendByStage: z.array(z.object({ name: z.string(), value: z.number() })),
  platformRanking: z.array(z.object({ platform: z.string(), leads: z.number() })),
  // E.7: leads and spend on the SAME row, so the report must not read "$X spent,
  // 0 leads" for a dead-lettered run with no way to tell that from a barren campaign.
  perCampaign: z.array(
    z.object({
      id: z.string(),
      name: z.string(),
      status: z.string(),
      leads: z.number(),
      cpl: cplSchema,
      spend: z.number(),
      leadsFound: z.number().nullable().catch(null),
      leadsDelivered: z.number().nullable().catch(null),
      delivery: deliverySchema,
    }),
  ),
});

const EMPTY_REPORTS_PERIOD = {
  labels: [],
  matchesByPlatform: [],
  cplTrend: [],
  spendByStage: [],
  platformRanking: [],
  perCampaign: [],
};

export const reportsSchema = z
  .object({
    today: reportsPeriodSchema,
    week: reportsPeriodSchema,
    month: reportsPeriodSchema,
  })
  .catch({
    today: EMPTY_REPORTS_PERIOD,
    week: EMPTY_REPORTS_PERIOD,
    month: EMPTY_REPORTS_PERIOD,
  });

export const teamMemberSchema = z.object({
  id: z.string(),
  // The real account id (v7) — used as the target for role/remove writes.
  userId: z.number().nullable().catch(null),
  name: z.string(),
  email: z.string().nullable().catch(null),
  role: roleSchema.catch('viewer'),
  initials: z.string().catch(''),
  status: z.string().catch('active'),
  createdAt: z.number().nullable().catch(null),
});

/** A pending team invite (the copy-link path). */
export const inviteSchema = z.object({
  id: z.string(),
  email: z.string().catch(''),
  role: roleSchema.catch('viewer'),
  status: z.string().catch('pending'),
  createdAt: z.number().nullable().catch(null),
  expiresAt: z.number().nullable().catch(null),
});

export const integrationSchema = z.object({
  id: z.string(),
  platform: z.string(),
  name: z.string(),
  connected: z.boolean(),
  detail: z.string().catch(''),
  source: z.string().catch('derived'),
});

/* ---- billing (v13; BILLING on /api/settings, mirror of _build_billing) ---- */

/** One tier in the comparison grid. `prices` are null for the sales-led Scale
 * tier (no self-serve price). `leadCap` is interval-independent. */
export const billingTierSchema = z.object({
  tier: z.string(),
  displayName: z.string(),
  leadCap: z.number(),
  // v27 plan limits: how many non-archived campaigns this tier allows. `null` means
  // UNLIMITED, not "unset" — the grid must print "Unlimited campaigns", never "0".
  // `.catch(null)` reads a pre-v27 bridge's omission as unlimited on purpose: this is
  // display copy for OTHER tiers, and the real gate is the server's 402.
  campaignCap: z.number().nullable().catch(null),
  selfServe: z.boolean(),
  prices: z.object({
    month: z.number().nullable(),
    year: z.number().nullable(),
  }),
});

/** The org's current subscription + usage meter + the tier catalogue. `leadsUsed`
 * uses the SAME billing-period anchor the run gate enforces, so the meter can
 * never disagree with enforcement. */
export const billingSchema = z.object({
  tier: z.string(),
  interval: z.string().nullable(),
  status: z.string(),
  periodEnd: z.number().nullable(),
  cancelAtPeriodEnd: z.boolean(),
  leadCap: z.number(),
  leadsUsed: z.number(),
  // v27 plan limits. `campaignCap` null = unlimited, so every gate reads
  // `campaignCap !== null && campaignsUsed >= campaignCap` — a falsy check would read
  // unlimited as zero and disable New Campaign for a paying org. Both default the way
  // a pre-v27 bridge behaved (uncapped, nothing used) rather than locking the page.
  campaignCap: z.number().nullable().catch(null),
  campaignsUsed: z.number().catch(0),
  // v27 reveal allowance: how many DISTINCT leads this org may un-redact per billing
  // period, and how many it already has. `null` = unlimited, exactly like
  // `campaignCap` — and it is also what a bridge that predates the reveal cap sends
  // (nothing), which is why the meter is hidden rather than drawn as "0 / 0". Read
  // `revealCap !== null` before gating; a falsy check would read unlimited as zero.
  //
  // DISTINCT leads, not reveal calls: reopening a drawer on a lead this period already
  // revealed does not move `revealsUsed` and never trips the cap. The counter is the
  // server's; the panel only renders it.
  revealCap: z.number().nullable().catch(null),
  revealsUsed: z.number().catch(0),
  // Largest lead target ONE run may request — the resolved period cap (incl. any
  // per-org override), which is why it is read here and not off the `tiers` grid.
  // A SOFT bound: the engine's stop condition is per-session, so a run can overshoot
  // (E.6). Copy it as "up to N leads per run", never "exactly N".
  maxRunLeads: z.number().catch(0),
  usageRatio: z.number(),
  nearLimit: z.boolean(),
  tiers: z.array(billingTierSchema).catch([]),
});

/* ---- run control plane (POST /api/run + RUN block in /api/state) ---- */

/** Safe by default: a dry run never spends; live is an explicit opt-in. */
export const runModeSchema = z.enum(['dry', 'live']);

/** A run targets exactly one campaign, or every live campaign in a batch. */
export const runScopeSchema = z.enum(['campaign', 'all']);

/** The single in-flight run, or null when the engine is idle. ISO timestamp. */
export const activeRunSchema = z.object({
  // The run_id — lets the activity feed (GET /api/run/activity) target this run.
  id: z.string(),
  scope: runScopeSchema,
  campaignId: z.string().nullable(),
  mode: runModeSchema,
  startedAt: z.string(),
  // v12: whether the run is cooperatively paused (idling between reels). Older
  // payloads omit it → not paused.
  paused: z.boolean().catch(false),
  // v12: 'manual' (operator) or 'scheduled' (the schedule daemon). Older payloads
  // omit it → 'manual'.
  launchSource: z.string().catch('manual'),
});

/** A finished run kept in the recent history. ISO timestamps. */
export const runRecordSchema = z.object({
  id: z.string(),
  scope: runScopeSchema,
  campaignId: z.string().nullable(),
  mode: runModeSchema,
  startedAt: z.string(),
  finishedAt: z.string().nullable(),
  outcome: z.enum(['ok', 'error', 'aborted']),
  summary: z.string(),
  // v12: 'manual' | 'scheduled' — older payloads omit it → 'manual'.
  launchSource: z.string().catch('manual'),
});

export const runBlockSchema = z.object({
  active: activeRunSchema.nullable(),
  recent: z.array(runRecordSchema),
});

/* ---- live run activity feed (GET /api/run/activity) ---- */

/** Severity of one emitted event. Unknown levels degrade to 'info' (never throw). */
export const runEventLevelSchema = z
  .enum(['info', 'success', 'warn', 'error'])
  .catch('info');

/**
 * One append-only event from a run. `id` is the global, strictly-increasing
 * insertion id — the unique paging cursor and React key. `seq` is only a
 * per-session display ordinal that RESETS per session under run-all, so it is
 * NOT unique across the feed — never page or key on it. `phase` is a free
 * string (lifecycle | relevance | comments | engage | feed_walk | halt | …);
 * `detail` is a raw JSON STRING the UI parses lazily (and tolerates failing to
 * parse). `createdAt` is epoch SECONDS (float). `campaignId` is null for
 * run-level (run-all) events.
 */
export const runEventSchema = z.object({
  id: z.number(),
  seq: z.number(),
  campaignId: z.string().nullable(),
  phase: z.string(),
  level: runEventLevelSchema,
  message: z.string(),
  detail: z.string().nullable().catch(null),
  createdAt: z.number(),
  // C7: the platform of the session that emitted this event (multi-platform fan-out).
  // Null when the session is missing/pruned, or on older payloads.
  platform: z.string().nullable().catch(null),
});

/** Live tally for the run. Each counter degrades to 0 on a malformed value. */
export const runCountersSchema = z.object({
  reelsSeen: z.number().catch(0),
  relevancePasses: z.number().catch(0),
  commentsScored: z.number().catch(0),
  matches: z.number().catch(0),
  spendUsd: z.number().catch(0),
  likes: z.number().catch(0),
  follows: z.number().catch(0),
});

/** An open health/safety flag surfaced for the run (e.g. feed tapping out). */
export const runFlagSchema = z.object({
  kind: z.string(),
  severity: z.string(),
  detail: z.string().nullable(),
});

/**
 * FIX 2: fleet-job status for a run routed to the worker fleet. Null for an
 * in-process (non-fleet) run. `status` is a free string coming from the engine
 * (queued|leased|running|done|failed|interrupted) — kept as a string so an
 * unknown status never throws the page out; the banner handles the known ones.
 * `lastEventAt` is epoch SECONDS (MAX(run_events.created_at) for the run) or null
 * when the run has emitted nothing yet — used to detect a stalled fleet run.
 * `reason` is the worker's failure code (e.g. `cdp_unreachable`) or null — free text
 * from the server, so render it as text and read the failed/succeeded wording off
 * `status`, never off `reason` being present (a done job can carry one too).
 */
export const fleetJobSchema = z.object({
  jobId: z.string(),
  status: z.string(),
  lastEventAt: z.number().nullable(),
  leaseExpiresAt: z.number().nullable(),
  reason: z.string().nullable().catch(null),
  attempts: z.number().nullable().catch(null),
  maxAttempts: z.number().nullable().catch(null),
});

/**
 * The customer-safe word for what a run is doing right now, folded out of the latest
 * event's internal `phase` by the server's explicit allow-list (`_ORG_RUN_PHASES`).
 * An unknown/absent phase degrades to 'working' — never to a raw internal string, and
 * never to 'done', which would stop the poller on a live run.
 */
export const runPhaseSchema = z
  .enum(['starting', 'searching', 'qualifying', 'stopped', 'done', 'failed', 'working'])
  .catch('working');

/**
 * The activity snapshot for one poll.
 *
 * v27: for an ORG caller `events` is ALWAYS `[]` and `eventsRedacted` is true — the
 * narrative feed is a superadmin surface now (`adminRunActivitySchema`). The key and
 * the `after`/`cursor` plumbing stay so the poll contract is unchanged; `cursor` simply
 * never advances. What replaces the log is the block of SCALARS below, which the bridge
 * folds out of the events server-side (Section E) — never an event row with keys
 * deleted, so a future engine's new detail key cannot ride along to a customer.
 *
 * Every scalar is a max/sum over the whole run, so all of them are monotonic: a
 * customer must never watch a progress number fall back down mid-run.
 *
 * `finished` already reflects the fleet override on the server: a fleet job that
 * is queued/leased/running reports finished=false (keep polling even when silent),
 * done/failed/interrupted reports finished=true. `fleetJob` is null for in-process
 * runs — degrades to null so a malformed block never throws the page out.
 */
export const runActivitySchema = z.object({
  runId: z.string(),
  finished: z.boolean(),
  counters: runCountersSchema,
  events: z.array(runEventSchema),
  // v27. `false` when absent, which is the honest reading of a PRE-v27 bridge: it
  // really is still sending events. It is a "why is this empty" marker for the UI,
  // never the thing that decides whether events may be rendered — the customer app
  // simply does not render them (B3).
  eventsRedacted: z.boolean().catch(false),
  // ---- Section E progress scalars (org-facing; no message/detail/session id) ----
  phase: runPhaseSchema,
  /** What the run DISCOVERED: the deduped event estimate, reconciled upward against
   *  the org's real `matches` rows once the job acks. */
  leadsFound: z.number().catch(0),
  /**
   * What actually REACHED the account: the org's real `matches` row count. `null` is
   * UNKNOWN — a bridge that predates E.5 and never reported it — and is NOT zero: a
   * signal the server did not report must never read as "nothing was delivered".
   * Read `delivery` for the verdict; only render this number when it is non-null.
   */
  leadsDelivered: z.number().nullable().catch(null),
  /** The reconciliation of the pair above — see `deliverySchema`. A finished run at
   *  `not_delivered` found leads that never reached the customer; show both numbers
   *  and label its spend as spend on an incomplete run. */
  delivery: deliverySchema,
  itemsScanned: z.number().catch(0),
  relevantFound: z.number().catch(0),
  /** Epoch SECONDS of the newest event, or null when the run has emitted nothing.
   *  A timestamp is not a log — it is the liveness beat behind the stall banner. */
  lastEventAt: z.number().nullable().catch(null),
  /** The run's plan-clamped lead target, for "7 of 10 leads". Null when it is not
   *  durably known (an in-process run carries it in the POST /api/run response
   *  instead). A TARGET, not a ceiling — runs can overshoot it (E.6). */
  targetLeads: z.number().nullable().catch(null),
  flags: z.array(runFlagSchema).catch([]),
  cursor: z.number().catch(0),
  fleetJob: fleetJobSchema.nullable().catch(null),
});

export const runActivityResponseSchema = z.object({
  ok: z.boolean(),
  data: runActivitySchema.nullable(),
  error: z.string().nullable(),
});

export const EMPTY_HEALTH = {
  overall: 'operational',
  login: { state: 'valid', detail: '' },
  checkpoint: { state: 'clear', detail: '' },
  canary: { emptyStreak: 0, limit: 0, lastJson: '—', detail: '' },
  actionBlock: { state: 'none', detail: '' },
  feed: { skipRatio: 0, threshold: 0, flagged: false, lastFlag: '—', lastResteer: '—', detail: '' },
};

export const panelStateSchema = z.object({
  CONFIG: panelConfigSchema,
  CAMPAIGNS: z.array(campaignSchema),
  MATCHES: z.array(matchSchema),
  // v7 role pruning: a member's state carries only CONFIG/CAMPAIGNS/MATCHES, and a
  // viewer's omits TEAM/INTEGRATIONS — so every other key defaults when absent.
  SESSIONS: z.array(sessionSchema).catch([]),
  REELS: z.array(reelSchema).catch([]),
  PLATFORMS: z.array(platformSummarySchema).catch([]),
  ESCALATION_LOG: z.array(escalationEntrySchema).catch([]),
  ALERTS: z.array(alertSchema).catch([]),
  HEALTH: healthSchema.catch(EMPTY_HEALTH),
  SOUL: soulSchema.catch({ file: 'soul.md', rules: [] }),
  DASHBOARD: dashboardSchema,
  REPORTS: reportsSchema,
  TEAM: z.array(teamMemberSchema).catch([]),
  INVITES: z.array(inviteSchema).catch([]),
  INTEGRATIONS: z.array(integrationSchema).catch([]),
  // Run control plane; older payloads omit it → idle defaults (no active run).
  RUN: runBlockSchema.catch({ active: null, recent: [] }),
});

export const statusWriteResponseSchema = z.object({
  ok: z.boolean(),
  data: z.object({ commentId: z.string(), status: matchStatusSchema }).nullable(),
  error: z.string().nullable(),
});

/**
 * POST /api/lead/reveal → ONE lead's raw identity (v27 reveal-on-demand).
 *
 * No `.catch()` fallbacks here on purpose: unlike every other boundary in this file,
 * a degraded value would be a LIE about a person — a blank handle reading as "no
 * handle" rather than "the reveal did not work". A malformed body must fail the parse
 * and surface as an error the drawer can show, so the redaction is never quietly
 * reported as an empty identity.
 */
export const revealedLeadSchema = z.object({
  // The server echoes the lead's identity back (`id` is the composite lead uid) so the
  // drawer can prove the answer belongs to the lead it asked about before rendering it
  // — a late response landing on a reopened drawer must not paint another person's
  // handle onto this lead.
  id: z.string(),
  commentId: z.string(),
  platform: z.string(),
  username: z.string(),
  text: z.string(),
  reelId: z.string(),
});

export const revealLeadResponseSchema = z.object({
  ok: z.boolean(),
  data: revealedLeadSchema.nullable(),
  error: z.string().nullable(),
});

/** Generic {ok,data,error} envelope for the v3 write endpoints. */
export const writeResponseSchema = z.object({
  ok: z.boolean(),
  data: z.unknown().nullable(),
  error: z.string().nullable(),
});

/** POST /api/integration/telegram/start → a short-lived wizard token. */
export const telegramLoginStartSchema = z.object({
  token: z.string(),
});

export const telegramLoginStartResponseSchema = z.object({
  ok: z.boolean(),
  data: telegramLoginStartSchema.nullable(),
  error: z.string().nullable(),
});

/** POST /api/integration/telegram/verify → whether 2FA is still required. */
export const telegramVerifyResultSchema = z.object({
  needsPassword: z.boolean().catch(false),
});

export const telegramVerifyResponseSchema = z.object({
  ok: z.boolean(),
  data: telegramVerifyResultSchema.nullable(),
  error: z.string().nullable(),
});
