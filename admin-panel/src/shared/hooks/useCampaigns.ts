import { useQuery } from '@tanstack/react-query';
import { usePanelRepository } from '@/shared/api/repositoryContext';
import { queryKeys } from '@/shared/api/queryKeys';
import { unwrap } from '@/shared/lib/result';
import { runAwareInterval } from '@/shared/hooks/pollIntervals';
import { useCan } from '@/shared/hooks/useCan';

/**
 * GET /api/campaigns — every campaign card, pooled org-wide sessions, and the RUN block.
 * This is the canonical owner of run state: it self-observes its own RUN.active to poll
 * fast during a run, and `useRunStatus` reads the same cache so other pages share it.
 *
 * Gated on `view_campaigns`: a leads-only member would 403 here, and since `useRunStatus`
 * (hence `useLeads`/the sidebar) pulls this in for ALL roles, gating keeps it from polling
 * a forbidden endpoint — it simply reports no active run instead.
 */
export function useCampaigns() {
  const repository = usePanelRepository();
  const enabled = useCan('view_campaigns');
  return useQuery({
    queryKey: queryKeys.campaigns,
    queryFn: async () => unwrap(await repository.fetchCampaigns()),
    enabled,
    refetchInterval: (query) => runAwareInterval(Boolean(query.state.data?.RUN.active)),
    staleTime: 2_000,
  });
}
