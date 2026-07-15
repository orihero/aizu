import { describe, expect, test } from 'vitest';
import { buildActiveRun, buildCampaign, buildRunBlock, buildRunRecord } from '@/test/fixtures';
import type { CampaignBriefForm } from '@/shared/types/domain';
import {
  budgetPct,
  isArchived,
  isRunnable,
  isScheduled,
  scheduleSummary,
  isWarmEnough,
  selectActiveCampaignCount,
  selectCampaignIsRunnable,
  selectCampaignsByStatus,
  selectIsAnyRunActive,
  selectIsRunPaused,
  selectIsCampaignRunning,
  selectLastRunForCampaign,
  selectRunnableCampaigns,
  selectStatusFilterCounts,
  warmthTier,
} from './campaigns';

const WARM = buildCampaign().warmth;   // fixture default is a warm (full) account
const COLD = { ...WARM, score: 30, state: 'warming' as const, meetsGate: false };

const BRIEF: CampaignBriefForm = {
  platform: 'instagram',
  goal: 'lead',
  threshold: 0.7,
  languageMix: ['en'],
  relevanceDef: 'saas product',
  matchDef: 'buyer intent',
  extractDef: '- phone',
  relevancePrompt: '',
  matchPrompt: '',
  visionPrompt: '',
  seedHashtags: [],
  seedAccounts: [],
  seedChannels: [],
};

const campaigns = [
  buildCampaign({ id: 'a', status: 'live', spent: 5, budgetCap: 20 }),
  buildCampaign({ id: 'b', status: 'paused', spent: 18, budgetCap: 20 }),
  buildCampaign({ id: 'c', status: 'draft', spent: 0, budgetCap: 0 }),
];

describe('budgetPct', () => {
  test('computes percentage of cap, capped at 100', () => {
    expect(budgetPct(buildCampaign({ spent: 5, budgetCap: 20 }))).toBe(25);
    expect(budgetPct(buildCampaign({ spent: 40, budgetCap: 20 }))).toBe(100);
  });

  test('is 0 when no cap is set', () => {
    expect(budgetPct(buildCampaign({ spent: 5, budgetCap: 0 }))).toBe(0);
  });
});

describe('selectActiveCampaignCount', () => {
  test('counts only live campaigns', () => {
    expect(selectActiveCampaignCount(campaigns)).toBe(1);
  });
});

describe('selectCampaignsByStatus', () => {
  test('all returns everything; a status filters', () => {
    expect(selectCampaignsByStatus(campaigns, 'all')).toHaveLength(3);
    expect(selectCampaignsByStatus(campaigns, 'paused').map((c) => c.id)).toEqual(['b']);
  });

  test('archived campaigns are hidden from all and surface only under archived', () => {
    const withArchived = [
      ...campaigns,
      buildCampaign({ id: 'z', status: 'paused', archivedAt: '2026-06-29T00:00:00+00:00' }),
    ];
    // 'all' and 'paused' both exclude the archived row.
    expect(selectCampaignsByStatus(withArchived, 'all').map((c) => c.id)).toEqual(['a', 'b', 'c']);
    expect(selectCampaignsByStatus(withArchived, 'paused').map((c) => c.id)).toEqual(['b']);
    expect(selectCampaignsByStatus(withArchived, 'archived').map((c) => c.id)).toEqual(['z']);
  });
});

describe('selectStatusFilterCounts', () => {
  test('tallies each status plus all and archived', () => {
    const counts = selectStatusFilterCounts(campaigns);
    expect(counts).toEqual({ all: 3, live: 1, paused: 1, draft: 1, ended: 0, archived: 0 });
  });

  test('archived rows count only under archived, never toward the active buckets', () => {
    const withArchived = [
      buildCampaign({ id: 'a', status: 'live' }),
      buildCampaign({ id: 'z', status: 'paused', archivedAt: '2026-06-29T00:00:00+00:00' }),
    ];
    expect(selectStatusFilterCounts(withArchived)).toEqual({
      all: 1, live: 1, paused: 0, draft: 0, ended: 0, archived: 1,
    });
  });
});

describe('isScheduled / scheduleSummary', () => {
  test('isScheduled tracks the scheduleEnabled flag', () => {
    expect(isScheduled(buildCampaign({ scheduleEnabled: false }))).toBe(false);
    expect(isScheduled(buildCampaign({ scheduleEnabled: true }))).toBe(true);
  });

  test('scheduleSummary renders a human cadence, or null when unscheduled', () => {
    expect(scheduleSummary(buildCampaign({ scheduleEnabled: false }))).toBeNull();
    expect(
      scheduleSummary(buildCampaign({ scheduleEnabled: true, scheduleKind: 'daily', scheduleHour: 9, scheduleMinute: 0 })),
    ).toBe('Daily 09:00');
    expect(
      scheduleSummary(buildCampaign({ scheduleEnabled: true, scheduleKind: 'weekdays', scheduleHour: 14, scheduleMinute: 30 })),
    ).toBe('Weekdays 14:30');
    expect(
      scheduleSummary(buildCampaign({ scheduleEnabled: true, scheduleKind: 'weekly', scheduleDow: 0, scheduleHour: 9, scheduleMinute: 5 })),
    ).toBe('Weekly Mon 09:05');
  });
});

describe('isArchived', () => {
  test('true only when archivedAt is set', () => {
    expect(isArchived(buildCampaign({ archivedAt: null }))).toBe(false);
    expect(isArchived(buildCampaign({ archivedAt: '2026-06-29T00:00:00+00:00' }))).toBe(true);
  });

  test('an archived campaign is never runnable, even live with a brief', () => {
    expect(
      isRunnable(buildCampaign({ status: 'live', briefForm: BRIEF, archivedAt: '2026-06-29T00:00:00+00:00' })),
    ).toBe(false);
  });
});

describe('isRunnable', () => {
  test('live campaign with a brief is runnable', () => {
    expect(isRunnable(buildCampaign({ status: 'live', briefForm: BRIEF }))).toBe(true);
  });

  test('paused campaign with a brief is runnable', () => {
    expect(isRunnable(buildCampaign({ status: 'paused', briefForm: BRIEF }))).toBe(true);
  });

  test('draft campaign with a brief is runnable', () => {
    expect(isRunnable(buildCampaign({ status: 'draft', briefForm: BRIEF }))).toBe(true);
  });

  test('draft campaign without a brief is not runnable', () => {
    expect(isRunnable(buildCampaign({ status: 'draft', briefForm: null }))).toBe(false);
  });
});

describe('isWarmEnough', () => {
  test('true when server says meetsGate and the score clears gateMin', () => {
    expect(isWarmEnough(buildCampaign({ warmth: WARM }))).toBe(true);
  });

  test('false when below the gate', () => {
    expect(isWarmEnough(buildCampaign({ warmth: COLD }))).toBe(false);
  });

  test('false when the verdict and score disagree (trust check)', () => {
    const inconsistent = { ...WARM, score: 10, meetsGate: true };
    expect(isWarmEnough(buildCampaign({ warmth: inconsistent }))).toBe(false);
  });
});

describe('selectCampaignIsRunnable', () => {
  test('needs a runnable brief, a warm account, and no active run', () => {
    const idle = buildRunBlock();
    expect(selectCampaignIsRunnable(
      buildCampaign({ status: 'live', briefForm: BRIEF, warmth: WARM }), idle)).toBe(true);
    // cold account blocks even a runnable brief
    expect(selectCampaignIsRunnable(
      buildCampaign({ status: 'live', briefForm: BRIEF, warmth: COLD }), idle)).toBe(false);
    // an active run blocks everything
    expect(selectCampaignIsRunnable(
      buildCampaign({ status: 'live', briefForm: BRIEF, warmth: WARM }),
      buildRunBlock({ active: buildActiveRun() }))).toBe(false);
  });
});

describe('warmthTier', () => {
  test('maps a raw score to a tier', () => {
    expect(warmthTier(30)).toBe('warming');
    expect(warmthTier(55)).toBe('ready');
    expect(warmthTier(85)).toBe('full');
  });

  test('ended campaign is never runnable even with a brief', () => {
    expect(isRunnable(buildCampaign({ status: 'ended', briefForm: BRIEF }))).toBe(false);
  });
});

describe('selectRunnableCampaigns', () => {
  test('keeps only campaigns the engine can run', () => {
    const list = [
      buildCampaign({ id: 'a', status: 'live', briefForm: BRIEF }),
      buildCampaign({ id: 'b', status: 'draft', briefForm: null }),
      buildCampaign({ id: 'c', status: 'ended', briefForm: BRIEF }),
    ];
    expect(selectRunnableCampaigns(list).map((c) => c.id)).toEqual(['a']);
  });
});

describe('selectIsAnyRunActive', () => {
  test('false for an idle run block, true once a run is active', () => {
    expect(selectIsAnyRunActive(buildRunBlock())).toBe(false);
    expect(selectIsAnyRunActive(buildRunBlock({ active: buildActiveRun() }))).toBe(true);
  });
});

describe('selectIsRunPaused', () => {
  test('true only when the active run is paused', () => {
    expect(selectIsRunPaused(buildRunBlock())).toBe(false);
    expect(selectIsRunPaused(buildRunBlock({ active: buildActiveRun({ paused: false }) }))).toBe(false);
    expect(selectIsRunPaused(buildRunBlock({ active: buildActiveRun({ paused: true }) }))).toBe(true);
  });
});

describe('selectIsCampaignRunning', () => {
  test('true only for the campaign whose single run is in flight', () => {
    const run = buildRunBlock({ active: buildActiveRun({ scope: 'campaign', campaignId: 'cmp-001' }) });
    expect(selectIsCampaignRunning(run, 'cmp-001')).toBe(true);
    expect(selectIsCampaignRunning(run, 'cmp-002')).toBe(false);
  });

  test('false for a batch (all) run even when ids would match', () => {
    const run = buildRunBlock({ active: buildActiveRun({ scope: 'all', campaignId: null }) });
    expect(selectIsCampaignRunning(run, 'cmp-001')).toBe(false);
  });

  test('false when no run is active', () => {
    expect(selectIsCampaignRunning(buildRunBlock(), 'cmp-001')).toBe(false);
  });
});

describe('selectLastRunForCampaign', () => {
  test('returns the most recent finished run for the campaign', () => {
    const run = buildRunBlock({
      recent: [
        buildRunRecord({ id: 'r1', campaignId: 'cmp-001', startedAt: '2026-06-18T10:00:00Z', summary: 'old' }),
        buildRunRecord({ id: 'r2', campaignId: 'cmp-001', startedAt: '2026-06-18T12:00:00Z', summary: 'new' }),
      ],
    });
    expect(selectLastRunForCampaign(run, 'cmp-001')?.summary).toBe('new');
  });

  test('surfaces a failed run for the campaign', () => {
    const run = buildRunBlock({
      recent: [buildRunRecord({ campaignId: 'cmp-001', outcome: 'error', summary: 'connect ECONNREFUSED 127.0.0.1:9333' })],
    });
    const last = selectLastRunForCampaign(run, 'cmp-001');
    expect(last?.outcome).toBe('error');
    expect(last?.summary).toContain('ECONNREFUSED');
  });

  test('ignores other campaigns and batch (all) runs', () => {
    const run = buildRunBlock({
      recent: [
        buildRunRecord({ id: 'r1', scope: 'campaign', campaignId: 'cmp-002' }),
        buildRunRecord({ id: 'r2', scope: 'all', campaignId: null, startedAt: '2026-06-18T13:00:00Z' }),
      ],
    });
    expect(selectLastRunForCampaign(run, 'cmp-001')).toBeNull();
  });

  test('null when there is no recent history', () => {
    expect(selectLastRunForCampaign(buildRunBlock(), 'cmp-001')).toBeNull();
  });
});

describe('selectActiveCampaignCount (live boundary)', () => {
  test('is 0 when no campaign is live', () => {
    const noLive = [
      buildCampaign({ id: 'a', status: 'paused' }),
      buildCampaign({ id: 'b', status: 'draft' }),
    ];
    expect(selectActiveCampaignCount(noLive)).toBe(0);
  });
});
