import { useState } from 'react';
import { Plus } from 'lucide-react';
import { PageHeader } from '@/app/layout/PageHeader';
import { AsyncBoundary } from '@/shared/ui/AsyncBoundary';
import { Button } from '@/shared/ui/Button';
import { Card } from '@/shared/ui/Card';
import { useControlFlags, useFleet } from './adminHooks';
import { WorkerTable } from './WorkerTable';
import { ControlFlagsCard } from './ControlFlagsCard';
import { EnqueueJobModal } from './EnqueueJobModal';
import { ExecutionBackendCard } from './ExecutionBackendCard';

/** The fleet console — worker presence + lifecycle controls (§5e). */
export function FleetPage() {
  const fleet = useFleet();
  const flags = useControlFlags();
  const [enqueuing, setEnqueuing] = useState(false);

  const online = fleet.data?.filter((w) => w.status === 'online').length ?? 0;
  const total = fleet.data?.length ?? 0;

  return (
    <>
      <PageHeader
        title="Fleet"
        subtitle={total > 0 ? `${online} of ${total} workers online` : 'Worker fleet'}
        actions={
          <Button onClick={() => { setEnqueuing(true); }}>
            <Plus className="size-3.5" aria-hidden />
            Enqueue job
          </Button>
        }
      />
      <ExecutionBackendCard />
      <Card>
        <AsyncBoundary
          isLoading={fleet.isLoading}
          error={fleet.error}
          onRetry={() => {
            void fleet.refetch();
          }}
        >
          <WorkerTable workers={fleet.data ?? []} />
        </AsyncBoundary>
      </Card>

      <AsyncBoundary
        isLoading={flags.isLoading}
        error={flags.error}
        onRetry={() => {
          void flags.refetch();
        }}
      >
        <ControlFlagsCard flags={flags.data ?? []} />
      </AsyncBoundary>

      <EnqueueJobModal isOpen={enqueuing} onClose={() => { setEnqueuing(false); }} />
    </>
  );
}
