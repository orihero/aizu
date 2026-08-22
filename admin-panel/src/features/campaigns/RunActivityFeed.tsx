import { Check, Loader2, X } from 'lucide-react';
import { cn } from '@/shared/lib/cn';
import { formatMoney } from '@/shared/lib/formatters';
import type { FleetJob, RunEvent, RunEventLevel } from '@/shared/types/domain';
import type { RunActivityState } from '@/shared/lib/runActivity';

// Render at most this many rows; a long run can emit thousands. Newest are kept.
const MAX_VISIBLE_EVENTS = 200;

// A fleet run is "stalled" once it's been this long since its last emitted event
// while still leased/running — the worker is alive to the server but silent to us.
const STALL_THRESHOLD_SEC = 120;

// Glyph + color per level — deliberately the same glyphs the engine logs with, so
// the feed reads like the colorful run log it mirrors.
const LEVEL_META: Readonly<Record<RunEventLevel, { glyph: string; className: string }>> = {
  info: { glyph: '▶', className: 'text-info' },
  success: { glyph: '✓', className: 'text-success' },
  warn: { glyph: '⚠', className: 'text-warn' },
  error: { glyph: '✗', className: 'text-danger' },
};

const COUNTER_TILES = [
  { key: 'reelsSeen', label: 'Reels' },
  { key: 'relevancePasses', label: 'Relevant' },
  { key: 'commentsScored', label: 'Comments' },
  { key: 'matches', label: 'Leads' },
  { key: 'likes', label: 'Likes' },
  { key: 'follows', label: 'Follows' },
] as const;

function formatClock(epochSeconds: number): string {
  return new Date(epochSeconds * 1000).toLocaleTimeString([], { hour12: false });
}

function CounterTile({ label, value }: { readonly label: string; readonly value: string }) {
  return (
    <div className="rounded-tile bg-surface-2 px-3 py-2">
      <div className="text-base font-bold tabular-nums text-text">{value}</div>
      <div className="text-[10px] font-semibold uppercase tracking-wide text-text-faint">{label}</div>
    </div>
  );
}

function EventRow({ event }: { readonly event: RunEvent }) {
  const meta = LEVEL_META[event.level];
  return (
    <li className="flex items-start gap-2 py-1.5">
      <span className={cn('select-none text-sm leading-5', meta.className)} aria-hidden>
        {meta.glyph}
      </span>
      <div className="min-w-0 flex-1">
        <p className="break-words text-sm leading-5 text-text">{event.message}</p>
        <div className="mt-0.5 flex items-center gap-2">
          <span className="rounded bg-surface-2 px-1.5 py-0.5 font-mono text-[10px] text-text-faint">
            {event.phase}
          </span>
          <time className="text-[10px] tabular-nums text-text-faint">{formatClock(event.createdAt)}</time>
        </div>
      </div>
    </li>
  );
}

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
 * stalled vs finished — the pieces the silent-hang case (one 'Run started' event
 * then nothing) otherwise leaves invisible.
 *
 * Staleness is computed at render against Date.now(); the feed re-renders every
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

interface RunActivityFeedProps {
  readonly activity: RunActivityState | null;
  readonly isError: boolean;
}

/**
 * Live preview of a running campaign: a counter strip (reels seen, relevance
 * passes, comments scored, leads, engagement) plus the newest-first narrative of
 * what the agent is doing right now. Driven by `useRunActivity`; presentational
 * only. Newest events are on top so the latest action needs no scrolling.
 */
export function RunActivityFeed({ activity, isError }: RunActivityFeedProps) {
  if (isError) {
    return (
      <p className="text-xs font-medium text-text-faint">
        Couldn’t load live activity — the run may have just finished. Its outcome shows below when it ends.
      </p>
    );
  }

  const counters = activity?.counters;
  const flags = activity?.flags ?? [];
  const events = activity?.events ?? [];
  const visible = events.length > MAX_VISIBLE_EVENTS ? events.slice(-MAX_VISIBLE_EVENTS) : events;

  return (
    <div className="space-y-3">
      <FleetJobBanner fleetJob={activity?.fleetJob ?? null} />

      <div className="grid grid-cols-3 gap-2">
        {COUNTER_TILES.map((tile) => (
          <CounterTile key={tile.key} label={tile.label} value={String(counters?.[tile.key] ?? 0)} />
        ))}
      </div>

      {/* Spend is a different unit (money), so it gets its own full-width row
          rather than dangling as an orphaned 7th cell in the 3-col count grid. */}
      <div className="flex items-center justify-between rounded-tile bg-surface-2 px-3 py-2">
        <span className="text-[10px] font-semibold uppercase tracking-wide text-text-faint">Spend</span>
        <span className="text-sm font-bold tabular-nums text-text">{formatMoney(counters?.spendUsd ?? 0)}</span>
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

      <div className="border-t border-border pt-2">
        {events.length === 0 ? (
          <p className="py-3 text-center text-xs text-text-faint">
            {activity?.finished
              ? 'No activity was recorded for this run.'
              : 'Waiting for the first event…'}
          </p>
        ) : (
          <ol className="divide-y divide-border/60">
            {[...visible].reverse().map((event) => (
              <EventRow key={event.id} event={event} />
            ))}
          </ol>
        )}
      </div>
    </div>
  );
}
