import { useQuery } from '@tanstack/react-query';
import { usePanelRepository } from '@/shared/api/repositoryContext';
import { queryKeys } from '@/shared/api/queryKeys';
import { unwrap } from '@/shared/lib/result';
import { IDLE_REFETCH_MS } from '@/shared/hooks/pollIntervals';

/** GET /api/settings — workspace config + team + invites + integrations. Operator
 *  surface; idle cadence is plenty. */
export function useSettings() {
  const repository = usePanelRepository();
  return useQuery({
    queryKey: queryKeys.settings,
    queryFn: async () => unwrap(await repository.fetchSettings()),
    refetchInterval: IDLE_REFETCH_MS,
    staleTime: 2_000,
  });
}
