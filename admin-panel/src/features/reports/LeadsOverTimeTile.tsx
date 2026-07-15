import { Card, CardBody, CardHeader } from '@/shared/ui/Card';
import { MultiLineChart } from '@/shared/ui/charts';
import { selectLineSeries } from '@/shared/selectors/reports';
import type { ReportsPeriod } from '@/shared/types/domain';
import { TileEmpty } from './TileEmpty';

interface LeadsOverTimeTileProps {
  readonly period: ReportsPeriod;
}

/** Hero tile — leads per channel over the selected period. */
export function LeadsOverTimeTile({ period }: LeadsOverTimeTileProps) {
  const series = selectLineSeries(period);
  const hasData = series.length > 0 && period.labels.length > 0;
  return (
    <Card className="col-span-full">
      <CardHeader title="Leads by channel over time" subtitle="one line per channel" />
      <CardBody>
        {hasData ? (
          <MultiLineChart labels={period.labels} series={series} byPlatform height={300} />
        ) : (
          <TileEmpty />
        )}
      </CardBody>
    </Card>
  );
}
