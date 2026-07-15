import { useState } from 'react';
import { useQuery } from '@tanstack/react-query';
import { Link } from 'react-router-dom';
import { OctagonAlert, X } from 'lucide-react';
import { usePanelRepository } from '@/shared/api/repositoryContext';
import { queryKeys } from '@/shared/api/queryKeys';
import { unwrap } from '@/shared/lib/result';
import { useCan } from '@/shared/hooks/useCan';
import { IDLE_REFETCH_MS } from '@/shared/hooks/pollIntervals';
import { HEALTH_ANCHOR_ID } from '@/features/reports/healthAnchor';

/**
 * Unmissable halt-tier banner. Rendered only while the engine reports an open halt flag
 * — content comes from the newest halt alert. Reads HEALTH/ALERTS from the dashboard
 * endpoint (sharing its cache), gated on `view_dashboard`: a leads-only member can't read
 * it and never saw a halt banner anyway, so the query simply stays idle for them.
 */
export function HaltBanner() {
  const repository = usePanelRepository();
  const canViewHealth = useCan('view_dashboard');
  const { data: state } = useQuery({
    queryKey: queryKeys.dashboard,
    queryFn: async () => unwrap(await repository.fetchDashboard()),
    enabled: canViewHealth,
    refetchInterval: IDLE_REFETCH_MS,
    staleTime: 2_000,
  });
  const [isDismissed, setIsDismissed] = useState(false);

  if (!state || isDismissed || state.HEALTH.overall !== 'halted') return null;
  const alert = state.ALERTS.find((a) => a.tier === 'halt') ?? null;

  return (
    <div
      role="alert"
      className="mb-4 flex items-center gap-3 rounded-tile border border-danger/40 bg-danger-soft px-5 py-3 text-[13px] shadow-tile"
    >
      <OctagonAlert className="size-[18px] shrink-0 text-danger" aria-hidden />
      <span className="grow">
        <strong className="font-bold text-danger">{alert?.title ?? 'Engine halted'}</strong>
        {' — '}
        {alert?.desc ?? 'see Health for the open flag.'}
      </span>
      {alert ? <span className="tabular shrink-0 text-text-faint">{alert.time}</span> : null}
      <Link
        to={{ pathname: '/reports', hash: HEALTH_ANCHOR_ID }}
        className="shrink-0 rounded-full border border-danger/40 bg-surface px-3 py-1.5 text-xs font-semibold text-danger transition-colors hover:bg-danger hover:text-white"
      >
        View health
      </Link>
      <button
        type="button"
        onClick={() => { setIsDismissed(true); }}
        aria-label="Dismiss"
        className="shrink-0 rounded-full p-1.5 text-text-muted transition-colors hover:bg-surface hover:text-text"
      >
        <X className="size-3.5" aria-hidden />
      </button>
    </div>
  );
}
