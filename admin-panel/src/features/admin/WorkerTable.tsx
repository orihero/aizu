import { useState } from 'react';
import { Server, ShieldOff } from 'lucide-react';
import { Badge, type BadgeTone } from '@/shared/ui/Badge';
import { Button } from '@/shared/ui/Button';
import { ConfirmDialog } from '@/shared/ui/ConfirmDialog';
import { EmptyState } from '@/shared/ui/EmptyState';
import type { FleetWorker } from '@/shared/schemas/admin';
import { capabilitySummary, formatAge } from './format';
import { useRevokeWorker } from './adminHooks';

const STATUS_TONE: Readonly<Record<FleetWorker['status'], BadgeTone>> = {
  online: 'success',
  stale: 'warn',
  offline: 'neutral',
};

function WorkerRow({ worker }: { readonly worker: FleetWorker }) {
  const revoke = useRevokeWorker();
  const [confirming, setConfirming] = useState(false);
  const revoked = worker.revokedAt !== null;
  const label = worker.displayName ?? worker.host ?? worker.id;

  return (
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
              must re-register with the bootstrap secret before it can lease jobs again.
            </>
          }
          onConfirm={() => {
            revoke.mutate(worker.id, { onSettled: () => { setConfirming(false); } });
          }}
          onClose={() => { setConfirming(false); }}
        />
      </td>
    </tr>
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
