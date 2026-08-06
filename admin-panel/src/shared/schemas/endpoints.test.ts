import { describe, expect, test } from 'vitest';
import {
  agentNotReadyResponseSchema,
  agentReadinessSchema,
  launchAgentLoginResponseSchema,
} from './endpoints';

const READY = {
  ready: true,
  cdp: 'ok',
  instagram: 'logged_in',
  checkedAt: 1_718_800_000,
  detail: null,
  cdpUrl: 'http://127.0.0.1:9222',
};

describe('agentReadinessSchema', () => {
  test('parses the healthy shape', () => {
    const parsed = agentReadinessSchema.parse(READY);
    expect(parsed.ready).toBe(true);
    expect(parsed.detail).toBeNull();
  });

  test('parses a not-ready shape with a detail string', () => {
    const parsed = agentReadinessSchema.parse({
      ...READY, ready: false, cdp: 'unreachable', instagram: 'unknown',
      detail: 'connect ECONNREFUSED 127.0.0.1:9222',
    });
    expect(parsed.ready).toBe(false);
    expect(parsed.cdp).toBe('unreachable');
    expect(parsed.detail).toContain('ECONNREFUSED');
  });

  test('rejects an unknown cdp/instagram enum value (fixed contract, no silent coercion)', () => {
    expect(agentReadinessSchema.safeParse({ ...READY, cdp: 'sideways' }).success).toBe(false);
    expect(agentReadinessSchema.safeParse({ ...READY, instagram: 'sideways' }).success).toBe(false);
  });
});

describe('launchAgentLoginResponseSchema', () => {
  test('parses launched + nested readiness', () => {
    const parsed = launchAgentLoginResponseSchema.parse({ launched: true, readiness: READY });
    expect(parsed.launched).toBe(true);
    expect(parsed.readiness.ready).toBe(true);
  });
});

describe('agentNotReadyResponseSchema', () => {
  test('parses the 409 gate shape (its own error/detail/readiness, not the write envelope)', () => {
    const parsed = agentNotReadyResponseSchema.parse({
      error: 'agent_not_ready',
      detail: 'Chrome (CDP) unreachable — launch the login browser first.',
      readiness: { ...READY, ready: false, cdp: 'unreachable' },
    });
    expect(parsed.error).toBe('agent_not_ready');
    expect(parsed.readiness.cdp).toBe('unreachable');
  });

  test('rejects a body whose error literal does not match (so it falls through to generic 409 handling)', () => {
    expect(
      agentNotReadyResponseSchema.safeParse({ error: 'some_other_error', detail: 'x', readiness: READY }).success,
    ).toBe(false);
  });
});
