import { Card, CardBody, CardHeader } from '@/shared/ui/Card';
import { DonutChart, seriesColors } from '@/shared/ui/charts';
import { useChartPalette } from '@/shared/hooks/useChartPalette';
import { selectDonutData, selectTotalSpend } from '@/shared/selectors/reports';
import { formatMoney, formatPercent } from '@/shared/lib/formatters';
import type { ReportsPeriod } from '@/shared/types/domain';
import { TileEmpty } from './TileEmpty';

interface SpendByStageTileProps {
  readonly period: ReportsPeriod;
}

/** Donut of spend by pipeline stage with a matching color-coded legend. */
export function SpendByStageTile({ period }: SpendByStageTileProps) {
  const palette = useChartPalette();
  const colors = seriesColors(palette);
  const data = selectDonutData(period);
  const total = selectTotalSpend(period);
  const hasData = data.length > 0 && total > 0;

  return (
    <Card className="lg:col-span-2">
      <CardHeader title="Spend by stage" subtitle="this period" />
      <CardBody>
        {hasData ? (
          <div className="flex flex-col items-center gap-4 sm:flex-row">
            <div className="w-[180px] shrink-0">
              <DonutChart
                data={data}
                centerLabel="spent"
                centerValue={formatMoney(total)}
                height={180}
              />
            </div>
            <ul className="min-w-0 grow space-y-1.5">
              {data.map((datum, i) => (
                <li
                  key={datum.name}
                  className="flex items-center gap-2 border-b border-border/60 py-1.5 text-xs last:border-0"
                >
                  <span
                    className="size-2.5 shrink-0 rounded-full"
                    style={{ background: colors[i % colors.length] }}
                    aria-hidden
                  />
                  <span className="grow truncate text-text-muted">{datum.name}</span>
                  <span className="shrink-0 font-semibold tabular-nums">
                    {formatMoney(datum.value)}
                  </span>
                  <span className="w-9 shrink-0 text-right text-[11px] text-text-faint tabular-nums">
                    {formatPercent(datum.value / total)}
                  </span>
                </li>
              ))}
            </ul>
          </div>
        ) : (
          <TileEmpty />
        )}
      </CardBody>
    </Card>
  );
}
