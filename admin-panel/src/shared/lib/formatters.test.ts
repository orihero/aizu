import { describe, expect, test } from 'vitest';
import {
  formatMoney,
  formatNumber,
  formatPercent,
  formatScore,
  formatThresholdDelta,
} from './formatters';

describe('formatters', () => {
  test('formatMoney renders two decimals', () => {
    expect(formatMoney(5.625)).toBe('$5.63');
    expect(formatMoney(0)).toBe('$0.00');
  });

  test('formatNumber adds thousands separators', () => {
    expect(formatNumber(12345)).toBe('12,345');
  });

  test('formatPercent rounds a ratio', () => {
    expect(formatPercent(0.349)).toBe('35%');
  });

  test('formatScore renders two decimals', () => {
    expect(formatScore(0.7)).toBe('0.70');
  });

  test('formatThresholdDelta is sign-correct above and below', () => {
    expect(formatThresholdDelta(0.91, 0.7)).toBe('+0.21 above threshold');
    expect(formatThresholdDelta(0.66, 0.7)).toBe('−0.04 below threshold');
  });
});
