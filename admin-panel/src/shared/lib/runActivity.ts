import type { RunActivity, RunEvent, RunCounters, RunFlag, FleetJob } from '@/shared/types/domain';

/**
 * Accumulated, render-ready state of a live run's activity feed.
 *
 * The feed is paged: each poll returns only events with `id` greater than the
 * `after` cursor we sent. We fold those pages into one growing, oldest-first list
 * here so the drawer shows the whole run, not just the latest page. `counters`,
 * `flags` and `finished` are SNAPSHOTS (the server recomputes them each poll), so
 * they replace rather than accumulate. `fleetJob` is likewise a snapshot (null for
 * an in-process run) — replaced from each page, never accumulated.
 */
export interface RunActivityState {
  readonly runId: string | null;
  readonly events: readonly RunEvent[];   // oldest-first, deduped by global id
  readonly cursor: number;                // max event id folded in = the next `after`
  readonly counters: RunCounters | null;
  readonly flags: readonly RunFlag[];
  readonly finished: boolean;
  readonly fleetJob: FleetJob | null;     // null for in-process runs; snapshot-replaced
}

export const EMPTY_RUN_ACTIVITY: RunActivityState = {
  runId: null,
  events: [],
  cursor: 0,
  counters: null,
  flags: [],
  finished: false,
  fleetJob: null,
};

/**
 * Fold one polled page into the accumulator.
 *
 * Events append by strictly-increasing global `id` (never `seq`, which resets
 * per session under run-all). Anything at or below the current cursor is dropped,
 * so an overlapping/replayed page can't double-list a row. A page for a different
 * run resets the accumulator (a new run was opened in the same drawer).
 */
export function mergeRunActivity(acc: RunActivityState, page: RunActivity): RunActivityState {
  const base = acc.runId === page.runId ? acc : EMPTY_RUN_ACTIVITY;
  const fresh = page.events
    .filter((event) => event.id > base.cursor)
    .sort((a, b) => a.id - b.id);
  const events = fresh.length > 0 ? [...base.events, ...fresh] : base.events;
  const last = fresh.at(-1);
  const lastId = last ? last.id : base.cursor;
  return {
    runId: page.runId,
    events,
    cursor: Math.max(base.cursor, page.cursor, lastId),
    counters: page.counters,
    flags: page.flags,
    finished: page.finished,
    fleetJob: page.fleetJob,
  };
}
