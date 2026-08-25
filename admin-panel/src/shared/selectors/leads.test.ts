import { describe, expect, test } from 'vitest';
import { buildMatch } from '@/test/fixtures';
import {
  EMPTY_LEAD_FILTERS,
  LEAD_INTENT_PLACEHOLDER,
  leadIntentLabel,
  leadsToCsv,
  pageCount,
  selectFilteredLeads,
  selectLeadById,
  selectLeadPage,
  selectLeadStats,
  selectSortedLeads,
} from './leads';

const leads = [
  buildMatch({ id: 'a', commentId: 'a', status: 'new', platform: 'instagram', intent: 'Asks the price of the red pair', reason: 'price question' }),
  buildMatch({ id: 'b', commentId: 'b', status: 'interested', platform: 'youtube', intent: 'Wants a demo next week', reason: 'demo request' }),
  buildMatch({ id: 'c', commentId: 'c', status: 'interested', platform: 'instagram', intent: 'Looking for size 42 in Tashkent', reason: 'size + city given' }),
];

describe('selectFilteredLeads', () => {
  test('returns all leads with empty filters', () => {
    expect(selectFilteredLeads(leads, EMPTY_LEAD_FILTERS)).toHaveLength(3);
  });

  test('filters by status', () => {
    const result = selectFilteredLeads(leads, { ...EMPTY_LEAD_FILTERS, status: 'interested' });
    expect(result.map((l) => l.commentId)).toEqual(['b', 'c']);
  });

  test('filters by platform', () => {
    const result = selectFilteredLeads(leads, { ...EMPTY_LEAD_FILTERS, platform: 'youtube' });
    expect(result.map((l) => l.commentId)).toEqual(['b']);
  });

  // v27: there is no username and no comment text to search — `intent` and `reason`
  // (plus the extracted blob) are the whole searchable surface, matching what the
  // bridge's own `q` searches so a client-side filter agrees with a server-side one.
  test('matches query against intent, reason, and extracted values', () => {
    expect(selectFilteredLeads(leads, { ...EMPTY_LEAD_FILTERS, query: 'tashkent' })).toHaveLength(1);
    expect(selectFilteredLeads(leads, { ...EMPTY_LEAD_FILTERS, query: 'demo request' })).toHaveLength(1);
    // Every fixture carries the default extracted blob (phone + intent: pricing).
    expect(selectFilteredLeads(leads, { ...EMPTY_LEAD_FILTERS, query: '4155550142' })).toHaveLength(3);
  });
});

describe('selectLeadStats', () => {
  test('computes totals and win rate', () => {
    const stats = selectLeadStats(leads);
    expect(stats.total).toBe(3);
    expect(stats.newCount).toBe(1);
    expect(stats.won).toBe(2);
    expect(stats.wonRate).toBeCloseTo(2 / 3);
  });

  test('win rate is 0 with no leads', () => {
    expect(selectLeadStats([]).wonRate).toBe(0);
  });
});

describe('selectLeadPage / pageCount', () => {
  test('slices the requested page', () => {
    expect(selectLeadPage(leads, 1, 2).map((l) => l.commentId)).toEqual(['a', 'b']);
    expect(selectLeadPage(leads, 2, 2).map((l) => l.commentId)).toEqual(['c']);
  });

  test('pageCount is at least 1', () => {
    expect(pageCount(0, 12)).toBe(1);
    expect(pageCount(25, 12)).toBe(3);
  });
});

describe('selectSortedLeads', () => {
  const scored = [
    buildMatch({ commentId: 'low', intent: 'cheap shipping question', score: 0.2, capturedAt: { date: 'Jun 1', time: '09:00', ts: 100 } }),
    buildMatch({ commentId: 'high', intent: 'asks to buy today', score: 0.9, capturedAt: { date: 'Jun 3', time: '09:00', ts: 300 } }),
    buildMatch({ commentId: 'mid', intent: 'borrowing a catalogue', score: 0.5, capturedAt: { date: 'Jun 2', time: '09:00', ts: 200 } }),
  ];

  test('sorts by score descending', () => {
    const result = selectSortedLeads(scored, { key: 'score', dir: 'desc' });
    expect(result.map((l) => l.commentId)).toEqual(['high', 'mid', 'low']);
  });

  test('sorts by score ascending', () => {
    const result = selectSortedLeads(scored, { key: 'score', dir: 'asc' });
    expect(result.map((l) => l.commentId)).toEqual(['low', 'mid', 'high']);
  });

  test('sorts by capture timestamp, not the display label', () => {
    const result = selectSortedLeads(scored, { key: 'captured', dir: 'desc' });
    expect(result.map((l) => l.commentId)).toEqual(['high', 'mid', 'low']);
  });

  test('sorts by intent alphabetically', () => {
    const result = selectSortedLeads(scored, { key: 'intent', dir: 'asc' });
    expect(result.map((l) => l.commentId)).toEqual(['high', 'mid', 'low']);
  });

  test('does not mutate the input array', () => {
    const original = scored.map((l) => l.commentId);
    selectSortedLeads(scored, { key: 'score', dir: 'asc' });
    expect(scored.map((l) => l.commentId)).toEqual(original);
  });
});

describe('selectLeadById', () => {
  test('finds by the composite lead id or returns null', () => {
    expect(selectLeadById(leads, 'b')?.intent).toBe('Wants a demo next week');
    expect(selectLeadById(leads, 'zzz')).toBeNull();
  });

  test('two campaigns sharing a commentId resolve to their own records', () => {
    // A bare commentId is NOT unique: matching on it returned whichever copy came
    // first, so the drawer opened (and wrote status to) the wrong campaign's lead.
    const a = buildMatch({ commentId: 'dup', campaignId: 'cmp-a', intent: 'intent A' });
    const b = buildMatch({ commentId: 'dup', campaignId: 'cmp-b', intent: 'intent B' });
    const x = buildMatch({ commentId: 'dup', campaignId: 'cmp-a', platform: 'x', intent: 'intent X' });
    const all = [a, b, x];
    expect(selectLeadById(all, a.id)?.intent).toBe('intent A');
    expect(selectLeadById(all, b.id)?.intent).toBe('intent B');
    expect(selectLeadById(all, x.id)?.intent).toBe('intent X');
    // The raw comment id resolves to nothing — it is not an identity.
    expect(selectLeadById(all, 'dup')).toBeNull();
  });
});

describe('leadsToCsv', () => {
  test('emits a header plus one row per lead and escapes quotes', () => {
    const csv = leadsToCsv([buildMatch({ commentId: 'x', intent: 'says q"uote' })]);
    const lines = csv.split('\n');
    expect(lines[0]).toContain('intent');
    expect(lines).toHaveLength(2);
    expect(lines[1]).toContain('"says q""uote"');
  });
});

describe('leadIntentLabel', () => {
  test('returns the intent when the engine derived one', () => {
    expect(leadIntentLabel(buildMatch({ intent: 'Wants a demo' }))).toBe('Wants a demo');
  });

  test('an empty or whitespace intent gets the neutral placeholder', () => {
    // '' is a REAL value (a pre-v27 lead, or nothing derivable honestly). It renders as
    // a placeholder and NEVER falls back to an identifier — that fallback is the whole
    // leak the redaction closed.
    expect(leadIntentLabel(buildMatch({ intent: '' }))).toBe(LEAD_INTENT_PLACEHOLDER);
    expect(leadIntentLabel(buildMatch({ intent: '   ' }))).toBe(LEAD_INTENT_PLACEHOLDER);
    expect(LEAD_INTENT_PLACEHOLDER).not.toContain('c1');
  });
});

import {
  LEAD_STATUS_ORDER,
  isTerminalStatus,
  selectBoardColumns,
  selectLeadTimeline,
} from './leads';
import { buildLeadNote, buildStatusChange } from '@/test/fixtures';

describe('LEAD_STATUS_ORDER + isTerminalStatus', () => {
  test('has the six statuses in pipeline order', () => {
    expect(LEAD_STATUS_ORDER).toEqual([
      'new', 'in_progress', 'interested', 'closed', 'couldnt_connect', 'archived',
    ]);
  });

  test('terminal statuses are closed / couldnt_connect / archived', () => {
    expect(isTerminalStatus('closed')).toBe(true);
    expect(isTerminalStatus('couldnt_connect')).toBe(true);
    expect(isTerminalStatus('archived')).toBe(true);
    expect(isTerminalStatus('new')).toBe(false);
    expect(isTerminalStatus('in_progress')).toBe(false);
    expect(isTerminalStatus('interested')).toBe(false);
  });
});

describe('selectBoardColumns', () => {
  test('groups leads into six ordered columns, empties preserved', () => {
    const columns = selectBoardColumns([
      buildMatch({ commentId: 'a', status: 'new' }),
      buildMatch({ commentId: 'b', status: 'interested' }),
      buildMatch({ commentId: 'c', status: 'interested' }),
    ]);
    expect(columns.map((c) => c.status)).toEqual(LEAD_STATUS_ORDER);
    const byStatus = Object.fromEntries(columns.map((c) => [c.status, c.leads.length]));
    expect(byStatus.new).toBe(1);
    expect(byStatus.interested).toBe(2);
    expect(byStatus.closed).toBe(0);
  });
});

describe('selectLeadTimeline', () => {
  test('merges status changes and notes oldest-first', () => {
    const lead = buildMatch({
      statusHistory: [buildStatusChange({ atTs: 100 }), buildStatusChange({ atTs: 300 })],
      notes: [buildLeadNote({ id: 'n1', createdAtTs: 200 })],
    });
    const items = selectLeadTimeline(lead);
    expect(items.map((i) => i.ts)).toEqual([100, 200, 300]);
    expect(items.map((i) => i.kind)).toEqual(['status', 'note', 'status']);
  });

  test('returns empty for a pristine lead', () => {
    expect(selectLeadTimeline(buildMatch())).toEqual([]);
  });
});
