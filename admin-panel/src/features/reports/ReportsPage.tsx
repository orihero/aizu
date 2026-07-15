import { useEffect, useState } from 'react';
import { useLocation } from 'react-router-dom';
import { PageHeader } from '@/app/layout/PageHeader';
import { AsyncBoundary } from '@/shared/ui/AsyncBoundary';
import { useReports } from '@/shared/hooks/useReports';
import { usePersistedQueryState } from '@/shared/hooks/usePersistedQueryState';
import { asDashboardPeriod } from '@/shared/selectors/dashboard';
import { selectReportPeriod } from '@/shared/selectors/reports';
import type { DashboardPeriodKey } from '@/shared/types/domain';
import { ReportsPeriodSelector } from './ReportsPeriodSelector';
import { LeadsOverTimeTile } from './LeadsOverTimeTile';
import { CplTrendTile } from './CplTrendTile';
import { SpendByStageTile } from './SpendByStageTile';
import { ChannelComparisonTile } from './ChannelComparisonTile';
import { CampaignPerformanceTile } from './CampaignPerformanceTile';
import { SystemHealthTile } from './SystemHealthTile';
import { HEALTH_ANCHOR_HASH, HEALTH_ANCHOR_ID, HEALTH_HIGHLIGHT_MS } from './healthAnchor';

export function ReportsPage() {
  const { data, isLoading, error, refetch } = useReports();
  const location = useLocation();
  const [isHealthHighlighted, setIsHealthHighlighted] = useState(false);
  const [period, setPeriod] = usePersistedQueryState<DashboardPeriodKey>({
    paramKey: 'period',
    storageKey: 'reports:period',
    defaultValue: 'week',
    parse: asDashboardPeriod,
    serialize: (value) => (value === 'week' ? null : value),
    validate: asDashboardPeriod,
  });

  // When the halt banner deep-links here (#system-health), scroll the health
  // tile into view and pulse it. Waits for `data` so the tile is mounted first.
  useEffect(() => {
    if (location.hash !== HEALTH_ANCHOR_HASH || !data) return;
    const tile = document.getElementById(HEALTH_ANCHOR_ID);
    if (!tile) return;
    // scrollIntoView is absent in some non-browser environments (e.g. jsdom).
    if (typeof tile.scrollIntoView === 'function') {
      tile.scrollIntoView({ behavior: 'smooth', block: 'center' });
    }
    setIsHealthHighlighted(true);
    const timer = window.setTimeout(() => { setIsHealthHighlighted(false); }, HEALTH_HIGHLIGHT_MS);
    return () => { window.clearTimeout(timer); };
  }, [location.hash, data]);

  return (
    <>
      <PageHeader
        title="Reports"
        subtitle="Cross-channel analytics — leads, spend and CPL in one view."
        actions={<ReportsPeriodSelector value={period} onChange={setPeriod} />}
      />
      <AsyncBoundary isLoading={isLoading} error={error} onRetry={() => void refetch()}>
        {data ? (
          <div className="grid grid-cols-1 gap-4 lg:grid-cols-4">
            <LeadsOverTimeTile period={selectReportPeriod(data.REPORTS, period)} />
            <CplTrendTile period={selectReportPeriod(data.REPORTS, period)} />
            <SpendByStageTile period={selectReportPeriod(data.REPORTS, period)} />
            <ChannelComparisonTile period={selectReportPeriod(data.REPORTS, period)} />
            <SystemHealthTile
              health={data.HEALTH}
              id={HEALTH_ANCHOR_ID}
              isHighlighted={isHealthHighlighted}
            />
            <CampaignPerformanceTile period={selectReportPeriod(data.REPORTS, period)} />
          </div>
        ) : null}
      </AsyncBoundary>
    </>
  );
}
