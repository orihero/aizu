import type { FleetWorker } from '@/shared/schemas/admin';
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
