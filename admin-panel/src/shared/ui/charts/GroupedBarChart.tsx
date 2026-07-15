import {
  Bar,
  BarChart,
  CartesianGrid,
  Legend,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from 'recharts';
import { useChartPalette } from '@/shared/hooks/useChartPalette';
import { usePrefersReducedMotion } from '@/shared/hooks/usePrefersReducedMotion';
import { useTooltipProps } from './chartTooltip';

export interface GroupedBarDatum {
  readonly category: string;
  readonly current: number;
  readonly previous: number;
}

interface GroupedBarChartProps {
  readonly data: readonly GroupedBarDatum[];
  readonly currentLabel?: string;
  readonly previousLabel?: string;
  readonly height?: number;
}

/** Two-series grouped bars: current vs previous period (leads by channel). */
export function GroupedBarChart({
  data,
  currentLabel = 'Current',
  previousLabel = 'Previous',
  height = 220,
}: GroupedBarChartProps) {
  const palette = useChartPalette();
  const tooltip = useTooltipProps();
  const reduced = usePrefersReducedMotion();
  return (
    <div style={{ height }}>
      <ResponsiveContainer>
        <BarChart data={[...data]} margin={{ top: 8, right: 8, bottom: 0, left: -16 }} barGap={4}>
          <CartesianGrid stroke={palette.grid} vertical={false} />
          <XAxis
            dataKey="category"
            tick={{ fill: palette.tick, fontSize: 10 }}
            tickLine={false}
            axisLine={{ stroke: palette.grid }}
          />
          <YAxis tick={{ fill: palette.tick, fontSize: 10 }} tickLine={false} axisLine={false} allowDecimals={false} />
          <Tooltip {...tooltip} cursor={{ fill: palette.grid, fillOpacity: 0.4 }} />
          <Legend wrapperStyle={{ fontSize: 11 }} />
          <Bar dataKey="previous" name={previousLabel} fill={palette.grid} radius={[4, 4, 0, 0]} isAnimationActive={!reduced} animationDuration={800} />
          <Bar dataKey="current" name={currentLabel} fill={palette.brand} radius={[4, 4, 0, 0]} isAnimationActive={!reduced} animationDuration={800} animationBegin={150} />
        </BarChart>
      </ResponsiveContainer>
    </div>
  );
}
