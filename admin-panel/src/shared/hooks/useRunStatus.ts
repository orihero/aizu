import { useCampaigns } from '@/shared/hooks/useCampaigns';
import { selectIsAnyRunActive } from '@/shared/selectors/campaigns';
import type { ActiveRun } from '@/shared/types/domain';

/**
 * Centralised run-active observer. Reads the RUN block owned by `useCampaigns`; because
 * React Query dedupes by query key, run-sensitive pages (dashboard, leads) can call this
 * to drive their polling cadence WITHOUT issuing a second request — they subscribe to the
 * same `queryKeys.campaigns` cache entry.
 */
export function useRunStatus(): { isRunActive: boolean; activeRun: ActiveRun | null } {
  const { data } = useCampaigns();
  const run = data?.RUN;
  return {
    isRunActive: run ? selectIsAnyRunActive(run) : false,
    activeRun: run?.active ?? null,
  };
}
