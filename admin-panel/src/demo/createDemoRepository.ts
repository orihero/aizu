/**
 * Demo-capture repository: a thin, deterministic subclass of the test-seam
 * `FakePanelRepository`, seeded with the fixed state in `demoFixtures.ts`.
 *
 * Only loaded when `VITE_DEMO_CAPTURE=1` (see `main.tsx`) — never bundled into
 * a production build, since the import is dynamic and the branch is dead code
 * once the env var is unset.
 */
import { FakePanelRepository } from '@/test/fakePanelRepository';
import { appError, err, ok, type Result } from '@/shared/lib/result';
import { buildCampaign, buildRunRecord } from '@/test/fixtures';
import type { PanelRepository } from '@/shared/api/panelRepository';
import type {
  Campaign, CampaignInput, GenerateCampaignInput, GeneratedCampaignDraft, InterviewRequest,
  InterviewResponse, RunActivity, RunCounters, RunInput, RunStartResult,
} from '@/shared/types/domain';
import {
  DEMO_CAMPAIGN_ID,
  DEMO_GENERATED_DRAFT,
  DEMO_INTERVIEW_ROUND1,
  DEMO_PANEL_STATE,
  DEMO_RUN_ID,
  DEMO_RUN_SCRIPT,
  DEMO_RUN_STARTED_AT,
  DEMO_USER,
} from './demoFixtures';

/** How many scripted run events are revealed on each `fetchRunActivity` poll —
 * keyed off poll COUNT (react-query's ~2s interval), never wall-clock, so the
 * replay is deterministic across captures regardless of machine speed. */
const RELEASE_PER_POLL = 2;

const EMPTY_COUNTERS: RunCounters = {
  reelsSeen: 0, relevancePasses: 0, commentsScored: 0, matches: 0, spendUsd: 0, likes: 0, follows: 0,
};

/** On-camera pacing: how long the synthesize call stays in flight, so
 * `CampaignGenerateProgress` visibly walks all three staged lines (they advance
 * every 1.6s and clamp on the last) before `CampaignReview` resolves. */
const GENERATE_IN_FLIGHT_MS = 5200;
/** A shorter beat for the interview kick-off (Continue → first question). */
const INTERVIEW_IN_FLIGHT_MS = 1200;

function pace(ms: number): Promise<void> {
  return new Promise((resolve) => { setTimeout(resolve, ms); });
}

/** comma/newline-separated free text → trimmed, de-blanked list (mirrors the
 * form layer's splitList, duplicated locally to avoid reaching into a
 * feature-private helper). */
function splitFields(raw: string | undefined): string[] {
  if (!raw) return [];
  return raw.split(/[,\n]/).map((s) => s.trim()).filter((s) => s.length > 0);
}

/** The warmth verdict for a freshly-created (never run) draft campaign —
 * below the gate, so its Run affordance shows "Warming — not ready" rather
 * than implying a brand-new draft could go live immediately. */
function freshWarmth(): Campaign['warmth'] {
  return {
    score: 28,
    state: 'warming',
    gateMin: 40,
    gateFull: 70,
    meetsGate: false,
    components: { age: 0.2, ramp: 0.2, network: 0.2, profile: 0.4, trust: 0.2 },
    trend: [10, 16, 22, 28],
    etaHours: 40,
    checkedAt: 'Aug 4',
  };
}

/** Patch an existing campaign in place — covers both a full edit-save (brief
 * present) and a status-only write like Activate/Pause/Resume (brief absent). */
function patchCampaign(existing: Campaign, input: CampaignInput): Campaign {
  const brief = input.brief;
  const platforms = brief
    ? (brief.channels && brief.channels.length > 0 ? brief.channels.map((c) => c.platform) : [brief.platform])
    : existing.platforms;
  return {
    ...existing,
    ...(input.displayName !== undefined ? { name: input.displayName } : {}),
    ...(input.status !== undefined ? { status: input.status } : {}),
    ...(input.budgetCap !== undefined ? { budgetCap: input.budgetCap } : {}),
    ...(input.goalTarget !== undefined ? { goalTarget: input.goalTarget } : {}),
    ...(brief
      ? {
        briefForm: brief,
        platform: brief.platform,
        platforms,
        threshold: brief.threshold,
        languages: brief.languageMix,
        extractFields: splitFields(brief.extractDef),
      }
      : {}),
  };
}

/** Build a brand-new draft campaign from the wizard's create input. */
function newDraftCampaign(input: CampaignInput): Campaign {
  const brief = input.brief;
  const platforms = brief
    ? (brief.channels && brief.channels.length > 0 ? brief.channels.map((c) => c.platform) : [brief.platform])
    : ['instagram'];
  return buildCampaign({
    id: input.campaignId,
    name: input.displayName ?? input.campaignId,
    status: input.status ?? 'draft',
    threshold: brief?.threshold ?? 0.7,
    languages: brief?.languageMix ? [...brief.languageMix] : ['en'],
    extractFields: splitFields(brief?.extractDef),
    startedAt: '—',
    brief: brief?.goal ?? '',
    platform: platforms[0] ?? 'instagram',
    platforms,
    budgetCap: input.budgetCap ?? 0,
    goalTarget: input.goalTarget ?? null,
    briefForm: brief ?? null,
    spent: 0,
    leads: 0,
    cpl: null,
    spark: [],
    warmth: freshWarmth(),
    archivedAt: null,
    pausedReason: null,
    scheduleEnabled: false,
    fleetRunId: null,
  });
}

export class DemoPanelRepository extends FakePanelRepository {
  /** Polls served so far for the (single) demo run, keyed by run id. */
  private readonly pollCounts = new Map<string, number>();

  constructor() {
    super(DEMO_PANEL_STATE);
    this.currentUser = DEMO_USER;
    // One interview round (a `platforms` question, all six pre-suggested); the
    // queue then empties, so round 2 falls through to the base class's default
    // done:true reply and the wizard proceeds straight to synthesis.
    this.interviewResults = [DEMO_INTERVIEW_ROUND1];
    this.generatedDraft = DEMO_GENERATED_DRAFT;
  }

  /** Demo pacing: hold the synthesize call in flight long enough for the
   * staged progress copy to play through on camera (capture 03 / beat 03). */
  override async generateCampaign(input: GenerateCampaignInput): Promise<Result<GeneratedCampaignDraft>> {
    await pace(GENERATE_IN_FLIGHT_MS);
    return super.generateCampaign(input);
  }

  /** Same idea, shorter: Continue → first interview card gets a visible beat. */
  override async runInterview(input: InterviewRequest): Promise<Result<InterviewResponse>> {
    await pace(INTERVIEW_IN_FLIGHT_MS);
    return super.runInterview(input);
  }

  private consumeFailure(): boolean {
    if (this.failNextWrite) {
      this.failNextWrite = false;
      return true;
    }
    return false;
  }

  /** Create (wizard Save) OR patch-by-id (Activate/Pause/Resume all reuse this
   * same write) — either way the campaign list re-renders from real state. */
  override createCampaign(input: CampaignInput): Promise<Result<void>> {
    if (this.consumeFailure()) {
      return Promise.resolve(err(appError('http', 'simulated write failure', 500)));
    }
    this.campaignCreates.push(input);
    const campaigns = this.state.CAMPAIGNS;
    const idx = campaigns.findIndex((c) => c.id === input.campaignId);
    const nextCampaigns = idx >= 0
      ? campaigns.map((c, i) => (i === idx ? patchCampaign(c, input) : c))
      : [...campaigns, newDraftCampaign(input)];
    this.state = { ...this.state, CAMPAIGNS: nextCampaigns };
    return Promise.resolve(ok(undefined));
  }

  /** Starts the single scripted live run and arms the activity replay. */
  override runCampaign(input: RunInput): Promise<Result<RunStartResult>> {
    if (this.runStartFailure === 'agent_not_ready') {
      return Promise.resolve(
        err(appError('http', this.runStartAgentNotReadyDetail, 409, 'agent_not_ready')),
      );
    }
    if (this.consumeFailure()) {
      return Promise.resolve(err(appError('http', 'simulated write failure', 500)));
    }
    this.runRequests.push(input);
    this.pollCounts.set(DEMO_RUN_ID, 0);
    this.state = {
      ...this.state,
      RUN: {
        active: {
          id: DEMO_RUN_ID,
          scope: 'campaign',
          campaignId: input.campaignId ?? DEMO_CAMPAIGN_ID,
          // 'live' (not 'dry') — RunBlock/RunDrawer read better on screen live.
          mode: 'live',
          startedAt: DEMO_RUN_STARTED_AT,
          paused: false,
          launchSource: 'manual',
        },
        recent: this.state.RUN.recent,
      },
    };
    return Promise.resolve(ok(this.nextRunStart));
  }

  /** Progressive replay of the scripted run: reveals `RELEASE_PER_POLL` more
   * events each time this is called (poll COUNT, not wall-clock), with
   * counters ticking up monotonically. Once the whole script has played, the
   * run is retired into RUN.recent — same transition the real bridge makes
   * once a run exits — so the card/drawer fall back to idle on the next sync. */
  override fetchRunActivity(runId: string, afterSeq = 0): Promise<Result<RunActivity>> {
    this.runActivityFetches.push({ runId, afterSeq });
    if (runId !== DEMO_RUN_ID) {
      return Promise.resolve(err(appError('http', `unknown run ${runId}`, 404)));
    }
    const polls = (this.pollCounts.get(runId) ?? 0) + 1;
    this.pollCounts.set(runId, polls);
    const releaseCount = Math.min(DEMO_RUN_SCRIPT.length, polls * RELEASE_PER_POLL);
    const released = DEMO_RUN_SCRIPT.slice(0, releaseCount);
    const events = released.map((s) => s.event).filter((e) => e.id > afterSeq);
    const finished = releaseCount >= DEMO_RUN_SCRIPT.length;
    const last = released.at(-1);
    const counters = last ? last.counters : EMPTY_COUNTERS;

    if (finished && this.state.RUN.active !== null) {
      const active = this.state.RUN.active;
      this.state = {
        ...this.state,
        RUN: {
          active: null,
          recent: [
            buildRunRecord({
              id: runId,
              scope: active.scope,
              campaignId: active.campaignId,
              mode: active.mode,
              startedAt: active.startedAt,
              finishedAt: DEMO_RUN_STARTED_AT,
              outcome: 'ok',
              summary: `ran 1 ok — ${String(counters.matches)} match(es)`,
              launchSource: active.launchSource,
            }),
            ...this.state.RUN.recent,
          ],
        },
      };
    }

    return Promise.resolve(ok({
      runId, finished, counters, events, flags: [], cursor: releaseCount, fleetJob: null,
    }));
  }
}

export function createDemoRepository(): PanelRepository {
  return new DemoPanelRepository();
}
