import { describe, expect, test } from 'vitest';
import { appError } from '@/shared/lib/result';
import { describeRunStartError } from './describeRunStartError';

describe('describeRunStartError', () => {
  test('flags the agent-not-ready 409 and keeps the server-provided detail as the message', () => {
    const result = describeRunStartError(
      appError('http', 'Chrome (CDP) unreachable — launch the login browser first.', 409, 'agent_not_ready'),
    );
    expect(result.agentNotReady).toBe(true);
    expect(result.upgrade).toBe(false);
    expect(result.message).toContain('Chrome (CDP) unreachable');
  });

  test('a plain 409 (single-run lock) is NOT flagged as agent-not-ready', () => {
    const result = describeRunStartError(appError('http', 'a run is already active', 409));
    expect(result.agentNotReady).toBe(false);
    expect(result.message).toBe('a run is already active');
    expect(result.detail).toBeNull();
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
    expect(result.upgrade).toBe(false);
  });

  test('the lead-cap 402 gets plain copy, an upgrade CTA, and keeps the specifics', () => {
    const result = describeRunStartError(
      appError('http', 'Plan limit reached (10 leads). Resets Jul 1. Upgrade to keep running.', 402),
    );

    // "Plan limit reached" reads as a system fault; the headline says what actually
    // happened, and the server's sentence (cap size + reset date) survives as detail.
    expect(result.message).toMatch(/used every lead your plan includes/i);
    expect(result.detail).toContain('Resets Jul 1');
    expect(result.upgrade).toBe(true);
    expect(result.agentNotReady).toBe(false);
  });

  test('the inactive-subscription 402 gets its own copy, not the lead-cap copy', () => {
    const result = describeRunStartError(
      appError('http', 'Your subscription is past_due. Update billing in Settings to start new runs.', 402),
    );

    expect(result.message).toMatch(/subscription isn’t active/i);
    expect(result.upgrade).toBe(true);
  });

  test('an unrecognised 402 falls back to neutral plan copy rather than guessing', () => {
    const result = describeRunStartError(appError('http', 'payment required', 402));

    expect(result.message).toMatch(/plan doesn’t allow this run/i);
    expect(result.upgrade).toBe(true);
  });

  test('a missing-AI-key 503 is NEVER sold as an upgrade', () => {
    // Prod quirk: AI drafting fails independently with "AI drafting is unavailable (no
    // AI key)". That is an unconfigured provider, not a plan the customer can buy — the
    // 402 branch is keyed off the status precisely so the two can't be confused.
    const result = describeRunStartError(
      appError('http', 'OpenRouter key is not configured', 503),
    );

    expect(result.upgrade).toBe(false);
    expect(result.message).toBe('OpenRouter key is not configured');
  });
});
