const MONEY_DECIMALS = 2;

export function formatMoney(usd: number): string {
  return `$${usd.toFixed(MONEY_DECIMALS)}`;
}

export function formatNumber(value: number): string {
  return new Intl.NumberFormat('en-US').format(value);
}

export function formatPercent(ratio: number): string {
  return `${Math.round(ratio * 100)}%`;
}

export function formatScore(score: number): string {
  return score.toFixed(2);
}

/** "+0.21 above threshold" / "−0.04 below threshold" — sign-correct. */
export function formatThresholdDelta(score: number, threshold: number): string {
  const diff = score - threshold;
  const sign = diff >= 0 ? '+' : '−';
  const relation = diff >= 0 ? 'above' : 'below';
  return `${sign}${Math.abs(diff).toFixed(2)} ${relation} threshold`;
}
