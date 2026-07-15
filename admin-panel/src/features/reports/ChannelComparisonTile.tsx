import { Card, CardBody, CardHeader } from '@/shared/ui/Card';
import { platformColor, seriesColors } from '@/shared/ui/charts';
import { useChartPalette } from '@/shared/hooks/useChartPalette';
import { selectPlatformRanking } from '@/shared/selectors/reports';
import { formatNumber } from '@/shared/lib/formatters';
import type { ReportsPeriod } from '@/shared/types/domain';
import { TileEmpty } from './TileEmpty';

interface ChannelComparisonTileProps {
  readonly period: ReportsPeriod;
}

/** Ranked channel rows with relative-width progress bars (ranked by leads). */
export function ChannelComparisonTile({ period }: ChannelComparisonTileProps) {
  const palette = useChartPalette();
  const fallbacks = seriesColors(palette);
  const ranking = selectPlatformRanking(period);

  return (
    <Card className="lg:col-span-2">
      <CardHeader title="Channel comparison" subtitle="ranked by leads" />
      <CardBody>
        {ranking.length > 0 ? (
          <ul className="space-y-3">
            {ranking.map((row, i) => {
              const fallback = fallbacks[i % fallbacks.length] ?? palette.brand;
              const color = platformColor(row.platform, fallback);
              return (
                <li
                  key={row.platform}
                  className="grid grid-cols-[16px_5.5rem_1fr_auto] items-center gap-3"
                >
                  <span className="font-head text-xs font-extrabold text-text-faint tabular-nums">
                    {i + 1}
                  </span>
                  <span className="truncate text-xs font-semibold">{row.platform}</span>
                  <span className="h-2.5 overflow-hidden rounded-full bg-surface-2">
                    <span
                      className="block h-full rounded-full"
                      style={{ width: `${row.share * 100}%`, background: color }}
                    />
                  </span>
                  <span className="text-right text-xs font-bold tabular-nums">
                    {formatNumber(row.leads)}
                  </span>
                </li>
              );
            })}
          </ul>
        ) : (
          <TileEmpty />
        )}
      </CardBody>
    </Card>
  );
}
