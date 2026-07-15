import { useTheme } from './useTheme';

export interface ChartPalette {
  readonly grid: string;
  readonly tick: string;
  readonly brand: string;
  readonly brand2: string;
  readonly accent: string;
  readonly success: string;
  readonly warn: string;
  readonly danger: string;
  readonly info: string;
  readonly cloud: string;
  readonly surface: string;
  readonly tooltipBg: string;
  readonly tooltipBorder: string;
}

/* AIZU "Ink × Lime" palette — concrete values mirror src/index.css per theme.
   On dark the primary line is lime (ink would vanish on the near-black ground);
   on light it's ink. accent is the brighter highlight (endpoints / last bar). */
const DARK: ChartPalette = {
  grid: '#23262f',
  tick: '#6b7080',
  brand: '#d9f24f',
  brand2: '#8a90a0',
  accent: '#eaff6f',
  success: '#34d399',
  warn: '#fbbf24',
  danger: '#f87171',
  info: '#60a5fa',
  cloud: '#c084fc',
  surface: '#181a22',
  tooltipBg: '#23262f',
  tooltipBorder: '#282c38',
};

const LIGHT: ChartPalette = {
  grid: '#eef0f4',
  tick: '#9b9eab',
  brand: '#16161a',
  brand2: '#6b6e7b',
  accent: '#d9f24f',
  success: '#22c55e',
  warn: '#f59e0b',
  danger: '#ef4444',
  info: '#0ea5e9',
  cloud: '#7c3aed',
  surface: '#ffffff',
  tooltipBg: '#16161a',
  tooltipBorder: '#16161a',
};

/** Recharts needs concrete color values — resolved per active theme. */
export function useChartPalette(): ChartPalette {
  const { theme } = useTheme();
  return theme === 'dark' ? DARK : LIGHT;
}
