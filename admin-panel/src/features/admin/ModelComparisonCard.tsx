import { useState } from 'react';
import { FlaskConical } from 'lucide-react';
import { AsyncBoundary } from '@/shared/ui/AsyncBoundary';
import { Badge } from '@/shared/ui/Badge';
import { Card, CardBody, CardHeader } from '@/shared/ui/Card';
import { ConfirmDialog } from '@/shared/ui/ConfirmDialog';
import { cn } from '@/shared/lib/cn';
import { useModelComparisonSettings, useSetModelComparisonEnabled } from './adminHooks';

/** The platform-wide model-comparison switch (v17) — when on, every match-stage
 *  (lead) decision also fires the env-declared comparison models alongside the
 *  production model, purely for latency/cost/agreement logging. Off is today's
 *  exact single-model behaviour; on only affects IN-PROCESS runs live — a
 *  distributed worker box needs its own MODEL_COMPARISON_ENABLED env. */
export function ModelComparisonCard() {
  const { data, isLoading, error, refetch } = useModelComparisonSettings();
  const setEnabled = useSetModelComparisonEnabled();
  const [pendingConfirm, setPendingConfirm] = useState(false);
  const enabled = data?.enabled ?? false;
  const models = data?.models ?? [];

  return (
    <Card className="mt-6">
      <CardHeader
        title="Model comparison"
        subtitle="Fan a match-stage decision out to comparison models alongside production, for A/B logging."
      />
      <CardBody>
        <AsyncBoundary
          isLoading={isLoading}
          error={error}
          onRetry={() => {
            void refetch();
          }}
        >
          <div className="flex items-center justify-between gap-4 rounded-tile border border-border bg-surface p-4">
            <div className="flex items-center gap-2.5">
              <FlaskConical className="size-4 text-text-muted" aria-hidden />
              <div>
                <div className="flex items-center gap-2">
                  <span className="font-semibold text-text">Fan-out</span>
                  <Badge tone={enabled ? 'success' : 'neutral'}>
                    {enabled ? 'on' : 'off'}
                  </Badge>
                </div>
                <span className="text-xs text-text-muted">
                  {models.length > 0
                    ? `Comparison models: ${models.join(', ')}`
                    : 'No MODEL_COMPARISON_MODELS configured on this box.'}
                </span>
              </div>
            </div>
            <button
              type="button"
              role="switch"
              aria-checked={enabled}
              disabled={setEnabled.isPending || models.length === 0}
              onClick={() => { setPendingConfirm(true); }}
              title={models.length === 0 ? 'Set MODEL_COMPARISON_MODELS to enable' : undefined}
              className={cn(
                'inline-flex h-6 w-11 shrink-0 items-center rounded-full transition-colors',
                enabled ? 'bg-brand' : 'bg-surface-2',
                (setEnabled.isPending || models.length === 0) && 'opacity-50',
              )}
            >
              <span
                className={cn(
                  'size-4 rounded-full bg-on-brand shadow transition-transform',
                  enabled ? 'translate-x-6' : 'translate-x-1',
                )}
              />
            </button>
          </div>
        </AsyncBoundary>
      </CardBody>

      <ConfirmDialog
        isOpen={pendingConfirm}
        title={enabled ? 'Turn off model comparison?' : 'Turn on model comparison?'}
        tone={enabled ? 'danger' : 'default'}
        confirmLabel={enabled ? 'Turn off' : 'Turn on'}
        isPending={setEnabled.isPending}
        message={
          enabled
            ? 'In-process runs stop fanning match-stage decisions out to comparison models. Nothing already logged is deleted.'
            : `Every in-process match-stage decision will also fire ${models.join(', ')} alongside production, purely for comparison logging — the lead a run captures is unaffected.`
        }
        onConfirm={() => {
          setEnabled.mutate(!enabled, { onSettled: () => { setPendingConfirm(false); } });
        }}
        onClose={() => { setPendingConfirm(false); }}
      />
    </Card>
  );
}
