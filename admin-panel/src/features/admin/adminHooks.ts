import { useEffect, useRef } from 'react';
import { keepPreviousData, useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { usePanelRepository } from '@/shared/api/repositoryContext';
import { queryKeys } from '@/shared/api/queryKeys';
import { unwrap } from '@/shared/lib/result';
import type {
  AdminOrgLeadsQuery,
  AdminRunActivity,
  AdminRunEvent,
  ControlFlagSetInput,
  EnqueueJobInput,
  ExecutionBackend,
  ImpersonateInput,
  MintEnrolmentTokenInput,
} from '@/shared/schemas/admin';

const MODEL_COMPARISON_STATS_POLL_MS = 15_000;

/** Workers heartbeat every ~20s; poll a touch faster so presence stays fresh. */
const FLEET_POLL_MS = 10_000;
/** Default rows fetched for the audit log (server clamps 1..1000). */
const AUDIT_LIMIT = 200;

// ---- reads ----------------------------------------------------------------

export function useFleet() {
  const repository = usePanelRepository();
  return useQuery({
    queryKey: queryKeys.adminFleet,
    queryFn: async () => unwrap(await repository.fetchFleet()),
    refetchInterval: FLEET_POLL_MS,
    staleTime: 2_000,
  });
}

export function useControlFlags() {
  const repository = usePanelRepository();
  return useQuery({
    queryKey: queryKeys.adminControlFlags,
    queryFn: async () => unwrap(await repository.fetchControlFlags()),
    staleTime: 5_000,
  });
}

export function useEnrolmentTokens() {
  const repository = usePanelRepository();
  return useQuery({
    queryKey: queryKeys.adminEnrolmentTokens,
    queryFn: async () => unwrap(await repository.fetchEnrolmentTokens()),
    staleTime: 5_000,
  });
}

export function useAdminOrgs() {
  const repository = usePanelRepository();
  return useQuery({
    queryKey: queryKeys.adminOrgs,
    queryFn: async () => unwrap(await repository.fetchAdminOrgs()),
    staleTime: 10_000,
  });
}

export function useAdminOrgCampaigns(orgId: number) {
  const repository = usePanelRepository();
  return useQuery({
    queryKey: queryKeys.adminOrgCampaigns(orgId),
    queryFn: async () => unwrap(await repository.fetchAdminOrgCampaigns(orgId)),
    staleTime: 10_000,
  });
}

export function useAdminOrgLeads(query: AdminOrgLeadsQuery) {
  const repository = usePanelRepository();
  return useQuery({
    queryKey: queryKeys.adminOrgLeads(query.orgId, query.page, query.pageSize),
    queryFn: async () => unwrap(await repository.fetchAdminOrgLeads(query)),
    placeholderData: keepPreviousData,
    staleTime: 10_000,
  });
}

/** One org's recent runs — the picker for the narrative feed. Static enough that the
 * console does not poll it; opening a run polls the feed, which is where the movement is. */
export function useAdminOrgRuns(orgId: number) {
  const repository = usePanelRepository();
  return useQuery({
    queryKey: queryKeys.adminOrgRuns(orgId),
    queryFn: async () => unwrap(await repository.fetchAdminOrgRuns(orgId)),
    staleTime: 10_000,
  });
}

// ---- run log (v27 superadmin narrative feed) -------------------------------

/** A live run emits events every few seconds; 2s keeps the log tailing without
 * hammering the bridge. Polling stops once the run reports `finished`. */
const RUN_ACTIVITY_POLL_MS = 2_000;
/**
 * …but a page that moved the cursor forward may not be the last one: the bridge caps a
 * page (`Store.fetch_run_events` LIMITs it), so the console must walk the backlog on the
 * cursor rather than render the first page and call it the run. Re-poll almost at once in
 * that case, which drains a long finished run's history in a few round-trips.
 *
 * Deliberately keyed off "the page advanced us", NOT off a mirrored copy of the server's
 * page size: a constant duplicated here would silently start truncating the day the
 * bridge changed its LIMIT. The cost is one extra (empty) request to confirm the end.
 */
const RUN_ACTIVITY_DRAIN_MS = 150;

/**
 * Accumulated, render-ready state of one run's FULL narrative feed.
 *
 * Events accumulate (each poll returns only rows past the cursor we sent); `counters`,
 * `flags` and `finished` are snapshots the bridge recomputes every poll, so they replace.
 * This is the un-redacted admin shape — never plumb it into an org-facing component.
 */
export interface AdminRunActivityState {
  readonly runId: string | null;
  readonly events: readonly AdminRunEvent[];  // oldest-first, deduped by global id
  readonly cursor: number;                    // max event id folded in = the next `after`
  readonly counters: AdminRunActivity['counters'] | null;
  readonly flags: AdminRunActivity['flags'];
  readonly finished: boolean;
  /** The last page moved the cursor forward, so there may be another page behind it. */
  readonly draining: boolean;
}

export const EMPTY_ADMIN_RUN_ACTIVITY: AdminRunActivityState = {
  runId: null,
  events: [],
  cursor: 0,
  counters: null,
  flags: [],
  finished: false,
  draining: false,
};

/**
 * Fold one polled page into the accumulator.
 *
 * Rows append by strictly-increasing global `id` — never `seq`, which RESETS per session
 * and would let a batch run's second session replay ids the console already holds. A page
 * for a different run resets the accumulator (another run was opened in the same console).
 */
export function mergeAdminRunActivity(
  acc: AdminRunActivityState,
  page: AdminRunActivity,
): AdminRunActivityState {
  const base = acc.runId === page.runId ? acc : EMPTY_ADMIN_RUN_ACTIVITY;
  const fresh = page.events
    .filter((event) => event.id > base.cursor)
    .sort((a, b) => a.id - b.id);
  const last = fresh.at(-1);
  return {
    runId: page.runId,
    events: fresh.length > 0 ? [...base.events, ...fresh] : base.events,
    cursor: Math.max(base.cursor, page.cursor, last ? last.id : 0),
    counters: page.counters,
    flags: page.flags,
    finished: page.finished,
    // Keyed off rows we actually FOLDED IN, not off rows the page carried: a replayed or
    // overlapping page adds nothing, and treating it as progress would drain forever
    // against a bridge that keeps answering the same rows.
    draining: fresh.length > 0,
  };
}

export interface UseAdminRunActivityResult {
  readonly activity: AdminRunActivityState | null;
  readonly isError: boolean;
  readonly error: Error | null;
}

/**
 * Poll one run's full feed, paging forward on the monotonic event-id cursor. Pass `null`
 * when no run is selected to disable polling entirely.
 *
 * The cursor lives in a ref rather than query state because every poll must send the
 * `after` of the page already folded in; putting it in the key would give each poll its
 * own cache entry and lose the accumulated history (see `queryKeys.adminRunActivity`).
 */
export function useAdminRunActivity(runId: string | null): UseAdminRunActivityResult {
  const repository = usePanelRepository();
  const accumulator = useRef<AdminRunActivityState>(EMPTY_ADMIN_RUN_ACTIVITY);

  // Drop the accumulated feed once no run is selected, so opening a different run starts
  // clean. Safe here — the query is disabled when runId is null, so no in-flight queryFn
  // races this reset; the in-queryFn run-id guard still covers an in-place run switch.
  useEffect(() => {
    if (runId === null) accumulator.current = EMPTY_ADMIN_RUN_ACTIVITY;
  }, [runId]);

  const query = useQuery({
    queryKey: queryKeys.adminRunActivity(runId ?? '∅'),
    enabled: runId !== null,
    staleTime: 0,
    gcTime: 0,
    retry: false,
    refetchInterval: (q) => {
      const state = q.state.data;
      if (state === undefined) return RUN_ACTIVITY_POLL_MS;
      if (state.draining) return RUN_ACTIVITY_DRAIN_MS;
      return state.finished ? false : RUN_ACTIVITY_POLL_MS;
    },
    queryFn: async () => {
      // The cursor resets to 0 whenever the run id changes (a new run was opened).
      const after = accumulator.current.runId === runId ? accumulator.current.cursor : 0;
      const page = unwrap(await repository.fetchAdminRunActivity({ runId: runId as string, after }));
      const next = mergeAdminRunActivity(accumulator.current, page);
      accumulator.current = next;
      return next;
    },
  });

  return { activity: query.data ?? null, isError: query.isError, error: query.error };
}

export function useAudit() {
  const repository = usePanelRepository();
  return useQuery({
    queryKey: queryKeys.adminAudit,
    queryFn: async () => unwrap(await repository.fetchAudit(AUDIT_LIMIT)),
    staleTime: 5_000,
  });
}

export function useExecutionBackend() {
  const repository = usePanelRepository();
  return useQuery({
    queryKey: queryKeys.adminExecutionBackend,
    queryFn: async () => unwrap(await repository.fetchExecutionBackend()),
    staleTime: 10_000,
  });
}

export function useModelComparisonSettings() {
  const repository = usePanelRepository();
  return useQuery({
    queryKey: queryKeys.adminModelComparison,
    queryFn: async () => unwrap(await repository.fetchModelComparisonSettings()),
    staleTime: 10_000,
  });
}

export function useModelComparisonStats() {
  const repository = usePanelRepository();
  return useQuery({
    queryKey: queryKeys.adminModelComparisonStats,
    queryFn: async () => unwrap(await repository.fetchModelComparisonStats()),
    refetchInterval: MODEL_COMPARISON_STATS_POLL_MS,
    staleTime: 5_000,
  });
}

// ---- writes ---------------------------------------------------------------

export function useSetControlFlag() {
  const repository = usePanelRepository();
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: async (input: ControlFlagSetInput) => {
      unwrap(await repository.setControlFlag(input));
    },
    // A flag change shifts both the flags list and (via halt/drain) the fleet view.
    onSettled: () => {
      void queryClient.invalidateQueries({ queryKey: queryKeys.adminControlFlags });
      void queryClient.invalidateQueries({ queryKey: queryKeys.adminFleet });
    },
  });
}

export function useRevokeWorker() {
  const repository = usePanelRepository();
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: async (workerId: string) => {
      unwrap(await repository.revokeWorker(workerId));
    },
    onSettled: () => {
      void queryClient.invalidateQueries({ queryKey: queryKeys.adminFleet });
    },
  });
}

export function useMintEnrolmentToken() {
  const repository = usePanelRepository();
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: async (input: MintEnrolmentTokenInput) =>
      unwrap(await repository.mintEnrolmentToken(input)),
    // The token isn't redeemed yet — no worker exists from this alone, so the
    // fleet view is untouched; only the enrolment-tokens list grew by one.
    onSettled: () => {
      void queryClient.invalidateQueries({ queryKey: queryKeys.adminEnrolmentTokens });
    },
  });
}

export function useRevokeEnrolmentToken() {
  const repository = usePanelRepository();
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: async (tokenId: string) => {
      unwrap(await repository.revokeEnrolmentToken(tokenId));
    },
    onSettled: () => {
      void queryClient.invalidateQueries({ queryKey: queryKeys.adminEnrolmentTokens });
    },
  });
}

export function useEnqueueJob() {
  const repository = usePanelRepository();
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: async (input: EnqueueJobInput) => unwrap(await repository.enqueueJob(input)),
    onSettled: () => {
      void queryClient.invalidateQueries({ queryKey: queryKeys.adminFleet });
    },
  });
}

export function useSetExecutionBackend() {
  const repository = usePanelRepository();
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: async (backend: ExecutionBackend) => {
      unwrap(await repository.setExecutionBackend(backend));
    },
    onSettled: () => {
      void queryClient.invalidateQueries({ queryKey: queryKeys.adminExecutionBackend });
    },
  });
}

export function useSetModelComparisonEnabled() {
  const repository = usePanelRepository();
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: async (enabled: boolean) => {
      unwrap(await repository.setModelComparisonEnabled(enabled));
    },
    onSettled: () => {
      void queryClient.invalidateQueries({ queryKey: queryKeys.adminModelComparison });
    },
  });
}

export function useStartImpersonation() {
  const repository = usePanelRepository();
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: async (input: ImpersonateInput) => {
      unwrap(await repository.startImpersonation(input));
    },
    // Every cached org read is now the target org's — drop them all so nothing stale lingers.
    onSettled: () => queryClient.invalidateQueries(),
  });
}
