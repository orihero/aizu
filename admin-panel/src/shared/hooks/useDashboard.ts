import { useQuery } from '@tanstack/react-query';
import { usePanelRepository } from '@/shared/api/repositoryContext';
import { queryKeys } from '@/shared/api/queryKeys';
import { unwrap } from '@/shared/lib/result';
import { runAwareInterval } from '@/shared/hooks/pollIntervals';
import { useRunStatus } from '@/shared/hooks/useRunStatus';

/**
 * GET /api/dashboard — org-wide bento, ticker matches, health and alerts. Polls fast
 * during a run by observing the shared run state (no extra request — see useRunStatus).
 */
export function useDashboard() {
  const repository = usePanelRepository();
  const { isRunActive } = useRunStatus();
  return useQuery({
    queryKey: queryKeys.dashboard,
    queryFn: async () => unwrap(await repository.fetchDashboard()),
    refetchInterval: runAwareInterval(isRunActive),
    staleTime: 2_000,
  });
}
