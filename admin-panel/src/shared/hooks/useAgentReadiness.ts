import { useCallback, useRef, useState } from 'react';
import { useQuery } from '@tanstack/react-query';
import { usePanelRepository } from '@/shared/api/repositoryContext';
import { queryKeys } from '@/shared/api/queryKeys';
import { unwrap } from '@/shared/lib/result';
import type { AgentReadiness } from '@/shared/types/domain';

// GET /api/agent/readiness tolerates a <=60s-old cached result unless `refresh` forces
// a live probe — poll at that same cadence so we're never asking more often than the
// server would actually re-check. react-query's global refetchOnWindowFocus (see
// app/providers.tsx) covers "came back to the tab" for free, on top of this interval.
const POLL_INTERVAL_MS = 60_000;

export interface UseAgentReadinessResult {
  /** undefined only before the first fetch resolves. */
  readonly readiness: AgentReadiness | undefined;
  readonly isLoading: boolean;
  readonly isError: boolean;
  /** Force a live probe past the server's cache — the banner's "Re-check" button. */
  readonly recheck: () => Promise<void>;
  /** True only while a manually-triggered recheck() is in flight (not the silent
   * background poll), so the "Re-check" button's spinner reflects an actual click. */
  readonly isRechecking: boolean;
}

/**
 * Global poll of the Instagram warmed-browser agent's readiness (CDP reachable +
 * session logged in), shared by every consumer via the query cache — the banner and
 * anything else that cares about "can a live run start right now" read the same data
 * instead of each opening their own probe.
 */
export function useAgentReadiness(): UseAgentReadinessResult {
  const repository = usePanelRepository();
  // queryFn takes no args, so a manually-forced live probe is threaded through a ref
  // the next queryFn invocation reads and clears — same trick useRunActivity uses for
  // its cursor, just for a one-shot flag instead of an accumulator.
  const forceRefresh = useRef(false);
  const [isRechecking, setIsRechecking] = useState(false);

  const { data, isLoading, isError, refetch } = useQuery({
    queryKey: queryKeys.agentReadiness,
    queryFn: async () => {
      const refresh = forceRefresh.current;
      forceRefresh.current = false;
      return unwrap(await repository.getAgentReadiness(refresh ? { refresh: true } : undefined));
    },
    refetchInterval: POLL_INTERVAL_MS,
    staleTime: 30_000,
  });

  const recheck = useCallback(async () => {
    forceRefresh.current = true;
    setIsRechecking(true);
    try {
      await refetch();
    } finally {
      setIsRechecking(false);
    }
  }, [refetch]);

  return { readiness: data, isLoading, isError, recheck, isRechecking };
}
