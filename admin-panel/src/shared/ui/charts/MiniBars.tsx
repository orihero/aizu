import { Bar, BarChart, Cell, ResponsiveContainer } from 'recharts';
import { useChartPalette } from '@/shared/hooks/useChartPalette';
import { usePrefersReducedMotion } from '@/shared/hooks/usePrefersReducedMotion';

interface MiniBarsProps {
  readonly data: readonly number[];
  readonly color?: string;
  /** Highlight the most recent bar with the accent color. */
  readonly accentLast?: boolean;
  readonly height?: number;
}

/** Tiny bar history (e.g. CPL per day) — no axes. */
export function MiniBars({ data, color, accentLast = true, height = 48 }: MiniBarsProps) {
  const palette = useChartPalette();
  const reduced = usePrefersReducedMotion();
  const base = color ?? palette.brand;
  const rows = data.map((v, i) => ({ i, v }));
  return (
    <div style={{ height }}>
      <ResponsiveContainer>
        <BarChart data={rows} margin={{ top: 4, right: 0, bottom: 0, left: 0 }} barCategoryGap={2}>
          <Bar dataKey="v" radius={[2, 2, 0, 0]} isAnimationActive={!reduced} animationDuration={800}>
            {rows.map((row, i) => (
              <Cell
                key={row.i}
                fill={accentLast && i === rows.length - 1 ? palette.accent : base}
                fillOpacity={accentLast && i === rows.length - 1 ? 1 : 0.55}
              />
            ))}
          </Bar>
        </BarChart>
      </ResponsiveContainer>
    </div>
  );
}
