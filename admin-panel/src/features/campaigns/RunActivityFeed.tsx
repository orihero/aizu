import { Check, Loader2, X } from 'lucide-react';
import { cn } from '@/shared/lib/cn';
import { formatMoney, formatNumber } from '@/shared/lib/formatters';
import type { FleetJob } from '@/shared/types/domain';
import {
  describeDelivery,
  lastActivityLabel,
  runPhaseLabel,
  subordinateCount,
  targetProgressPct,
  type RunActivityState,
} from '@/shared/lib/runActivity';

// A fleet run is "stalled" once it's been this long since its last emitted event
// while still leased/running — the worker is alive to the server but silent to us.
const STALL_THRESHOLD_SEC = 120;

// A run that reports one of these is over; the progress block drops its live styling
// (no pulse, no spinner) so a finished run never looks like it is still working.
const TERMINAL_PHASES = new Set(['done', 'failed', 'stopped']);

const FLEET_TERMINAL = new Set(['done', 'failed', 'interrupted']);
const FLEET_ACTIVE = new Set(['leased', 'running']);

// Worker nack codes → one plain-language clause. Anything not listed falls back to the
// raw code, which is still infinitely better than the bare "Finished on the fleet" an
// operator used to get for a run whose worker never even attached its browser.
const FLEET_REASONS: Readonly<Record<string, string>> = {
  cdp_unreachable: "the worker's Chrome could not be attached",
  worker_timeout: 'the run exceeded its time cap on the worker',
  worker_stall: 'the run stopped making progress on the worker',
  credential_fetch_failed: 'the worker could not fetch this platform’s credential',
  campaign_not_found: 'the worker could not resolve this campaign',
  soul_missing: 'the worker has no soul/voice profile for this campaign',
  campaign_malformed: 'the campaign brief could not be parsed',
  error: 'the run crashed on the worker',
  halted: 'the run was halted',
};

function humanFleetReason(reason: string): string {
  return FLEET_REASONS[reason] ?? reason;
}

/**
 * Status row for a run routed to the worker fleet. Renders nothing for an
 * in-process run (fleetJob null). For a fleet run it turns the four job states
 * into one plain-language line so the operator can tell queued vs running vs
 * stalled vs finished — the pieces the silent-hang case otherwise leaves invisible.
 *
 * Staleness is computed at render against Date.now(); the block re-renders every
 * ~2s while polling, so "last activity Xs ago" ticks without extra timers.
 */
export function FleetJobBanner({ fleetJob }: { readonly fleetJob: FleetJob | null }) {
  if (!fleetJob) return null;

  const { status, lastEventAt, reason } = fleetJob;
  const nowSec = Date.now() / 1000;
  const ageSec = lastEventAt === null ? null : Math.max(0, Math.round(nowSec - lastEventAt));

  const base = 'flex items-center gap-2 rounded-tile px-3 py-2 text-xs font-semibold';

  if (status === 'queued') {
    return (
      <div className={cn(base, 'bg-surface-2 text-text-muted')} role="status">
        <Loader2 className="size-3.5 animate-spin" aria-hidden />
        Waiting for a worker to pick this up…
      </div>
    );
  }

  if (FLEET_TERMINAL.has(status)) {
    // The wording is keyed off `status` only — a `done` job can carry a reason too, and
    // labelling that one "Failed" would be a lie.
    const failed = status !== 'done';
    return (
      <div
        className={cn(base, failed ? 'bg-danger-soft text-danger' : 'bg-success-soft text-success')}
        role="status"
      >
        {failed ? <X className="size-3.5" aria-hidden /> : <Check className="size-3.5" aria-hidden />}
        {failed && reason ? `Failed on the fleet — ${humanFleetReason(reason)}` : 'Finished on the fleet'}
      </div>
    );
  }

  if (FLEET_ACTIVE.has(status)) {
    const stalled = ageSec === null || ageSec > STALL_THRESHOLD_SEC;
    if (stalled) {
      return (
        <div className={cn(base, 'bg-warn-soft text-warn')} role="status">
          <span className="select-none" aria-hidden>⚠</span>
          Stalled — running on the fleet but no activity for {ageSec === null ? '—' : `${ageSec}s`}
        </div>
      );
    }
    return (
      <div className={cn(base, 'bg-surface-2 text-text')} role="status">
        <span className="size-2 shrink-0 animate-pulse rounded-full bg-success" aria-hidden />
        Running on fleet — last activity {ageSec}s ago
      </div>
    );
  }

  // Unknown status (schema keeps it a free string) — show a neutral, honest line.
  return (
    <div className={cn(base, 'bg-surface-2 text-text-muted')} role="status">
      Running on the fleet…
    </div>
  );
}

function CounterTile({ label, value }: { readonly label: string; readonly value: string }) {
  return (
    <div className="rounded-tile bg-surface-2 px-3 py-2">
      <div className="text-sm font-bold tabular-nums text-text-muted">{value}</div>
      <div className="text-[10px] font-semibold uppercase tracking-wide text-text-faint">{label}</div>
    </div>
  );
}

/**
 * The headline block: how many leads this run has found, against its target, plus the
 * phase word and a liveness beat.
 *
 * This is the whole customer-facing progress story now that the narrative log is a
 * superadmin surface. It leans on `leadsFound` (event-derived, so it moves live even on
 * a fleet run whose session counters stay 0 until ack) rather than on the counters —
 * that inversion is the point of Section E, and getting it backwards leaves a customer
 * watching a screen of zeroes for the length of a run.
 */
function ProgressHeadline({
  activity,
  targetLeadsHint,
}: {
  readonly activity: RunActivityState | null;
  readonly targetLeadsHint: number | null;
}) {
  const phase = activity?.phase ?? 'starting';
  const finished = activity?.finished ?? false;
  const live = !finished && !TERMINAL_PHASES.has(phase);
  const leadsFound = activity?.leadsFound ?? 0;
  const target = activity?.targetLeads ?? targetLeadsHint;
  const pct = targetProgressPct(leadsFound, target);
  const age = lastActivityLabel(activity?.lastEventAt ?? null);

  return (
    <div className="rounded-tile bg-surface-2 px-3 py-3">
      <div className="flex items-center justify-between gap-2">
        <span className="inline-flex items-center gap-1.5 text-xs font-bold text-text">
          {live ? (
            <span className="size-2 shrink-0 animate-pulse rounded-full bg-success" aria-hidden />
          ) : null}
          {runPhaseLabel(phase)}
        </span>
        {/* A timestamp is not a log — it is the beat that stops a silent run from
            reading as a dead screen. "No activity yet" is a different claim from
            "last activity 0s ago", so the null case gets its own words. */}
        <span className="text-[11px] font-medium tabular-nums text-text-faint">
          {age === null ? 'No activity yet' : `Last activity ${age}`}
        </span>
      </div>

      <div className="mt-2 flex items-baseline gap-1.5">
        <span className="font-head text-[28px] font-bold leading-none tabular-nums text-text">
          {formatNumber(leadsFound)}
        </span>
        <span className="text-xs font-semibold text-text-muted">
          {target === null ? 'leads found' : `of ${formatNumber(target)} leads`}
        </span>
      </div>

      {pct === null ? null : (
        <div className="mt-2 h-1.5 overflow-hidden rounded-full bg-surface">
          <div
            className="h-full rounded-full bg-brand transition-all"
            style={{ width: `${pct}%` }}
          />
        </div>
      )}
    </div>
  );
}

interface RunActivityFeedProps {
  readonly activity: RunActivityState | null;
  readonly isError: boolean;
  /** The clamped target the POST /api/run response echoed back, used only when the
   *  activity payload has none. An IN-PROCESS run's target is never persisted anywhere
   *  /api/run/activity can read it (only a fleet job carries it in its spec), so without
   *  this the most common run in dev shows a bare count with no denominator. */
  readonly targetLeadsHint?: number | null;
}

/**
 * Live progress for one run: what it has found against its target, what it is doing,
 * when it last did anything, and — subordinate to all of that — the raw counters and
 * spend. Driven by `useRunActivity`; presentational only.
 *
 * v27 (B3/Section E): the narrative event list is gone. It was never safely
 * customer-facing — a match event's detail carries the very username and comment the
 * redaction hides — and a "filtered" feed would have shipped exactly those rows and
 * trusted a filter to drop them. The server now folds the events into scalars instead.
 */
export function RunActivityFeed({
  activity,
  isError,
  targetLeadsHint = null,
}: RunActivityFeedProps) {
  if (isError) {
    return (
      <p className="text-xs font-medium text-text-faint">
        Couldn’t load live progress — the run may have just finished. Its outcome shows below when it ends.
      </p>
    );
  }

  const counters = activity?.counters;
  const flags = activity?.flags ?? [];
  const notice = describeDelivery(activity);

  return (
    <div className="space-y-3">
      <FleetJobBanner fleetJob={activity?.fleetJob ?? null} />

      <ProgressHeadline activity={activity} targetLeadsHint={targetLeadsHint} />

      {/* E.5/E.7. A finished run that found leads it never handed over must say so in
          both numbers: "0 leads" denies work that happened, and the found count alone
          implies leads the customer can open. */}
      {notice ? (
        <div className="rounded-tile bg-warn-soft px-3 py-2 text-warn" role="status">
          <p className="text-xs font-bold">{notice.headline}</p>
          <p className="mt-0.5 text-[11px] font-medium leading-4">{notice.detail}</p>
        </div>
      ) : null}

      {/* Subordinate counters. Every one degrades to "—" rather than 0: they ship in the
          ack body, so a fleet run reads 0 for its whole life and a dead-lettered one
          reads 0 forever — printing that as a real zero would claim work didn't happen. */}
      <div className="grid grid-cols-3 gap-2">
        <CounterTile label="Posts seen" value={subordinateCount(activity?.itemsScanned)} />
        <CounterTile label="Relevant" value={subordinateCount(activity?.relevantFound)} />
        <CounterTile label="Comments read" value={subordinateCount(counters?.commentsScored)} />
      </div>

      {/* Spend is a different unit (money), so it gets its own full-width row. It is
          never hidden or zeroed on an incomplete run — the charge was really incurred
          and the accounting is correct; the LABEL is what carries the caveat. */}
      <div className="flex items-center justify-between rounded-tile bg-surface-2 px-3 py-2">
        <span className="text-[10px] font-semibold uppercase tracking-wide text-text-faint">
          {notice ? 'Spend (incomplete run)' : 'Spend'}
        </span>
        <span className="text-sm font-bold tabular-nums text-text">
          {counters == null ? '—' : formatMoney(counters.spendUsd)}
        </span>
      </div>

      {flags.length > 0 ? (
        <div className="flex flex-wrap gap-1.5">
          {flags.map((flag) => (
            <span
              key={`${flag.kind}:${flag.detail ?? ''}`}
              className="rounded-full bg-warn-soft px-2 py-0.5 text-[10px] font-semibold text-warn"
              title={flag.detail ?? undefined}
            >
              {flag.kind}
            </span>
          ))}
        </div>
      ) : null}
    </div>
  );
}
