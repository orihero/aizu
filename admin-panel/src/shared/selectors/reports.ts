import type { DashboardPeriodKey, Reports, ReportsPeriod } from '@/shared/types/domain';
import type { DonutDatum, LineSeries } from '@/shared/ui/charts';

export function selectReportPeriod(reports: Reports, period: DashboardPeriodKey): ReportsPeriod {
  return reports[period];
}

/** matchesByPlatform → multi-line series (one line per platform). */
export function selectLineSeries(period: ReportsPeriod): LineSeries[] {
  return period.matchesByPlatform.map((p) => ({ name: p.platform, values: p.values }));
}

/** spendByStage is already {name,value}; pass through for the donut. */
export function selectDonutData(period: ReportsPeriod): DonutDatum[] {
  return period.spendByStage.map((s) => ({ name: s.name, value: s.value }));
}

export function selectTotalSpend(period: ReportsPeriod): number {
  return period.spendByStage.reduce((sum, s) => sum + s.value, 0);
}

/** Platform ranking with a 0–1 share for relative progress bars. */
export interface RankedPlatform {
  readonly platform: string;
  readonly leads: number;
  readonly share: number;
}

export function selectPlatformRanking(period: ReportsPeriod): RankedPlatform[] {
  const max = Math.max(1, ...period.platformRanking.map((p) => p.leads));
  return period.platformRanking.map((p) => ({
    platform: p.platform,
    leads: p.leads,
    share: p.leads / max,
  }));
}
