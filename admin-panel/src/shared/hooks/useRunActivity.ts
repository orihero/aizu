import { useEffect, useRef } from 'react';
import { useQuery } from '@tanstack/react-query';
import { usePanelRepository } from '@/shared/api/repositoryContext';
import { queryKeys } from '@/shared/api/queryKeys';
import { unwrap } from '@/shared/lib/result';
import {
  EMPTY_RUN_ACTIVITY,
  mergeRunActivity,
  type RunActivityState,
} from '@/shared/lib/runActivity';

// A live run emits events every few seconds; 2s keeps the feed lively without
// hammering the bridge. Polling stops once the run reports `finished`.
const POLL_INTERVAL_MS = 2_000;

export interface UseRunActivityResult {
  readonly activity: RunActivityState | null;
  readonly isError: boolean;
  readonly error: unknown;
}

/**
 * Poll one run's live activity feed while it is in flight, accumulating paged
 * events into a single growing snapshot. Pass `null` when no run is active (e.g.
 * the drawer is closed) to disable polling entirely.
 *
 * The cursor is held in a ref (not query state) because each poll must send the
 * `after` of the page we already have; the accumulator advances it as pages fold
 * in. Polling halts when the run finishes — React Query keeps the final snapshot.
 */
export function useRunActivity(runId: string | null): UseRunActivityResult {
  const repository = usePanelRepository();
  const accumulator = useRef<RunActivityState>(EMPTY_RUN_ACTIVITY);

  // Drop the accumulated feed once no run is active (drawer closed / run cleared),
  // so reopening for a different run starts clean. Safe here — the query is
  // disabled when runId is null, so no in-flight queryFn races this reset. (The
  // in-queryFn run-id guard still covers an in-place run switch.)
  useEffect(() => {
    if (runId === null) accumulator.current = EMPTY_RUN_ACTIVITY;
  }, [runId]);

  const query = useQuery({
    queryKey: queryKeys.runActivity(runId ?? '∅'),
    enabled: runId !== null,
    staleTime: 0,
    gcTime: 0,
    retry: false,
    refetchInterval: (q) => (q.state.data?.finished ? false : POLL_INTERVAL_MS),
    queryFn: async () => {
      // After cursor resets to 0 whenever the run id changes (a new run opened).
      const after = accumulator.current.runId === runId ? accumulator.current.cursor : 0;
      const page = unwrap(await repository.fetchRunActivity(runId as string, after));
      const next = mergeRunActivity(accumulator.current, page);
      accumulator.current = next;
      return next;
    },
  });

  return { activity: query.data ?? null, isError: query.isError, error: query.error };
}
