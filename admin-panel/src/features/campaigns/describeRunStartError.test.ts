import { describe, expect, test } from 'vitest';
import { appError } from '@/shared/lib/result';
import { describeRunStartError } from './describeRunStartError';

describe('describeRunStartError', () => {
  test('flags the agent-not-ready 409 and keeps the server-provided detail as the message', () => {
    const result = describeRunStartError(
      appError('http', 'Chrome (CDP) unreachable — launch the login browser first.', 409, 'agent_not_ready'),
    );
    expect(result.agentNotReady).toBe(true);
    expect(result.message).toContain('Chrome (CDP) unreachable');
  });

  test('a plain 409 (single-run lock) is NOT flagged as agent-not-ready', () => {
    const result = describeRunStartError(appError('http', 'a run is already active', 409));
    expect(result.agentNotReady).toBe(false);
    expect(result.message).toBe('a run is already active');
  });

  test('a 400 not-runnable error surfaces the server message, unflagged', () => {
    const result = describeRunStartError(appError('http', 'campaign is not runnable', 400));
    expect(result.agentNotReady).toBe(false);
    expect(result.message).toBe('campaign is not runnable');
  });

  test('falls back to a generic message when the error carries no message', () => {
    const result = describeRunStartError(appError('network', ''));
    expect(result.message).toBe('Something went wrong starting the run.');
    expect(result.agentNotReady).toBe(false);
  });
});
