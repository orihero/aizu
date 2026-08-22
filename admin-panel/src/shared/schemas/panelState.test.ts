import { describe, expect, test } from 'vitest';
import {
  buildCampaign,
  buildFleetJob,
  buildPanelState,
  buildRunActivity,
  buildRunEvent,
} from '@/test/fixtures';
import {
  activeRunSchema,
  campaignBriefFormSchema,
  campaignSchema,
  channelEntrySchema,
  generatedCampaignDraftSchema,
  matchSchema,
  panelStateSchema,
  runActivitySchema,
  runEventSchema,
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
      expect(parsed.data.events).toHaveLength(1);
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
