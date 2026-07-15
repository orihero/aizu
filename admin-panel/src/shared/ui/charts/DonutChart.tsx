import { Cell, Pie, PieChart, ResponsiveContainer, Tooltip } from 'recharts';
import { useChartPalette } from '@/shared/hooks/useChartPalette';
import { usePrefersReducedMotion } from '@/shared/hooks/usePrefersReducedMotion';
import { seriesColors } from './chartColors';
import { useTooltipProps } from './chartTooltip';

export interface DonutDatum {
  readonly name: string;
  readonly value: number;
}

interface DonutChartProps {
  readonly data: readonly DonutDatum[];
  readonly centerLabel?: string;
  readonly centerValue?: string;
  readonly height?: number;
}

/** Donut (Pie with inner radius) with a center label — e.g. spend by stage. */
export function DonutChart({ data, centerLabel, centerValue, height = 220 }: DonutChartProps) {
  const palette = useChartPalette();
  const tooltip = useTooltipProps();
  const reduced = usePrefersReducedMotion();
  const colors = seriesColors(palette);
  return (
    <div className="relative" style={{ height }}>
      <ResponsiveContainer>
        <PieChart>
          <Tooltip {...tooltip} />
          <Pie
            data={[...data]}
            dataKey="value"
            nameKey="name"
            innerRadius="62%"
            outerRadius="100%"
            paddingAngle={2}
            isAnimationActive={!reduced}
            animationDuration={800}
          >
            {data.map((d, i) => (
              <Cell key={d.name} fill={colors[i % colors.length]} />
            ))}
          </Pie>
        </PieChart>
      </ResponsiveContainer>
      {centerValue ? (
        <div className="pointer-events-none absolute inset-0 flex flex-col items-center justify-center">
          <span className="font-head text-[22px] font-extrabold leading-none tabular">{centerValue}</span>
          {centerLabel ? <span className="mt-1 text-[11px] text-text-faint">{centerLabel}</span> : null}
        </div>
      ) : null}
    </div>
  );
}
