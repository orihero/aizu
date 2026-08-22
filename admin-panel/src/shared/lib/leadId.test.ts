import { describe, expect, test } from 'vitest';
import { leadRoute, leadUid, leadUidOf } from './leadId';

describe('leadUid', () => {
  test('is injective across the whole composite key', () => {
    // Every part must matter, or two real leads collapse into one panel row.
    expect(leadUid('cmp-a', 'instagram', 'c1')).not.toBe(leadUid('cmp-b', 'instagram', 'c1'));
    expect(leadUid('cmp-a', 'instagram', 'c1')).not.toBe(leadUid('cmp-a', 'x', 'c1'));
    expect(leadUid('cmp-a', 'instagram', 'c1')).not.toBe(leadUid('cmp-a', 'instagram', 'c2'));
  });

  test('a delimiter inside a part cannot forge another key', () => {
    expect(leadUid('a|b', 'instagram', 'c')).not.toBe(leadUid('a', 'b|instagram', 'c'));
    // A literal "%7C" must not read back as an escaped delimiter.
    expect(leadUid('a%7Cb', 'instagram', 'c')).not.toBe(leadUid('a|b', 'instagram', 'c'));
  });

  test('matches the engine encoding (aizu/panel.py::lead_uid)', () => {
    expect(leadUid('cmp-a', 'instagram', 'c1')).toBe('cmp-a|instagram|c1');
    expect(leadUid('a|b', 'x', '50%')).toBe('a%7Cb|x|50%25');
  });

  test('leadUidOf reads the three fields off a lead-shaped record', () => {
    expect(leadUidOf({ campaignId: 'cmp-a', platform: 'x', commentId: 'c1' }))
      .toBe(leadUid('cmp-a', 'x', 'c1'));
  });
});

describe('leadRoute', () => {
  test('survives a round trip through the router param decode', () => {
    const id = leadUid('a|b', 'instagram', '50%');
    const route = leadRoute(id);
    expect(route.startsWith('/leads/')).toBe(true);
    // react-router decodes the path param before handing it to useParams.
    expect(decodeURIComponent(route.slice('/leads/'.length))).toBe(id);
  });
});
