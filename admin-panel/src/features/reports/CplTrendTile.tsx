import { TrendingDown, TrendingUp } from 'lucide-react';
import { Badge } from '@/shared/ui/Badge';
import { Card, CardBody, CardHeader } from '@/shared/ui/Card';
import { AreaTrendChart } from '@/shared/ui/charts';
import { formatMoney } from '@/shared/lib/formatters';
import type { ReportsPeriod } from '@/shared/types/domain';
import { TileEmpty } from './TileEmpty';

interface CplTrendTileProps {
  readonly period: ReportsPeriod;
}

/** Percent change from first to last point; null when the math is undefined. */
function trendPct(values: readonly number[]): number | null {
  if (values.length < 2) return null;
  const first = values[0];
  const last = values[values.length - 1];
  if (first === undefined || last === undefined || first === 0) return null;
  return ((last - first) / first) * 100;
}

function ChangeBadge({ pct }: { readonly pct: number }) {
  const isFalling = pct <= 0;
  const Icon = isFalling ? TrendingDown : TrendingUp;
  return (
    <Badge tone={isFalling ? 'success' : 'danger'}>
      <Icon className="size-3" aria-hidden />
      {`${isFalling ? '−' : '+'}${Math.abs(pct).toFixed(1)}%`}
    </Badge>
  );
}

/** Single-series CPL trend with a current-value readout and period delta. */
export function CplTrendTile({ period }: CplTrendTileProps) {
  const { cplTrend, labels } = period;
  const hasData = cplTrend.length > 0 && labels.length > 0;
  const current = hasData ? cplTrend[cplTrend.length - 1] ?? 0 : 0;
  const pct = trendPct(cplTrend);

  return (
    <Card className="lg:col-span-2">
      <CardHeader
        title="CPL trend"
        subtitle="cost per lead · end of period"
        {...(pct !== null ? { actions: <ChangeBadge pct={pct} /> } : {})}
      />
      <CardBody>
        {hasData ? (
          <>
            <div className="mb-2 flex items-baseline gap-2">
              <span className="font-head text-[34px] font-extrabold leading-none tabular">
                {formatMoney(current)}
              </span>
              <span className="text-xs font-medium text-text-faint">current CPL</span>
            </div>
            <AreaTrendChart labels={labels} values={cplTrend} name="CPL" height={210} />
          </>
        ) : (
          <TileEmpty />
        )}
      </CardBody>
    </Card>
  );
}
