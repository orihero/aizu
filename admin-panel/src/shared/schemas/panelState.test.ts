import { describe, expect, test } from 'vitest';
import {
  buildCampaign,
  buildFleetJob,
  buildPanelState,
  buildReel,
  buildRunActivity,
  buildRunEvent,
} from '@/test/fixtures';
import {
  activeRunSchema,
  billingSchema,
  billingTierSchema,
  campaignBriefFormSchema,
  campaignSchema,
  channelEntrySchema,
  generatedCampaignDraftSchema,
  matchSchema,
  panelStateSchema,
  reelSchema,
  revealLeadResponseSchema,
  runActivitySchema,
  runEventSchema,
  tickerEntrySchema,
} from './panelState';

const VALID_BRIEF = {
  platform: 'instagram',
  goal: 'lead',
  threshold: 0.7,
  languageMix: [],
  relevanceDef: 'r',
  matchDef: 'm',
  extractDef: 'e',
  seedHashtags: [],
  seedAccounts: [],
  seedChannels: [],
};

describe('multi-platform channels (Phase 5)', () => {
  test('channelEntrySchema parses a well-formed entry with optional includeHomeFeed', () => {
    const parsed = channelEntrySchema.parse({
      platform: 'youtube', seedHashtags: ['a'], seedAccounts: [],
      seedChannels: ['UC1'], includeHomeFeed: false,
    });
    expect(parsed.platform).toBe('youtube');
    expect(parsed.includeHomeFeed).toBe(false);
  });

  test('a missing seed array degrades to [] rather than rejecting the entry (M1)', () => {
    const parsed = channelEntrySchema.parse({ platform: 'instagram' });
    expect(parsed.seedHashtags).toEqual([]);
    expect(parsed.seedAccounts).toEqual([]);
    expect(parsed.seedChannels).toEqual([]);
    expect(parsed.includeHomeFeed).toBeUndefined();
  });

  test('brief form keeps a valid channels array', () => {
    const parsed = campaignBriefFormSchema.parse({
      ...VALID_BRIEF,
      channels: [{ platform: 'instagram' }, { platform: 'youtube', seedChannels: ['UC1'] }],
    });
    expect(parsed.channels?.map((c) => c.platform)).toEqual(['instagram', 'youtube']);
  });

  test('an old brief without channels parses to channels: undefined', () => {
    expect(campaignBriefFormSchema.parse(VALID_BRIEF).channels).toBeUndefined();
  });

  test('a malformed channels value degrades to undefined, not a thrown brief', () => {
    const parsed = campaignBriefFormSchema.parse({ ...VALID_BRIEF, channels: 'oops' });
    expect(parsed.channels).toBeUndefined();
  });

  test('campaign.platforms degrades to undefined (NOT []) when absent or malformed', () => {
    expect(campaignSchema.parse(buildCampaign()).platforms).toBeUndefined();
    expect(
      campaignSchema.parse(buildCampaign({ platforms: 'oops' as never })).platforms,
    ).toBeUndefined();
  });

  test('campaign.platforms keeps a real list', () => {
    const parsed = campaignSchema.parse(buildCampaign({ platforms: ['instagram', 'youtube'] }));
    expect(parsed.platforms).toEqual(['instagram', 'youtube']);
  });
});

describe('campaign.fleetRunId (FIX 2)', () => {
  test('keeps a live fleet run id when present', () => {
    const parsed = campaignSchema.parse(buildCampaign({ fleetRunId: 'run-xyz' }));
    expect(parsed.fleetRunId).toBe('run-xyz');
  });

  test('defaults to null when the key is absent (older payloads)', () => {
    const { fleetRunId: _dropped, ...withoutFleet } = buildCampaign();
    expect(campaignSchema.parse(withoutFleet).fleetRunId).toBeNull();
  });

  test('degrades a malformed value to null rather than throwing', () => {
    expect(campaignSchema.parse(buildCampaign({ fleetRunId: 42 as never })).fleetRunId).toBeNull();
  });
});

describe('panelStateSchema', () => {
  test('accepts a complete payload', () => {
    expect(panelStateSchema.safeParse(buildPanelState()).success).toBe(true);
  });

  test('rejects a payload missing a top-level key', () => {
    const { MATCHES: _dropped, ...partial } = buildPanelState();
    expect(panelStateSchema.safeParse(partial).success).toBe(false);
  });

  test('coerces an unknown match status to new instead of failing', () => {
    const state = buildPanelState();
    const corrupted = {
      ...state,
      MATCHES: [{ ...state.MATCHES[0], status: 'banana' }],
    };
    const parsed = panelStateSchema.safeParse(corrupted);
    expect(parsed.success).toBe(true);
    if (parsed.success) expect(parsed.data.MATCHES[0]?.status).toBe('new');
  });

  test('coerces an unknown session flag to empty instead of failing', () => {
    const state = buildPanelState();
    const oddFlag = {
      ...state,
      SESSIONS: [{ ...state.SESSIONS[0], flag: 'mystery' }],
    };
    const parsed = panelStateSchema.safeParse(oddFlag);
    expect(parsed.success).toBe(true);
    if (parsed.success) expect(parsed.data.SESSIONS[0]?.flag).toBe('');
  });

  test('defaults a session runId to null when the engine omits it', () => {
    const state = buildPanelState();
    const [firstSession] = state.SESSIONS;
    if (!firstSession) throw new Error('fixture must include a session');
    const { runId: _dropped, ...sessionWithoutRunId } = firstSession;
    const parsed = panelStateSchema.safeParse({
      ...state,
      SESSIONS: [sessionWithoutRunId],
    });
    expect(parsed.success).toBe(true);
    if (parsed.success) expect(parsed.data.SESSIONS[0]?.runId).toBeNull();
  });
});

describe('matchSchema', () => {
  test('allows null sessionId and lang (engine may not know them)', () => {
    const state = buildPanelState();
    const match = { ...state.MATCHES[0], sessionId: null, lang: null };
    expect(matchSchema.safeParse(match).success).toBe(true);
  });

  test('recomputes id from the composite (campaignId, platform, commentId)', () => {
    // Identity is not the wire's to decide: a payload that flattens a lead to its
    // bare commentId (what the engine used to emit) must still parse to an id that
    // is unique per record, or two campaigns' leads collapse into one panel row.
    const base = buildPanelState().MATCHES[0];
    const a = matchSchema.safeParse({
      ...base, id: 'dup', commentId: 'dup', campaignId: 'cmp-a', platform: 'instagram',
    });
    const b = matchSchema.safeParse({
      ...base, id: 'dup', commentId: 'dup', campaignId: 'cmp-b', platform: 'instagram',
    });
    const x = matchSchema.safeParse({
      ...base, id: 'dup', commentId: 'dup', campaignId: 'cmp-a', platform: 'x',
    });
    expect(a.success && b.success && x.success).toBe(true);
    const ids = [a, b, x].map((r) => (r.success ? r.data.id : ''));
    expect(new Set(ids).size).toBe(3);
    expect(a.success && a.data.commentId).toBe('dup');  // raw comment id preserved
  });
});

describe('v3 backward compatibility', () => {
  test('parses a payload missing DASHBOARD/REPORTS/TEAM/INTEGRATIONS with safe defaults', () => {
    const {
      DASHBOARD: _d,
      REPORTS: _r,
      TEAM: _t,
      INTEGRATIONS: _i,
      ...legacy
    } = buildPanelState();
    const parsed = panelStateSchema.safeParse(legacy);
    expect(parsed.success).toBe(true);
    if (parsed.success) {
      expect(parsed.data.TEAM).toEqual([]);
      expect(parsed.data.INTEGRATIONS).toEqual([]);
      expect(parsed.data.DASHBOARD.month.leads.value).toBe(0);
      expect(parsed.data.REPORTS.month.labels).toEqual([]);
    }
  });

  test('parses an old-shape campaign without the v3 ops fields', () => {
    const state = buildPanelState();
    const legacyCampaign = {
      id: 'c1',
      name: 'Legacy',
      goalType: 'lead',
      status: 'live',
      threshold: 0.7,
      languages: [],
      extractFields: [],
      startedAt: 'Jun 1',
      brief: 'old',
    };
    const parsed = panelStateSchema.safeParse({ ...state, CAMPAIGNS: [legacyCampaign] });
    expect(parsed.success).toBe(true);
    if (parsed.success) {
      const c = parsed.data.CAMPAIGNS[0];
      expect(c?.budgetCap).toBe(0);
      expect(c?.cpl).toBeNull();
      expect(c?.spark).toEqual([]);
      expect(c?.platform).toBe('instagram');
    }
  });

  test('keeps a null cpl on a campaign', () => {
    const state = buildPanelState();
    const corrupted = { ...state, CAMPAIGNS: [{ ...state.CAMPAIGNS[0], cpl: null }] };
    const parsed = panelStateSchema.safeParse(corrupted);
    expect(parsed.success).toBe(true);
    if (parsed.success) expect(parsed.data.CAMPAIGNS[0]?.cpl).toBeNull();
  });

  test('allows a null team-member email', () => {
    const state = buildPanelState();
    const parsed = panelStateSchema.safeParse({
      ...state,
      TEAM: [{ ...state.TEAM[0], email: null }],
    });
    expect(parsed.success).toBe(true);
    if (parsed.success) expect(parsed.data.TEAM[0]?.email).toBeNull();
  });

  test('parses a payload missing RUN with idle defaults', () => {
    const { RUN: _run, ...legacy } = buildPanelState();
    const parsed = panelStateSchema.safeParse(legacy);
    expect(parsed.success).toBe(true);
    if (parsed.success) expect(parsed.data.RUN).toEqual({ active: null, recent: [] });
  });

  test('requires an id on the active run (run_id for the activity feed)', () => {
    const { id: _dropped, ...withoutId } = {
      id: 'run-1',
      scope: 'campaign',
      campaignId: 'cmp-001',
      mode: 'dry',
      startedAt: '2026-06-18T11:00:00Z',
    };
    expect(activeRunSchema.safeParse(withoutId).success).toBe(false);
    expect(activeRunSchema.safeParse({ ...withoutId, id: 'run-1' }).success).toBe(true);
  });

  test('parses a payload with an active run and recent history', () => {
    const state = buildPanelState({
      RUN: {
        active: { id: 'run-2', scope: 'campaign', campaignId: 'cmp-001', mode: 'live', startedAt: '2026-06-18T11:00:00Z', paused: false, launchSource: 'manual' },
        recent: [
          {
            id: 'run-1',
            scope: 'all',
            campaignId: null,
            mode: 'dry',
            startedAt: '2026-06-18T10:00:00Z',
            finishedAt: '2026-06-18T10:05:00Z',
            outcome: 'ok',
            summary: 'ran 2 ok',
            launchSource: 'manual',
          },
        ],
      },
    });
    const parsed = panelStateSchema.safeParse(state);
    expect(parsed.success).toBe(true);
    if (parsed.success) {
      expect(parsed.data.RUN.active?.mode).toBe('live');
      expect(parsed.data.RUN.recent[0]?.outcome).toBe('ok');
    }
  });
});

describe('generatedCampaignDraftSchema', () => {
  test('accepts a complete flat draft (form keys, comma-joined arrays)', () => {
    const draft = {
      name: 'Acme SaaS Lead Gen', objective: 'lead', platform: 'instagram',
      threshold: 0.7, budgetCap: 7500, goalTarget: 200, languages: 'en',
      relevanceDef: 'on-topic', matchDef: 'buyer intent', extractDef: '- phone',
      relevancePrompt: '', matchPrompt: '', visionPrompt: '',
      seedHashtags: 'projectmanagement, saas', seedAccounts: 'acme.io', seedChannels: '',
    };
    const parsed = generatedCampaignDraftSchema.safeParse(draft);
    expect(parsed.success).toBe(true);
    if (parsed.success) expect(parsed.data.name).toBe('Acme SaaS Lead Gen');
  });

  test('accepts a partial draft (only some fields present)', () => {
    const parsed = generatedCampaignDraftSchema.safeParse({ name: 'Just a name' });
    expect(parsed.success).toBe(true);
    if (parsed.success) {
      expect(parsed.data.name).toBe('Just a name');
      expect(parsed.data.platform).toBeUndefined();
    }
  });

  test('coerces a ragged string field to empty rather than throwing the draft out', () => {
    // A model returned a number where a string was expected — degrade, do not fail.
    const parsed = generatedCampaignDraftSchema.safeParse({ name: 'Ok', relevanceDef: 42 });
    expect(parsed.success).toBe(true);
    if (parsed.success) {
      expect(parsed.data.name).toBe('Ok');
      expect(parsed.data.relevanceDef).toBe('');
    }
  });

  test('drops a malformed numeric field (form keeps its default)', () => {
    const parsed = generatedCampaignDraftSchema.safeParse({ threshold: 'high' });
    expect(parsed.success).toBe(true);
    if (parsed.success) expect(parsed.data.threshold).toBeUndefined();
  });

  test('accepts an empty object (the manual path)', () => {
    expect(generatedCampaignDraftSchema.safeParse({}).success).toBe(true);
  });
});

describe('runActivitySchema', () => {
  test('accepts a well-formed activity payload', () => {
    const parsed = runActivitySchema.safeParse(buildRunActivity());
    expect(parsed.success).toBe(true);
    if (parsed.success) {
      expect(parsed.data.runId).toBe('run-001');
      expect(parsed.data.counters.matches).toBe(3);
      // v27/B3: `buildRunActivity()` is the ORG-facing shape, and an org caller can
      // never receive an event. For a payload WITH events, parse
      // `buildAdminRunActivity()` against `adminRunActivitySchema` instead.
      expect(parsed.data.events).toHaveLength(0);
      expect(parsed.data.eventsRedacted).toBe(true);
    }
  });

  test('requires a global id (the paging cursor / React key, not seq)', () => {
    const { id: _dropped, ...withoutId } = buildRunEvent();
    expect(runEventSchema.safeParse(withoutId).success).toBe(false);
    const parsed = runEventSchema.safeParse(buildRunEvent({ id: 142, seq: 3 }));
    expect(parsed.success).toBe(true);
    if (parsed.success) {
      expect(parsed.data.id).toBe(142);
      expect(parsed.data.seq).toBe(3);
    }
  });

  test('coerces an unknown event level to info instead of failing', () => {
    const parsed = runEventSchema.safeParse(buildRunEvent({ level: 'banana' as never }));
    expect(parsed.success).toBe(true);
    if (parsed.success) expect(parsed.data.level).toBe('info');
  });

  test('keeps a null detail (engine may not attach one)', () => {
    const parsed = runEventSchema.safeParse(buildRunEvent({ detail: null }));
    expect(parsed.success).toBe(true);
    if (parsed.success) expect(parsed.data.detail).toBeNull();
  });

  test('degrades a missing counter to 0 rather than throwing the page out', () => {
    const activity = buildRunActivity();
    const { matches: _dropped, ...partialCounters } = activity.counters;
    const parsed = runActivitySchema.safeParse({ ...activity, counters: partialCounters });
    expect(parsed.success).toBe(true);
    if (parsed.success) expect(parsed.data.counters.matches).toBe(0);
  });

  test('degrades malformed flags to an empty array', () => {
    const parsed = runActivitySchema.safeParse(buildRunActivity({ flags: 'oops' as never }));
    expect(parsed.success).toBe(true);
    if (parsed.success) expect(parsed.data.flags).toEqual([]);
  });

  test('defaults a missing cursor to 0', () => {
    const activity = buildRunActivity();
    const { cursor: _dropped, ...withoutCursor } = activity;
    const parsed = runActivitySchema.safeParse(withoutCursor);
    expect(parsed.success).toBe(true);
    if (parsed.success) expect(parsed.data.cursor).toBe(0);
  });

  test('parses a fleetJob block for a fleet-routed run (FIX 2)', () => {
    const parsed = runActivitySchema.safeParse(
      buildRunActivity({
        fleetJob: buildFleetJob({ jobId: 'job-9', status: 'running', leaseExpiresAt: null }),
      }),
    );
    expect(parsed.success).toBe(true);
    if (parsed.success) {
      expect(parsed.data.fleetJob?.jobId).toBe('job-9');
      expect(parsed.data.fleetJob?.status).toBe('running');
      expect(parsed.data.fleetJob?.leaseExpiresAt).toBeNull();
    }
  });

  test('keeps the fleet job failure reason (B6) instead of stripping it', () => {
    const parsed = runActivitySchema.safeParse(
      buildRunActivity({
        fleetJob: buildFleetJob({ status: 'failed', reason: 'cdp_unreachable', attempts: 3 }),
      }),
    );
    expect(parsed.success).toBe(true);
    if (parsed.success) {
      expect(parsed.data.fleetJob?.reason).toBe('cdp_unreachable');
      expect(parsed.data.fleetJob?.attempts).toBe(3);
      expect(parsed.data.fleetJob?.maxAttempts).toBe(3);
    }
  });

  test('an older server that omits reason/attempts degrades them to null', () => {
    const { reason: _r, attempts: _a, maxAttempts: _m, ...legacy } = buildFleetJob();
    const parsed = runActivitySchema.safeParse(buildRunActivity({ fleetJob: legacy as never }));
    expect(parsed.success).toBe(true);
    if (parsed.success) {
      expect(parsed.data.fleetJob?.reason).toBeNull();
      expect(parsed.data.fleetJob?.attempts).toBeNull();
    }
  });

  test('defaults fleetJob to null for an in-process run (key absent)', () => {
    const { fleetJob: _dropped, ...withoutFleet } = buildRunActivity();
    const parsed = runActivitySchema.safeParse(withoutFleet);
    expect(parsed.success).toBe(true);
    if (parsed.success) expect(parsed.data.fleetJob).toBeNull();
  });

  test('degrades a malformed fleetJob to null rather than throwing the page out', () => {
    const parsed = runActivitySchema.safeParse(buildRunActivity({ fleetJob: 'oops' as never }));
    expect(parsed.success).toBe(true);
    if (parsed.success) expect(parsed.data.fleetJob).toBeNull();
  });
});

// ---------------------------------------------------------------------------
// v27 redaction + plan limits. These fixtures are built INLINE rather than from
// @/test/fixtures on purpose: what is under test here is what the BOUNDARY does with
// a raw wire payload, including one from a bridge older than the field being added.
// ---------------------------------------------------------------------------

/** A minimally valid org-facing lead row, as `panel._build_matches` now emits it. */
const WIRE_LEAD = {
  id: 'cmp-001:instagram:c1',
  commentId: 'c1',
  campaignId: 'cmp-001',
  platform: 'instagram',
  sessionId: 's1',
  intent: 'Wants red Nike sneakers, size 42, in Tashkent',
  lang: 'ru',
  score: 0.82,
  reason: 'explicit buying intent',
  extracted: { phone: '+998901234567' },
  status: 'new',
  escalated: false,
  escalationCost: 0,
  capturedAt: { date: 'Jun 18', time: '11:04', ts: 1_718_708_640 },
  reelId: 'r1',
  statusBy: null,
  statusAt: null,
};

describe('matchSchema — v27 lead redaction', () => {
  test('keeps the derived intent', () => {
    const parsed = matchSchema.parse(WIRE_LEAD);
    expect(parsed.intent).toBe('Wants red Nike sneakers, size 42, in Tashkent');
  });

  test('STRIPS username and comment text even when a payload still carries them', () => {
    // The bridge no longer sends either, but z.object() dropping unknown keys is the
    // second line of defence: an older/rogue payload must not reach a component with
    // identity attached, or the redaction is one stale deployment from being undone.
    const parsed = matchSchema.parse({ ...WIRE_LEAD, username: 'aziz', text: 'how much?' });
    expect(parsed).not.toHaveProperty('username');
    expect(parsed).not.toHaveProperty('text');
  });

  test('STRIPS reelId — the post pointer left with the handle', () => {
    // WIRE_LEAD still carries `reelId` precisely so this asserts the stripping and not
    // just the absence of a key nobody sent. A reel id is `reelUrl()` away from a public
    // page showing the comment AND the handle, so a lead row holding one is the
    // redaction undone in a click. The only post pointer in the customer plane is on
    // `RevealedLead`, from the audited reveal.
    const parsed = matchSchema.parse(WIRE_LEAD);
    expect(WIRE_LEAD).toHaveProperty('reelId');
    expect(parsed).not.toHaveProperty('reelId');
  });

  test('defaults intent to "" when a pre-v27 bridge omits it (page must not blank)', () => {
    const { intent: _dropped, ...legacy } = WIRE_LEAD;
    const parsed = matchSchema.safeParse(legacy);
    expect(parsed.success).toBe(true);
    if (parsed.success) expect(parsed.data.intent).toBe('');
  });

  test('degrades a malformed intent to "" rather than throwing the lead out', () => {
    const parsed = matchSchema.safeParse({ ...WIRE_LEAD, intent: 42 });
    expect(parsed.success).toBe(true);
    if (parsed.success) expect(parsed.data.intent).toBe('');
  });
});

describe('tickerEntrySchema — v27', () => {
  const ROW = {
    id: 'cmp-001:instagram:c1',
    intent: 'Wants a CRM for a 12-person sales team',
    platform: 'instagram',
    score: 0.9,
    capturedAt: { date: 'Jun 18', time: '11:04' },
  };

  test('carries intent, never a handle', () => {
    const parsed = tickerEntrySchema.parse({ ...ROW, username: 'aziz' });
    expect(parsed.intent).toBe('Wants a CRM for a 12-person sales team');
    expect(parsed).not.toHaveProperty('username');
  });

  test('an omitted intent degrades to "" (placeholder), not a failed row', () => {
    const { intent: _dropped, ...legacy } = ROW;
    const parsed = tickerEntrySchema.safeParse(legacy);
    expect(parsed.success).toBe(true);
    if (parsed.success) expect(parsed.data.intent).toBe('');
  });
});

describe('billing — v27 plan limits', () => {
  const BILLING = {
    tier: 'free',
    interval: null,
    status: 'active',
    periodEnd: null,
    cancelAtPeriodEnd: false,
    leadCap: 10,
    leadsUsed: 3,
    campaignCap: 1,
    campaignsUsed: 1,
    revealCap: 10,
    revealsUsed: 4,
    maxRunLeads: 10,
    usageRatio: 0.3,
    nearLimit: false,
    tiers: [],
  };

  test('keeps the campaign allowance + per-run bound', () => {
    const parsed = billingSchema.parse(BILLING);
    expect(parsed.campaignCap).toBe(1);
    expect(parsed.campaignsUsed).toBe(1);
    expect(parsed.maxRunLeads).toBe(10);
  });

  test('a null campaignCap means UNLIMITED and must survive the boundary as null', () => {
    // The gate is `campaignCap !== null && used >= cap`. If null ever degraded to 0 a
    // paying org would find New Campaign disabled at zero campaigns.
    const parsed = billingSchema.parse({ ...BILLING, tier: 'pro', campaignCap: null });
    expect(parsed.campaignCap).toBeNull();
  });

  test('keeps the reveal allowance (Section F)', () => {
    const parsed = billingSchema.parse(BILLING);
    expect(parsed.revealCap).toBe(10);
    expect(parsed.revealsUsed).toBe(4);
  });

  test('a null revealCap means UNLIMITED and survives the boundary as null', () => {
    // Same rule as campaignCap: the meter reads `revealCap !== null`, so a null that
    // degraded to 0 would draw an at-limit bar for an org with no reveal cap at all.
    const parsed = billingSchema.parse({ ...BILLING, tier: 'pro', revealCap: null });
    expect(parsed.revealCap).toBeNull();
  });

  test('a pre-v27 bridge that omits the new keys parses uncapped, not locked out', () => {
    const {
      campaignCap: _c, campaignsUsed: _u, maxRunLeads: _m,
      revealCap: _rc, revealsUsed: _ru, ...legacy
    } = BILLING;
    const parsed = billingSchema.safeParse(legacy);
    expect(parsed.success).toBe(true);
    if (parsed.success) {
      expect(parsed.data.campaignCap).toBeNull();  // unlimited = no client-side block
      expect(parsed.data.campaignsUsed).toBe(0);
      expect(parsed.data.maxRunLeads).toBe(0);
      // A bridge without the reveal cap must not read as "0 reveals allowed" — that
      // would paint an at-limit meter on an org that has no such limit.
      expect(parsed.data.revealCap).toBeNull();
      expect(parsed.data.revealsUsed).toBe(0);
    }
  });

  test('each tier row carries its own campaignCap for the comparison grid', () => {
    const parsed = billingTierSchema.parse({
      tier: 'lite', displayName: 'Lite', leadCap: 50, campaignCap: 3,
      selfServe: true, prices: { month: 9.99, year: 99 },
    });
    expect(parsed.campaignCap).toBe(3);
    const legacy = billingTierSchema.parse({
      tier: 'scale', displayName: 'Scale', leadCap: 0,
      selfServe: false, prices: { month: null, year: null },
    });
    expect(legacy.campaignCap).toBeNull();
  });
});

describe('runActivitySchema — v27 redacted progress', () => {
  /** The org-facing payload exactly as `server._serve_run_activity` constructs it. */
  const ORG_ACTIVITY = {
    runId: 'run-001',
    finished: false,
    fleetJob: null,
    counters: {
      reelsSeen: 40, relevancePasses: 12, commentsScored: 30,
      matches: 7, spendUsd: 0.12, likes: 0, follows: 0,
    },
    phase: 'qualifying',
    leadsFound: 7,
    leadsDelivered: 7,
    itemsScanned: 40,
    relevantFound: 12,
    lastEventAt: 1_718_800_000.5,
    targetLeads: 10,
    events: [],
    eventsRedacted: true,
    flags: [],
    cursor: 0,
  };

  test('accepts the scalars-only payload with an empty events array', () => {
    const parsed = runActivitySchema.parse(ORG_ACTIVITY);
    expect(parsed.events).toEqual([]);
    expect(parsed.eventsRedacted).toBe(true);
    expect(parsed.phase).toBe('qualifying');
    expect(parsed.leadsFound).toBe(7);
    expect(parsed.targetLeads).toBe(10);
    expect(parsed.lastEventAt).toBe(1_718_800_000.5);
  });

  test('an unknown phase degrades to "working", never to a raw string or "done"', () => {
    // "done" would stop the poller on a live run; a raw internal phase would leak
    // engine vocabulary onto a customer's screen.
    const parsed = runActivitySchema.parse({ ...ORG_ACTIVITY, phase: 'jitter_backoff' });
    expect(parsed.phase).toBe('working');
    expect(runActivitySchema.parse({ ...ORG_ACTIVITY, phase: undefined }).phase).toBe('working');
  });

  test('leadsDelivered is null (UNKNOWN) when the server does not report it', () => {
    // A bridge older than E.5 omits it. Defaulting to 0 would make every such run look
    // like "found 7, delivered 0" — the dead-letter warning, on a perfectly healthy run.
    const { leadsDelivered: _dropped, ...legacy } = ORG_ACTIVITY;
    const parsed = runActivitySchema.parse(legacy);
    expect(parsed.leadsDelivered).toBeNull();
    expect(parsed.leadsFound).toBe(7);
  });

  test('keeps a leadsFound > leadsDelivered gap instead of reconciling it away', () => {
    // A dead-lettered run: 15 harvested on the worker, 0 acked to the cloud. Both
    // numbers must survive the boundary — rendering either alone is a lie (E.5).
    const parsed = runActivitySchema.parse({
      ...ORG_ACTIVITY, finished: true, phase: 'failed', leadsFound: 15, leadsDelivered: 0,
    });
    expect(parsed.leadsFound).toBe(15);
    expect(parsed.leadsDelivered).toBe(0);
  });

  test('a pre-v27 bridge (no progress block at all) still parses', () => {
    const {
      phase: _p, leadsFound: _l, leadsDelivered: _d, itemsScanned: _i,
      relevantFound: _r, lastEventAt: _e, targetLeads: _t, eventsRedacted: _x, ...legacy
    } = ORG_ACTIVITY;
    const parsed = runActivitySchema.safeParse(legacy);
    expect(parsed.success).toBe(true);
    if (parsed.success) {
      expect(parsed.data.eventsRedacted).toBe(false);  // it really is still sending events
      expect(parsed.data.phase).toBe('working');
      expect(parsed.data.leadsFound).toBe(0);
      expect(parsed.data.targetLeads).toBeNull();
      expect(parsed.data.lastEventAt).toBeNull();
    }
  });
});

describe('revealLeadResponseSchema — v27 reveal-on-demand', () => {
  test('unwraps one lead’s identity', () => {
    const parsed = revealLeadResponseSchema.parse({
      ok: true,
      data: {
        id: 'cmp-001:instagram:c1', commentId: 'c1', platform: 'instagram',
        username: 'aziz', text: 'how much?', reelId: 'r1',
      },
      error: null,
    });
    expect(parsed.data?.username).toBe('aziz');
    expect(parsed.data?.reelId).toBe('r1');
    // The echoed identity lets the drawer check the answer is for the lead it asked
    // about before painting a handle on screen.
    expect(parsed.data?.id).toBe('cmp-001:instagram:c1');
  });

  test('REJECTS a malformed identity rather than degrading it to blanks', () => {
    // Every other schema here degrades. This one must not: a blank handle would read
    // as "this person has no handle" instead of "the reveal did not work".
    expect(
      revealLeadResponseSchema.safeParse({
        ok: true,
        data: { id: 'cmp-001:instagram:c1', commentId: 'c1', platform: 'instagram',
                username: 'aziz' },
        error: null,
      }).success,
    ).toBe(false);
  });

  test('carries a refusal (403 viewer / 404 unknown lead) as data:null + error', () => {
    const parsed = revealLeadResponseSchema.parse({
      ok: false, data: null, error: 'your role does not permit this action',
    });
    expect(parsed.data).toBeNull();
    expect(parsed.error).toContain('does not permit');
  });
});

describe('delivery pairing (E.5 / E.7)', () => {
  test('runActivity carries the trio and keeps a not_delivered verdict intact', () => {
    const parsed = runActivitySchema.parse({
      runId: 'run-001',
      finished: true,
      counters: {
        reelsSeen: 40, relevancePasses: 12, commentsScored: 30,
        matches: 15, spendUsd: 1.4, likes: 0, follows: 0,
      },
      phase: 'failed',
      leadsFound: 15,
      leadsDelivered: 0,
      delivery: 'not_delivered',
      itemsScanned: 40,
      relevantFound: 12,
      lastEventAt: 1_718_800_000,
      targetLeads: 10,
      events: [],
      eventsRedacted: true,
      flags: [],
      cursor: 0,
    });
    // A dead-lettered run: 15 harvested on the worker, 0 acked to the cloud. Rendering
    // either number alone is a lie, so both must survive the boundary with the verdict.
    expect(parsed.delivery).toBe('not_delivered');
    expect(parsed.leadsFound).toBe(15);
    expect(parsed.leadsDelivered).toBe(0);
    // targetLeads is a TARGET, not a ceiling — 15 against a target of 10 is legal.
    expect(parsed.targetLeads).toBe(10);
  });

  test('a mid-flight fleet gap is "pending", not a fault', () => {
    const base = campaignSchema.parse(
      buildCampaign({ leadsFound: 7, leadsDelivered: 0, delivery: 'pending' } as never),
    );
    expect(base.delivery).toBe('pending');
    expect(base.leadsFound).toBe(7);
  });

  test('an unknown/absent delivery word degrades to "delivered" (no false alarm)', () => {
    // A bridge that cannot report a gap cannot have one to report. Degrading the other
    // way would stamp the not-delivered warning on every run against a lagging server.
    expect(campaignSchema.parse(buildCampaign()).delivery).toBe('delivered');
    expect(
      campaignSchema.parse(buildCampaign({ delivery: 'exploded' } as never)).delivery,
    ).toBe('delivered');
  });

  test('the campaign pair is null (fall back to `leads`) when a bridge omits it', () => {
    const parsed = campaignSchema.parse(buildCampaign());
    expect(parsed.leadsFound).toBeNull();
    expect(parsed.leadsDelivered).toBeNull();
  });
});

describe('reelSchema', () => {
  test('parses an org reel that omits the watchlist fields', () => {
    // v27: `panel._build_reels` ships `expiresInDays`/`newSinceLastPoll` only to the
    // superadmin plane. Both are watchlist-derived, and the engine writes a watchlist
    // row only for a post that produced a lead — so either one alone marks WHICH
    // scanned post to open to read the handle and the comment, with no audited reveal.
    // These MUST stay optional rather than `.catch(0)`: `REELS` is `.catch([])`, so a
    // required field would blank the whole watchlist instead of failing loudly.
    const parsed = reelSchema.parse(buildReel());
    expect(parsed.newSinceLastPoll).toBeUndefined();
    expect(parsed.expiresInDays).toBeUndefined();
    expect(parsed.id).toBe('r1');
  });

  test('still parses a superadmin reel that carries them', () => {
    const parsed = reelSchema.parse(
      buildReel({ expiresInDays: 5, newSinceLastPoll: 3 }),
    );
    expect(parsed.newSinceLastPoll).toBe(3);
    expect(parsed.expiresInDays).toBe(5);
  });
});
