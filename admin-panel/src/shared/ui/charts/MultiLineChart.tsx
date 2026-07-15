import {
  CartesianGrid,
  Legend,
  Line,
  LineChart,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from 'recharts';
import { useChartPalette } from '@/shared/hooks/useChartPalette';
import { platformColor, seriesColors } from './chartColors';
import { useTooltipProps } from './chartTooltip';

export interface LineSeries {
  readonly name: string;
  readonly values: readonly number[];
}

interface MultiLineChartProps {
  readonly labels: readonly string[];
  readonly series: readonly LineSeries[];
  /** Color series by platform name (else cycle the theme palette). */
  readonly byPlatform?: boolean;
  readonly height?: number;
}

/** Multi-series line chart sharing one x-axis (e.g. leads by platform over time). */
export function MultiLineChart({ labels, series, byPlatform = false, height = 240 }: MultiLineChartProps) {
  const palette = useChartPalette();
  const tooltip = useTooltipProps();
  const colors = seriesColors(palette);
  const rows = labels.map((label, i) => {
    const row: Record<string, string | number> = { label };
    for (const s of series) row[s.name] = s.values[i] ?? 0;
    return row;
  });
  return (
    <div style={{ height }}>
      <ResponsiveContainer>
        <LineChart data={rows} margin={{ top: 8, right: 8, bottom: 0, left: -16 }}>
          <CartesianGrid stroke={palette.grid} vertical={false} />
          <XAxis dataKey="label" tick={{ fill: palette.tick, fontSize: 10 }} tickLine={false} axisLine={{ stroke: palette.grid }} />
          <YAxis tick={{ fill: palette.tick, fontSize: 10 }} tickLine={false} axisLine={false} allowDecimals={false} />
          <Tooltip {...tooltip} />
          <Legend wrapperStyle={{ fontSize: 11 }} />
          {series.map((s, i) => {
            const base = colors[i % colors.length] ?? palette.brand;
            return (
              <Line
                key={s.name}
                type="monotone"
                dataKey={s.name}
                stroke={byPlatform ? platformColor(s.name, base) : base}
                strokeWidth={2}
                dot={false}
                isAnimationActive={false}
              />
            );
          })}
        </LineChart>
      </ResponsiveContainer>
    </div>
  );
}
