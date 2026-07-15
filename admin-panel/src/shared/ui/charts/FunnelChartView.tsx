import {
  Cell,
  Funnel,
  FunnelChart,
  LabelList,
  ResponsiveContainer,
  Tooltip,
} from 'recharts';
import { useChartPalette } from '@/shared/hooks/useChartPalette';
import { usePrefersReducedMotion } from '@/shared/hooks/usePrefersReducedMotion';
import { seriesColors } from './chartColors';
import { useTooltipProps } from './chartTooltip';

export interface FunnelStage {
  readonly name: string;
  readonly value: number;
}

interface FunnelChartViewProps {
  readonly stages: readonly FunnelStage[];
  readonly height?: number;
}

/** Native Recharts funnel: reels → relevant → scored → matches. */
export function FunnelChartView({ stages, height = 220 }: FunnelChartViewProps) {
  const palette = useChartPalette();
  const tooltip = useTooltipProps();
  const reduced = usePrefersReducedMotion();
  const colors = seriesColors(palette);
  const data = stages.map((s, i) => ({ ...s, fill: colors[i % colors.length] }));
  return (
    <div style={{ height }}>
      <ResponsiveContainer>
        <FunnelChart>
          <Tooltip {...tooltip} />
          <Funnel dataKey="value" data={data} isAnimationActive={!reduced} animationDuration={800}>
            <LabelList position="right" fill={palette.tick} stroke="none" dataKey="name" fontSize={11} />
            <LabelList position="left" fill={palette.tick} stroke="none" dataKey="value" fontSize={11} />
            {data.map((d) => (
              <Cell key={d.name} fill={d.fill} />
            ))}
          </Funnel>
        </FunnelChart>
      </ResponsiveContainer>
    </div>
  );
}
