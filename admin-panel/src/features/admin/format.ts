import type { FleetWorker } from '@/shared/schemas/admin';
import type { BadgeTone } from '@/shared/ui/Badge';
import { platformLabel } from '@/shared/lib/platformLabel';

const SEC_PER_MIN = 60;
const SEC_PER_HOUR = 3600;
const SEC_PER_DAY = 86400;

/** Compact "how long ago" from a duration in seconds (server-computed; no client clock). */
export function formatAge(seconds: number | null): string {
  if (seconds === null) return 'never';
  if (seconds < SEC_PER_MIN) return `${Math.floor(seconds)}s ago`;
  if (seconds < SEC_PER_HOUR) return `${Math.floor(seconds / SEC_PER_MIN)}m ago`;
  if (seconds < SEC_PER_DAY) return `${Math.floor(seconds / SEC_PER_HOUR)}h ago`;
  return `${Math.floor(seconds / SEC_PER_DAY)}d ago`;
}

/** Absolute local timestamp from an epoch-SECONDS value (matches the engine's REAL epoch). */
export function formatTimestamp(epochSeconds: number | null): string {
  if (epochSeconds === null) return '—';
  return new Date(epochSeconds * 1000).toLocaleString();
}

/** One-line summary of a worker's declared capabilities, e.g. "Instagram @acme +2". */
export function capabilitySummary(worker: FleetWorker): string {
  const head = worker.capabilities[0];
  if (!head) return 'none';
  const [, platform, handle] = head;
  const first = `${platformLabel(platform)} @${handle}`;
  const rest = worker.capabilities.length - 1;
  return rest > 0 ? `${first} +${rest}` : first;
}

/** Time-of-day for one run event, 24h with seconds. Epoch SECONDS (float), like every
 * engine timestamp. Deliberately clock-only: a log reads as a timeline, and repeating
 * the date on all 500 rows costs the width the message needs. */
export function formatClock(epochSeconds: number): string {
  return new Date(epochSeconds * 1000).toLocaleTimeString([], { hour12: false });
}

/**
 * How long a run took, from its start/end epoch SECONDS.
 *
 * Both ends must be real: a run still open has no duration yet, and substituting the
 * browser's clock for the missing end would silently report the wall time since the
 * bridge's `startedAt` — a number that drifts with every clock skew between the two
 * boxes. `—` is the honest answer. Same reason `finishedAt` is null while a run's last
 * session is still running (see `_build_admin_org_runs`).
 */
export function formatRunDuration(
  startedAt: number | null,
  finishedAt: number | null,
): string {
  if (startedAt === null || finishedAt === null) return '—';
  const seconds = Math.max(0, Math.round(finishedAt - startedAt));
  if (seconds < SEC_PER_MIN) return `${seconds}s`;
  if (seconds < SEC_PER_HOUR) {
    return `${Math.floor(seconds / SEC_PER_MIN)}m ${seconds % SEC_PER_MIN}s`;
  }
  const hours = Math.floor(seconds / SEC_PER_HOUR);
  return `${hours}h ${Math.floor((seconds % SEC_PER_HOUR) / SEC_PER_MIN)}m`;
}

/**
 * Badge tone for a run's folded status (`_build_admin_org_runs`).
 *
 * The status is a free string on the wire — a run whose sessions all ended cleanly reads
 * `done`, one with an open session `running`, and a halted/failed one says so. An
 * unrecognised value falls back to neutral rather than to a colour that would assert
 * health the console cannot vouch for.
 */
export function runStatusTone(status: string): BadgeTone {
  return RUN_STATUS_TONES[status] ?? 'neutral';
}

const RUN_STATUS_TONES: Readonly<Record<string, BadgeTone>> = {
  done: 'success',
  running: 'info',
  halted: 'warn',
  failed: 'danger',
};
