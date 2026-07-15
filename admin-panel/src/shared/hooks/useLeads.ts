import { keepPreviousData, useQuery } from '@tanstack/react-query';
import { usePanelRepository } from '@/shared/api/repositoryContext';
import { queryKeys } from '@/shared/api/queryKeys';
import { unwrap } from '@/shared/lib/result';
import { runAwareInterval } from '@/shared/hooks/pollIntervals';
import { useRunStatus } from '@/shared/hooks/useRunStatus';
import type { LeadsQuery } from '@/shared/types/domain';

/**
 * GET /api/leads — org-wide leads, server-side filtered/sorted/paginated. Each
 * page+filter+sort combination caches independently; `keepPreviousData` holds the prior
 * page on screen while the next loads (no spinner flash on page change). Polls fast
 * during a run so new leads surface promptly.
 */
export function useLeads(query: LeadsQuery) {
  const repository = usePanelRepository();
  const { isRunActive } = useRunStatus();
  return useQuery({
    queryKey: queryKeys.leadsPage(query),
    queryFn: async () => unwrap(await repository.fetchLeads(query)),
    placeholderData: keepPreviousData,
    refetchInterval: runAwareInterval(isRunActive),
    staleTime: 2_000,
  });
}
