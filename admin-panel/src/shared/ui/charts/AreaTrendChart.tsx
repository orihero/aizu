import {
  Area,
  AreaChart,
  CartesianGrid,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from 'recharts';
import { useChartPalette } from '@/shared/hooks/useChartPalette';
import { useTooltipProps } from './chartTooltip';

interface AreaTrendChartProps {
  readonly labels: readonly string[];
  readonly values: readonly number[];
  readonly name: string;
  readonly color?: string;
  readonly height?: number;
}

/** Single-series gradient area trend (e.g. CPL over time). */
export function AreaTrendChart({ labels, values, name, color, height = 240 }: AreaTrendChartProps) {
  const palette = useChartPalette();
  const tooltip = useTooltipProps();
  const stroke = color ?? palette.brand;
  const id = `trend-${stroke.replace('#', '')}`;
  const rows = labels.map((label, i) => ({ label, value: values[i] ?? 0 }));
  return (
    <div style={{ height }}>
      <ResponsiveContainer>
        <AreaChart data={rows} margin={{ top: 8, right: 8, bottom: 0, left: -16 }}>
          <defs>
            <linearGradient id={id} x1="0" y1="0" x2="0" y2="1">
              <stop offset="0%" stopColor={stroke} stopOpacity={0.3} />
              <stop offset="100%" stopColor={stroke} stopOpacity={0} />
            </linearGradient>
          </defs>
          <CartesianGrid stroke={palette.grid} vertical={false} />
          <XAxis dataKey="label" tick={{ fill: palette.tick, fontSize: 10 }} tickLine={false} axisLine={{ stroke: palette.grid }} />
          <YAxis tick={{ fill: palette.tick, fontSize: 10 }} tickLine={false} axisLine={false} />
          <Tooltip {...tooltip} />
          <Area
            type="monotone"
            dataKey="value"
            name={name}
            stroke={stroke}
            strokeWidth={2}
            fill={`url(#${id})`}
            isAnimationActive={false}
          />
        </AreaChart>
      </ResponsiveContainer>
    </div>
  );
}
