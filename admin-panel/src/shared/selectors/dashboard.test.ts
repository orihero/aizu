import { describe, expect, test } from 'vitest';
import { buildDashboardPeriod, buildLeadNote, buildMatch, buildStatusChange } from '@/test/fixtures';
import {
  selectChannelData,
  selectFunnelStages,
  selectNeedsAttention,
  selectPeriod,
  selectPipelineStages,
  selectStatusDistribution,
  selectTeamActivity,
  selectWinRate,
} from './dashboard';

describe('selectPeriod', () => {
  test('picks the requested period', () => {
    const dashboard = {
      today: buildDashboardPeriod({ leads: { value: 1, delta: '0%', spark: [] } }),
      week: buildDashboardPeriod({ leads: { value: 7, delta: '0%', spark: [] } }),
      month: buildDashboardPeriod({ leads: { value: 30, delta: '0%', spark: [] } }),
    };
    expect(selectPeriod(dashboard, 'today').leads.value).toBe(1);
    expect(selectPeriod(dashboard, 'month').leads.value).toBe(30);
  });
});

describe('selectChannelData', () => {
  test('maps channel tuples to grouped-bar rows', () => {
    const rows = selectChannelData([
      { platform: 'instagram', current: 5, previous: 3 },
      { platform: 'youtube', current: 2, previous: 4 },
    ]);
    expect(rows).toEqual([
      { category: 'instagram', current: 5, previous: 3 },
      { category: 'youtube', current: 2, previous: 4 },
    ]);
  });
});

describe('selectFunnelStages', () => {
  test('orders reels → relevant → scored → leads', () => {
    const stages = selectFunnelStages({ reels: 100, relevant: 40, scored: 200, matches: 12 });
    expect(stages.map((s) => s.name)).toEqual(['Reels seen', 'Relevant', 'Scored', 'Leads']);
    expect(stages.map((s) => s.value)).toEqual([100, 40, 200, 12]);
  });
});

describe('v6 lead-pipeline selectors', () => {
  const matches = [
    buildMatch({ id: 'a', commentId: 'a', status: 'new' }),
    buildMatch({ id: 'b', commentId: 'b', status: 'in_progress' }),
    buildMatch({ id: 'c', commentId: 'c', status: 'interested' }),
    buildMatch({ id: 'd', commentId: 'd', status: 'closed' }),
    buildMatch({ id: 'e', commentId: 'e', status: 'couldnt_connect' }),
  ];

  test('selectStatusDistribution returns all six statuses in column order', () => {
    const dist = selectStatusDistribution(matches);
    expect(dist.map((d) => d.name)).toEqual([
      'New', 'In Progress', 'Interested', 'Closed', "Couldn't Connect", 'Archived',
    ]);
    expect(dist.map((d) => d.value)).toEqual([1, 1, 1, 1, 1, 0]);
  });

  test('selectPipelineStages maps the four progression stages', () => {
    expect(selectPipelineStages(matches).map((s) => s.value)).toEqual([1, 1, 1, 1]);
  });

  test('selectWinRate counts interested + closed over total', () => {
    expect(selectWinRate(matches)).toBeCloseTo(2 / 5);
    expect(selectWinRate([])).toBe(0);
  });

  test('selectTeamActivity groups status changes by actor, busiest first', () => {
    const withHistory = [
      buildMatch({ id: 'x', commentId: 'x', statusHistory: [
        buildStatusChange({ by: 'amy@co' }), buildStatusChange({ by: 'bo@co' }),
      ] }),
      buildMatch({ id: 'y', commentId: 'y', statusHistory: [buildStatusChange({ by: 'amy@co' })] }),
    ];
    const rows = selectTeamActivity(withHistory);
    expect(rows[0]).toEqual({ category: 'amy@co', current: 2, previous: 0 });
    expect(rows[1]).toEqual({ category: 'bo@co', current: 1, previous: 0 });
  });

  test('selectNeedsAttention counts stuck / couldnt-connect / idle against thresholds', () => {
    const now = 1_000_000_000;
    const old = now - 30 * 86_400;
    const data = [
      // Stuck: in_progress with stale last activity.
      buildMatch({ id: 's', commentId: 's', status: 'in_progress',
        capturedAt: { date: '—', time: '—', ts: old }, statusHistory: [buildStatusChange({ atTs: old })] }),
      // Couldn't connect (terminal — not idle).
      buildMatch({ id: 'c', commentId: 'c', status: 'couldnt_connect',
        capturedAt: { date: '—', time: '—', ts: old } }),
      // Idle open lead (new, ancient capture, no activity).
      buildMatch({ id: 'i', commentId: 'i', status: 'new',
        capturedAt: { date: '—', time: '—', ts: old } }),
      // Fresh lead — counts for nothing.
      buildMatch({ id: 'f', commentId: 'f', status: 'new',
        capturedAt: { date: '—', time: '—', ts: now } }),
    ];
    const a = selectNeedsAttention(data, { now });
    expect(a.stuckInProgress).toBe(1);
    expect(a.couldntConnect).toBe(1);
    expect(a.noActivity).toBe(2); // the stuck in_progress lead is also idle+open
  });

  test('selectNeedsAttention ignores a recently-noted lead', () => {
    const now = 1_000_000_000;
    const recent = now - 60;
    const data = [
      buildMatch({ id: 'n', commentId: 'n', status: 'new',
        capturedAt: { date: '—', time: '—', ts: now - 30 * 86_400 },
        notes: [buildLeadNote({ createdAtTs: recent })] }),
    ];
    expect(selectNeedsAttention(data, { now }).noActivity).toBe(0);
  });
});
