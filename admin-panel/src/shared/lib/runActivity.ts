import { formatNumber } from '@/shared/lib/formatters';
import type {
  Delivery,
  FleetJob,
  RunActivity,
  RunCounters,
  RunFlag,
  RunPhase,
} from '@/shared/types/domain';

/**
 * Render-ready state of one run's progress.
 *
 * v27: this is no longer an ACCUMULATOR. The narrative event log left the customer app
 * (B3), and what replaced it is a block of scalars the bridge folds out of those events
 * server-side (Section E). Every one is a max/sum over the whole run, so each poll is a
 * complete, monotonic snapshot — there are no pages left to fold and no cursor left to
 * advance, which is why `mergeRunActivity` is gone and `snapshotRunActivity` is a
 * projection. The superadmin console keeps its own event accumulator; the un-redacted
 * feed is its surface now, not this one's.
 */
export interface RunActivityState {
  readonly runId: string | null;
  /** Ack-time session counters. Null until a page lands. NOTE: for a fleet run these
   *  are 0 for the whole run (they ship in the ack body), which is exactly why they are
   *  the SUBORDINATE display and `leadsFound` below is the primary one. */
  readonly counters: RunCounters | null;
  readonly flags: readonly RunFlag[];
  readonly finished: boolean;
  readonly fleetJob: FleetJob | null;
  /** Customer-safe word for what the run is doing right now. */
  readonly phase: RunPhase;
  /** What the run DISCOVERED — event-derived, so it moves live even on a fleet run. */
  readonly leadsFound: number;
  /** What actually REACHED the account. `null` is UNKNOWN (a bridge that never reported
   *  it), never zero — read `delivery` for the verdict rather than comparing these two. */
  readonly leadsDelivered: number | null;
  readonly delivery: Delivery;
  readonly itemsScanned: number;
  readonly relevantFound: number;
  /** Epoch SECONDS of the newest event — the liveness beat, not a log. */
  readonly lastEventAt: number | null;
  /** The run's plan-clamped target. A TARGET, not a ceiling: runs overshoot it (E.6). */
  readonly targetLeads: number | null;
}

/** The "nothing polled yet" state. `phase: 'starting'` on purpose (E.3): zero events on
 *  a run we are actively polling means it is starting, not that it found nothing. */
export const EMPTY_RUN_ACTIVITY: RunActivityState = {
  runId: null,
  counters: null,
  flags: [],
  finished: false,
  fleetJob: null,
  phase: 'starting',
  leadsFound: 0,
  leadsDelivered: null,
  delivery: 'delivered',
  itemsScanned: 0,
  relevantFound: 0,
  lastEventAt: null,
  targetLeads: null,
};

/**
 * Project one polled page into render-ready state.
 *
 * `events` is deliberately NOT carried across: it is always `[]` for an org caller, and
 * dropping it at the seam means no customer-facing component can render a run event even
 * by accident — the redaction does not depend on a component remembering not to.
 */
export function snapshotRunActivity(page: RunActivity): RunActivityState {
  return {
    runId: page.runId,
    counters: page.counters,
    flags: page.flags,
    finished: page.finished,
    fleetJob: page.fleetJob,
    phase: page.phase,
    leadsFound: page.leadsFound,
    leadsDelivered: page.leadsDelivered,
    delivery: page.delivery,
    itemsScanned: page.itemsScanned,
    relevantFound: page.relevantFound,
    lastEventAt: page.lastEventAt,
    targetLeads: page.targetLeads,
  };
}

// The phase word a customer sees. Sentence-shaped rather than an internal token: the
// server already mapped its own phases through an allow-list, so anything arriving here
// is one of seven safe words and `working` is the honest degrade for a phase we (or a
// future engine) don't recognise.
const PHASE_LABEL: Readonly<Record<RunPhase, string>> = {
  starting: 'Starting up',
  searching: 'Searching for posts',
  qualifying: 'Reading comments',
  stopped: 'Stopped',
  done: 'Finished',
  failed: 'Failed',
  working: 'Working',
};

export function runPhaseLabel(phase: RunPhase): string {
  return PHASE_LABEL[phase];
}

/**
 * "12s ago" / "4m ago" / "2h ago" for the liveness line, or null when the run has
 * emitted nothing yet (the caller says "no activity yet", which is a different claim
 * from "last activity 0s ago").
 *
 * `nowMs` is injectable so the age is testable without freezing the clock globally.
 */
export function lastActivityLabel(
  lastEventAt: number | null,
  nowMs: number = Date.now(),
): string | null {
  if (lastEventAt === null) return null;
  const sec = Math.max(0, Math.round(nowMs / 1000 - lastEventAt));
  if (sec < 60) return `${sec}s ago`;
  const min = Math.floor(sec / 60);
  if (min < 60) return `${min}m ago`;
  return `${Math.floor(min / 60)}h ago`;
}

/**
 * A SUBORDINATE counter's display value: "—" for anything we cannot vouch for.
 *
 * Zero maps to "—" deliberately. These counters ship in the ack body, so a fleet run
 * reads 0 for its whole life (E.1) and one that never acks reads 0 forever — a rendered
 * "0 reels" would be a claim the run scanned nothing, which is the exact "unknown read
 * as zero" mistake CLAUDE.md calls out. The primary progress (`leadsFound`) is
 * event-derived and therefore live, so it is the number that may legitimately show 0.
 */
export function subordinateCount(value: number | null | undefined): string {
  return value === null || value === undefined || value === 0 ? '—' : formatNumber(value);
}

/** Percent of the run's lead target reached, or null when it has no known target.
 *  Clamped at 100 for the BAR only — the numbers beside it stay honest, because a run
 *  really can pass its target (E.6) and a bar that overflows its track is just broken. */
export function targetProgressPct(leadsFound: number, targetLeads: number | null): number | null {
  if (targetLeads === null || targetLeads <= 0) return null;
  return Math.min(100, Math.round((leadsFound / targetLeads) * 100));
}

/** The E.5/E.7 honest state for a run whose harvest never reached the account. */
export interface DeliveryNotice {
  readonly headline: string;
  readonly detail: string;
}

/**
 * Describe the found-vs-delivered gap, or null when there is nothing to explain.
 *
 * Only `not_delivered` produces a notice. `pending` is a live fleet run whose rows land
 * at ack — ordinary lag, and warning about it would stamp an alarm on every healthy run.
 * The verdict is read off `delivery` rather than re-derived from the two numbers so this
 * component and the campaign card can never disagree about what the gap means.
 */
export function describeDelivery(activity: RunActivityState | null): DeliveryNotice | null {
  if (activity === null || activity.delivery !== 'not_delivered') return null;
  const found = formatNumber(activity.leadsFound);
  // `leadsDelivered` null is UNKNOWN. The server only says `not_delivered` when it has
  // both numbers, but say "not confirmed" rather than print a zero we didn't receive.
  const reached = activity.leadsDelivered === null
    ? 'delivery was never confirmed'
    : `${formatNumber(activity.leadsDelivered)} reached your account`;
  return {
    headline: `Found ${found} · ${reached}`,
    detail:
      'This run ended before its leads could be handed over, so they are not in your ' +
      'leads list. The spend below was still incurred — it is spend on an incomplete run.',
  };
}
