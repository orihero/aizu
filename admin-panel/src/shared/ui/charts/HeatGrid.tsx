import { useChartPalette } from '@/shared/hooks/useChartPalette';
import { hexToRgb } from './chartColors';

interface HeatGridProps {
  /** 24 hourly values (index = hour of day). */
  readonly values: readonly number[];
  readonly color?: string;
}

function hourLabel(hour: number): string {
  const h12 = hour % 12 === 0 ? 12 : hour % 12;
  return `${h12}${hour < 12 ? 'a' : 'p'}`;
}

/**
 * Best-hour-to-post heatmap. The one chart with no Recharts primitive — a 12×2
 * CSS grid whose cell opacity scales with the hourly match count.
 */
export function HeatGrid({ values, color }: HeatGridProps) {
  const palette = useChartPalette();
  const rgb = hexToRgb(color ?? palette.brand);
  const max = Math.max(1, ...values);
  const cells = Array.from({ length: 24 }, (_, h) => values[h] ?? 0);
  return (
    <div className="grid grid-cols-12 gap-1" role="img" aria-label="Matches by hour of day">
      {cells.map((value, hour) => {
        const alpha = 0.07 + (value / max) * 0.93;
        return (
          <div
            key={hour}
            title={`${hourLabel(hour)} — ${value} ${value === 1 ? 'lead' : 'leads'}`}
            className="heat-cell flex aspect-square items-center justify-center rounded-md text-[8px] font-semibold text-text-faint"
            style={{
              background: `rgba(${rgb}, ${value === 0 ? 0.05 : alpha})`,
              animationDelay: `${hour * 20}ms`,
            }}
          >
            {hour % 6 === 0 ? hourLabel(hour) : ''}
          </div>
        );
      })}
    </div>
  );
}
