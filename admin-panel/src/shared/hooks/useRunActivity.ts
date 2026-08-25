import { useQuery } from '@tanstack/react-query';
import { usePanelRepository } from '@/shared/api/repositoryContext';
import { queryKeys } from '@/shared/api/queryKeys';
import { unwrap } from '@/shared/lib/result';
import { snapshotRunActivity, type RunActivityState } from '@/shared/lib/runActivity';

// A live run's scalars move every few seconds; 2s keeps the progress lively without
// hammering the bridge. Polling stops once the run reports `finished`.
const POLL_INTERVAL_MS = 2_000;

// The org-facing feed has nothing to page — `events` is always empty and `cursor` never
// advances (Section E) — so `after` is a constant. Kept in the call because the endpoint
// still accepts it and a pre-v27 bridge would otherwise re-send the whole log every poll.
const NO_CURSOR = 0;

export interface UseRunActivityResult {
  readonly activity: RunActivityState | null;
  readonly isError: boolean;
  readonly error: unknown;
}

/**
 * Poll one run's live progress while it is in flight. Pass `null` when no run is active
 * (e.g. the drawer is closed) to disable polling entirely.
 *
 * v27: this no longer accumulates anything. Each poll is a complete, monotonic snapshot
 * of the run's scalars, so there is no cursor ref and no cross-poll state — a page
 * simply replaces the last one, and switching runs can no longer leak the previous
 * run's rows into the new one because there are no rows. Polling halts when the run
 * finishes; React Query keeps the final snapshot so the drawer still shows the outcome.
 */
export function useRunActivity(runId: string | null): UseRunActivityResult {
  const repository = usePanelRepository();

  const query = useQuery({
    queryKey: queryKeys.runActivity(runId ?? '∅'),
    enabled: runId !== null,
    staleTime: 0,
    gcTime: 0,
    retry: false,
    refetchInterval: (q) => (q.state.data?.finished ? false : POLL_INTERVAL_MS),
    queryFn: async () =>
      snapshotRunActivity(unwrap(await repository.fetchRunActivity(runId as string, NO_CURSOR))),
  });

  return { activity: query.data ?? null, isError: query.isError, error: query.error };
}
