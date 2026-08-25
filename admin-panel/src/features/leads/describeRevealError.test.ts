import { describe, expect, test } from 'vitest';
import { appError } from '@/shared/lib/result';
import { describeRevealError } from './describeRevealError';

describe('describeRevealError', () => {
  test('the reveal-allowance 402 gets plain copy, an upgrade CTA, and keeps the specifics', () => {
    const result = describeRevealError(appError(
      'http',
      'Plan limit reached (10 lead reveals on Free). Resets Jul 1. Upgrade to reveal more leads.',
      402,
    ));
    expect(result.message).toMatch(/revealed every lead your plan includes/i);
    // The server's numbers and reset date are the useful part — kept, just demoted out
    // of the headline, where "Plan limit reached" reads as a system fault.
    expect(result.detail).toMatch(/Resets Jul 1/);
    expect(result.upgrade).toBe(true);
  });

  test('the copy names LEADS, not reveals — reopening a revealed lead costs nothing', () => {
    // The cap counts DISTINCT leads revealed this period. Copy that said "reveals" would
    // tell a customer that re-reading a lead they already opened spends allowance, which
    // is false and would make them ration something that is free.
    const { message } = describeRevealError(appError('http', 'Plan limit reached', 402));
    expect(message).toMatch(/\bleads?\b/i);
    expect(message).not.toMatch(/\breveals\b/i);
  });

  test('the viewer 403 keeps the server wording and offers NO upgrade', () => {
    // A role refusal is not a plan the customer can buy their way out of; an upgrade
    // link here would sell them a checkout that fixes nothing.
    const result = describeRevealError(appError('http', 'your role does not permit this action', 403));
    expect(result.message).toBe('your role does not permit this action');
    expect(result.detail).toBeNull();
    expect(result.upgrade).toBe(false);
  });

  test('the ownership 404 and a transport failure are plain errors too', () => {
    expect(describeRevealError(appError('http', 'unknown lead', 404)).upgrade).toBe(false);
    const network = describeRevealError(appError('network', 'bridge server unreachable'));
    expect(network.message).toBe('bridge server unreachable');
    expect(network.upgrade).toBe(false);
  });

  test('a blank message still produces a headline, never an empty alert', () => {
    expect(describeRevealError(appError('unknown', '   ')).message).toBe('The reveal didn’t work.');
  });
});
