import { useState } from 'react';
import { Cpu, Server } from 'lucide-react';
import { AsyncBoundary } from '@/shared/ui/AsyncBoundary';
import { Badge } from '@/shared/ui/Badge';
import { Card, CardBody, CardHeader } from '@/shared/ui/Card';
import { ConfirmDialog } from '@/shared/ui/ConfirmDialog';
import { cn } from '@/shared/lib/cn';
import { EXECUTION_BACKENDS, type ExecutionBackend } from '@/shared/schemas/admin';
import { useExecutionBackend, useSetExecutionBackend } from './adminHooks';

const META: Readonly<Record<ExecutionBackend, {
  label: string; description: string; icon: typeof Cpu;
}>> = {
  in_process: {
    label: 'In-process (cloud)',
    description: 'Runs execute on the cloud server via the RunManager.',
    icon: Cpu,
  },
  distributed: {
    label: 'Worker fleet',
    description: 'Runs are dispatched as jobs to independent worker boxes.',
    icon: Server,
  },
};

/** The platform-wide run-routing switch (§v16) — interchange the in-process
 *  RunManager and the distributed worker fleet. Flipping it reroutes EVERY run
 *  (org 'Run' buttons included), so the switch is confirmed. */
export function ExecutionBackendCard() {
  const { data, isLoading, error, refetch } = useExecutionBackend();
  const setBackend = useSetExecutionBackend();
  const [pending, setPending] = useState<ExecutionBackend | null>(null);
  const current = data?.backend;

  return (
    <Card className="mt-6">
      <CardHeader
        title="Run execution"
        subtitle="Where every campaign run executes — org 'Run' buttons included."
      />
      <CardBody>
        <AsyncBoundary
          isLoading={isLoading}
          error={error}
          onRetry={() => {
            void refetch();
          }}
        >
          <div className="grid gap-3 sm:grid-cols-2">
            {EXECUTION_BACKENDS.map((backend) => {
              const meta = META[backend];
              const Icon = meta.icon;
              const active = backend === current;
              return (
                <button
                  key={backend}
                  type="button"
                  aria-pressed={active}
                  disabled={setBackend.isPending}
                  onClick={() => {
                    if (!active) setPending(backend);
                  }}
                  className={cn(
                    'flex flex-col gap-1.5 rounded-tile border p-4 text-left transition',
                    active
                      ? 'border-brand/50 bg-brand/5 ring-1 ring-brand/20'
                      : 'border-border bg-surface hover:border-brand/30 hover:shadow-tile',
                  )}
                >
                  <div className="flex items-center gap-2">
                    <Icon className="size-4 text-text-muted" aria-hidden />
                    <span className="font-semibold text-text">{meta.label}</span>
                    {active ? <Badge tone="success">active</Badge> : null}
                  </div>
                  <span className="text-xs text-text-muted">{meta.description}</span>
                </button>
              );
            })}
          </div>
        </AsyncBoundary>
      </CardBody>

      <ConfirmDialog
        isOpen={pending !== null}
        title="Switch run execution?"
        tone="danger"
        confirmLabel="Switch"
        isPending={setBackend.isPending}
        message={
          pending === 'distributed'
            ? "Every campaign run — including org users' Run button — will be dispatched to the worker fleet instead of running on the cloud. A run with no capable worker will fail."
            : 'Every campaign run will execute in-process on the cloud server again.'
        }
        onConfirm={() => {
          if (pending) {
            setBackend.mutate(pending, { onSettled: () => { setPending(null); } });
          }
        }}
        onClose={() => { setPending(null); }}
      />
    </Card>
  );
}
