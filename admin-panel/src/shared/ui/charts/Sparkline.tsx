import { Area, AreaChart, ResponsiveContainer } from 'recharts';
import { useChartPalette } from '@/shared/hooks/useChartPalette';
import { usePrefersReducedMotion } from '@/shared/hooks/usePrefersReducedMotion';

interface SparklineProps {
  readonly data: readonly number[];
  readonly color?: string;
  readonly height?: number;
}

/** Compact trend line with a soft gradient fill — no axes (hero + campaign cards). */
export function Sparkline({ data, color, height = 48 }: SparklineProps) {
  const palette = useChartPalette();
  const reduced = usePrefersReducedMotion();
  const stroke = color ?? palette.brand;
  const id = `spark-${stroke.replace('#', '')}`;
  const rows = data.map((v, i) => ({ i, v }));
  return (
    <div style={{ height }}>
      <ResponsiveContainer>
        <AreaChart data={rows} margin={{ top: 4, right: 0, bottom: 0, left: 0 }}>
          <defs>
            <linearGradient id={id} x1="0" y1="0" x2="0" y2="1">
              <stop offset="0%" stopColor={stroke} stopOpacity={0.35} />
              <stop offset="100%" stopColor={stroke} stopOpacity={0} />
            </linearGradient>
          </defs>
          <Area
            type="monotone"
            dataKey="v"
            stroke={stroke}
            strokeWidth={2}
            fill={`url(#${id})`}
            isAnimationActive={!reduced}
            animationDuration={900}
            dot={false}
          />
        </AreaChart>
      </ResponsiveContainer>
    </div>
  );
}
