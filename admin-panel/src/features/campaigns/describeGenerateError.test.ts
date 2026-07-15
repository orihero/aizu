import { describe, expect, test } from 'vitest';
import { appError } from '@/shared/lib/result';
import { describeGenerateError } from './describeGenerateError';

describe('describeGenerateError', () => {
  test('maps a network error to a reachability message (retry, no manual fill)', () => {
    const result = describeGenerateError(appError('network', 'bridge server unreachable'));
    expect(result.title).toMatch(/couldn.t reach the server/i);
    expect(result.canFillManually).toBe(false);
  });

  test('maps a 503 to the no-AI-key message and offers manual fill', () => {
    const result = describeGenerateError(appError('http', 'service unavailable', 503));
    expect(result.title).toMatch(/AI drafting is unavailable/i);
    expect(result.canFillManually).toBe(true);
  });

  test('maps an OPENROUTER-mentioning error to the no-AI-key message even without a 503', () => {
    const result = describeGenerateError(appError('http', 'OPENROUTER_API_KEY is not configured', 500));
    expect(result.title).toMatch(/AI drafting is unavailable/i);
    expect(result.canFillManually).toBe(true);
  });

  test('maps a 422 to the unbuildable-input message', () => {
    const result = describeGenerateError(appError('http', 'could not draft a campaign', 422));
    expect(result.title).toMatch(/couldn.t turn that into a draft/i);
    expect(result.canFillManually).toBe(false);
  });

  test('maps a validation error to the unbuildable-input message', () => {
    const result = describeGenerateError(appError('validation', 'response shape mismatch'));
    expect(result.title).toMatch(/couldn.t turn that into a draft/i);
  });

  test('surfaces the server message for an otherwise-unmapped http error', () => {
    const result = describeGenerateError(appError('http', 'campaign quota exceeded', 402));
    expect(result.title).toBe('campaign quota exceeded');
    expect(result.canFillManually).toBe(false);
  });

  test('falls back to a generic message when the error carries no message', () => {
    const result = describeGenerateError(appError('unknown', ''));
    expect(result.title).toBe('Generation failed.');
  });
});
