import { useQuery } from '@tanstack/react-query';
import { usePanelRepository } from '@/shared/api/repositoryContext';
import { queryKeys } from '@/shared/api/queryKeys';
import { unwrap } from '@/shared/lib/result';
import { IDLE_REFETCH_MS } from '@/shared/hooks/pollIntervals';

/** GET /api/reports — org-wide time series + per-campaign rollup. Not run-critical, so
 *  it polls on the idle cadence only. */
export function useReports() {
  const repository = usePanelRepository();
  return useQuery({
    queryKey: queryKeys.reports,
    queryFn: async () => unwrap(await repository.fetchReports()),
    refetchInterval: IDLE_REFETCH_MS,
    staleTime: 2_000,
  });
}
