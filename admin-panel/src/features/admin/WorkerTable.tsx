import { useState } from 'react';
import { Server, ShieldOff } from 'lucide-react';
import { Badge, type BadgeTone } from '@/shared/ui/Badge';
import { Button } from '@/shared/ui/Button';
import { ConfirmDialog } from '@/shared/ui/ConfirmDialog';
import { EmptyState } from '@/shared/ui/EmptyState';
import type { FleetWorker } from '@/shared/schemas/admin';
import { capabilitySummary, formatAge } from './format';
import {
  healthSummary, remedyFor, statusLabel, titleFor, type HealthSummary,
} from './preflightCopy';
import { useRevokeWorker } from './adminHooks';

const STATUS_TONE: Readonly<Record<FleetWorker['status'], BadgeTone>> = {
  online: 'success',
  stale: 'warn',
  offline: 'neutral',
};

/** `title` is spread rather than passed as `undefined`: exactOptionalPropertyTypes. */
function HealthBadge({ health }: { readonly health: HealthSummary }) {
  return (
    <Badge tone={health.tone} {...(health.title !== null ? { title: health.title } : {})}>
      {health.label}
    </Badge>
  );
}

/**
 * The expanded preflight rows for one worker.
 *
 * `detail` is worker-authored text on a superadmin surface: it is interpolated as a text
 * child (never `dangerouslySetInnerHTML`), which is what keeps a box from putting markup
 * in this console (E1/E2/F18).
 */
function PreflightDetail({ worker }: { readonly worker: FleetWorker }) {
  const rows = worker.preflight?.failed ?? [];
  if (rows.length === 0) return null;
  return (
    <tr className="border-b border-border bg-surface-2/40 last:border-0">
      <td colSpan={9} className="px-4 py-3">
        <ul className="flex flex-col gap-2">
          {rows.map((row) => {
            const remedy = remedyFor(row);
            return (
              <li key={row.id} className="text-xs">
                <div className="flex items-center gap-2">
                  <Badge tone={row.severity === 'fatal' ? 'danger' : 'warn'}>
                    {statusLabel(row.status)}
                  </Badge>
                  <span className="font-semibold text-text">{titleFor(row.id)}</span>
                  <span className="text-text-faint">{row.id}</span>
                </div>
                {row.detail !== null && (
                  <div className="mt-0.5 text-text-muted">{row.detail}</div>
                )}
                {remedy !== null && <div className="mt-0.5 text-text-muted">{remedy}</div>}
              </li>
            );
          })}
        </ul>
      </td>
    </tr>
  );
}

function WorkerRow({ worker }: { readonly worker: FleetWorker }) {
  const revoke = useRevokeWorker();
  const [confirming, setConfirming] = useState(false);
  const [expanded, setExpanded] = useState(false);
  const revoked = worker.revokedAt !== null;
  const label = worker.displayName ?? worker.host ?? worker.id;
  const health = healthSummary(worker.preflight);
  const failedCount = worker.preflight?.failed.length ?? 0;

  return (
    <>
    <tr className="border-b border-border last:border-0">
      <td className="px-4 py-3">
        <Badge tone={STATUS_TONE[worker.status]}>{worker.status}</Badge>
      </td>
      <td className="px-4 py-3">
        <div className="font-semibold text-text" title={worker.id}>
          {label}
        </div>
        <div className="text-xs text-text-muted">
          {worker.os ?? 'unknown OS'}
          {worker.orgId !== null ? ` · org #${worker.orgId}` : ''}
        </div>
      </td>
      <td className="px-4 py-3">
        {failedCount > 0 ? (
          <button
            type="button"
            className="text-left"
            aria-expanded={expanded}
            onClick={() => { setExpanded((v) => !v); }}
          >
            <HealthBadge health={health} />
          </button>
        ) : (
          <HealthBadge health={health} />
        )}
      </td>
      <td className="px-4 py-3 text-text-muted">{capabilitySummary(worker)}</td>
      <td className="px-4 py-3">
        {worker.currentJob ? (
          <div>
            <div className="font-medium text-text" title={worker.currentJob.jobId}>
              {worker.currentJob.campaignId ?? worker.currentJob.jobId}
            </div>
            <div className="text-xs text-text-muted">
              {worker.currentJob.platform ?? 'unknown'} · {worker.currentJob.status}
            </div>
          </div>
        ) : (
          <span className="text-text-faint">idle</span>
        )}
      </td>
      <td className="px-4 py-3 tabular-nums text-text-muted">
        {worker.currentSessions}/{worker.maxSessions}
      </td>
      <td className="px-4 py-3 text-text-muted">{worker.agentVersion ?? '—'}</td>
      <td className="px-4 py-3 text-text-muted">{formatAge(worker.lastSeenAgeSec)}</td>
      <td className="px-4 py-3 text-right">
        {revoked ? (
          <Badge tone="danger">revoked</Badge>
        ) : (
          <Button variant="ghost" onClick={() => { setConfirming(true); }}>
            <ShieldOff className="size-3.5" aria-hidden />
            Revoke
          </Button>
        )}
        <ConfirmDialog
          isOpen={confirming}
          title="Revoke worker token?"
          tone="danger"
          confirmLabel="Revoke"
          isPending={revoke.isPending}
          message={
            <>
              <span className="font-semibold text-text">{label}</span> will be signed out and
              must be re-enrolled with a new enrolment token before it can lease jobs again.
            </>
          }
          onConfirm={() => {
            revoke.mutate(worker.id, { onSettled: () => { setConfirming(false); } });
          }}
          onClose={() => { setConfirming(false); }}
        />
      </td>
    </tr>
    {expanded && <PreflightDetail worker={worker} />}
    </>
  );
}

export function WorkerTable({ workers }: { readonly workers: readonly FleetWorker[] }) {
  if (workers.length === 0) {
    return (
      <EmptyState
        icon={Server}
        title="No workers registered"
        description="Worker boxes appear here once they register and heartbeat."
      />
    );
  }
  return (
    <div className="overflow-x-auto">
      <table className="w-full min-w-[920px] text-left text-[13px]">
        <thead>
          <tr className="border-b border-border text-xs font-semibold uppercase tracking-wide text-text-faint">
            <th className="px-4 py-2.5">Status</th>
            <th className="px-4 py-2.5">Worker</th>
            <th className="px-4 py-2.5">Health</th>
            <th className="px-4 py-2.5">Capabilities</th>
            <th className="px-4 py-2.5">Running</th>
            <th className="px-4 py-2.5">Sessions</th>
            <th className="px-4 py-2.5">Agent</th>
            <th className="px-4 py-2.5">Last seen</th>
            <th className="px-4 py-2.5 text-right">Actions</th>
          </tr>
        </thead>
        <tbody>
          {workers.map((w) => (
            <WorkerRow key={w.id} worker={w} />
          ))}
        </tbody>
      </table>
    </div>
  );
}
