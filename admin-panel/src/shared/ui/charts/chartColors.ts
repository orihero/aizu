import type { ChartPalette } from '@/shared/hooks/useChartPalette';

/** Stable per-platform colors (mirrors the mockup channel palette). */
export const PLATFORM_COLORS: Readonly<Record<string, string>> = {
  instagram: '#e1306c',
  youtube: '#ff0000',
  telegram: '#229ed9',
  facebook: '#1877f2',
  linkedin: '#0a66c2',
  tiktok: '#16161a',
  x: '#6b7280',
};

export function platformColor(platform: string, fallback: string): string {
  return PLATFORM_COLORS[platform.toLowerCase()] ?? fallback;
}

/** Ordered series colors for multi-series charts, resolved per theme. */
export function seriesColors(palette: ChartPalette): readonly string[] {
  return [palette.brand, palette.brand2, palette.success, palette.warn, palette.info, palette.cloud, palette.danger];
}

/** "#rrggbb" → "r, g, b" for rgba() composition (heatmap cell opacity). */
export function hexToRgb(hex: string): string {
  const clean = hex.replace('#', '');
  const value = clean.length === 3
    ? clean.split('').map((c) => c + c).join('')
    : clean;
  const r = parseInt(value.slice(0, 2), 16);
  const g = parseInt(value.slice(2, 4), 16);
  const b = parseInt(value.slice(4, 6), 16);
  return `${r}, ${g}, ${b}`;
}
