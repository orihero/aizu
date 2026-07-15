// Idle cadence — the DB updates continuously but nothing is time-critical.
export const IDLE_REFETCH_MS = 30_000;
// While a run is active, run-sensitive pages (dashboard, campaigns, leads) must reflect
// start AND finish quickly; 30s felt stuck. Centralised so every page polls in lockstep.
export const ACTIVE_RUN_REFETCH_MS = 4_000;

/** React Query refetchInterval for a run-sensitive query, given whether a run is live. */
export function runAwareInterval(isRunActive: boolean): number {
  return isRunActive ? ACTIVE_RUN_REFETCH_MS : IDLE_REFETCH_MS;
}
