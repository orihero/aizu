import { PolarAngleAxis, RadialBar, RadialBarChart, ResponsiveContainer } from 'recharts';
import { useChartPalette } from '@/shared/hooks/useChartPalette';
import { usePrefersReducedMotion } from '@/shared/hooks/usePrefersReducedMotion';

interface GoalGaugeProps {
  /** Progress percentage 0–100. */
  readonly pct: number;
  readonly color?: string;
  readonly height?: number;
  readonly label?: string;
}

/** Radial progress gauge (~260° arc) for goal completion. */
export function GoalGauge({ pct, color, height = 160, label }: GoalGaugeProps) {
  const palette = useChartPalette();
  const reduced = usePrefersReducedMotion();
  const fill = color ?? palette.brand;
  const value = Math.max(0, Math.min(100, pct));
  return (
    <div className="relative" style={{ height }}>
      <ResponsiveContainer>
        <RadialBarChart
          innerRadius="70%"
          outerRadius="100%"
          data={[{ value }]}
          startAngle={220}
          endAngle={-40}
        >
          <PolarAngleAxis type="number" domain={[0, 100]} tick={false} />
          <RadialBar
            dataKey="value"
            background={{ fill: palette.grid }}
            cornerRadius={999}
            fill={fill}
            isAnimationActive={!reduced}
            animationDuration={900}
          />
        </RadialBarChart>
      </ResponsiveContainer>
      <div className="pointer-events-none absolute inset-0 flex flex-col items-center justify-center">
        <span className="font-head text-[26px] font-extrabold leading-none tabular">{value}%</span>
        {label ? <span className="mt-1 text-[11px] text-text-faint">{label}</span> : null}
      </div>
    </div>
  );
}
