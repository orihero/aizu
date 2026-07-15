import { useState } from 'react';
import { ScrollText, ShieldCheck, ShieldAlert } from 'lucide-react';
import { PageHeader } from '@/app/layout/PageHeader';
import { AsyncBoundary } from '@/shared/ui/AsyncBoundary';
import { Badge } from '@/shared/ui/Badge';
import { Button } from '@/shared/ui/Button';
import { Card, CardBody } from '@/shared/ui/Card';
import { EmptyState } from '@/shared/ui/EmptyState';
import { usePanelRepository } from '@/shared/api/repositoryContext';
import type { AuditVerify } from '@/shared/schemas/admin';
import { formatTimestamp } from './format';
import { useAudit } from './adminHooks';

/** A login-failure action reads as a warning; everything else is informational. */
function actionTone(action: string): 'warn' | 'neutral' {
  return action.endsWith('.failed') ? 'warn' : 'neutral';
}

function VerifyBanner({ result }: { readonly result: AuditVerify }) {
  if (result.ok) {
    return (
      <div className="mb-4 flex items-center gap-2 rounded-tile bg-success-soft px-4 py-2.5 text-[13px] font-semibold text-success">
        <ShieldCheck className="size-4" aria-hidden />
        Chain intact — {result.count} entries verified.
      </div>
    );
  }
  return (
    <div
      role="alert"
      className="mb-4 flex items-center gap-2 rounded-tile bg-danger-soft px-4 py-2.5 text-[13px] font-semibold text-danger"
    >
      <ShieldAlert className="size-4" aria-hidden />
      Tamper detected — first bad row: #{result.firstBadId ?? '?'} (of {result.count} verified).
    </div>
  );
}

/** The hash-chained admin audit log (§5c) + on-demand chain verification. */
export function AuditPage() {
  const audit = useAudit();
  const repository = usePanelRepository();
  const [verify, setVerify] = useState<AuditVerify | null>(null);
  const [verifying, setVerifying] = useState(false);

  async function onVerify() {
    setVerifying(true);
    const result = await repository.verifyAudit();
    setVerifying(false);
    if (result.ok) setVerify(result.value);
  }

  return (
    <>
      <PageHeader
        title="Audit log"
        subtitle="Append-only, SHA-256 hash-chained. Every admin action, newest first."
        actions={
          <Button
            variant="ghost"
            disabled={verifying}
            onClick={() => {
              void onVerify();
            }}
          >
            <ShieldCheck className="size-3.5" aria-hidden />
            {verifying ? 'Verifying…' : 'Verify chain'}
          </Button>
        }
      />
      {verify ? <VerifyBanner result={verify} /> : null}
      <Card>
        <CardBody className="px-0 py-0">
          <AsyncBoundary
            isLoading={audit.isLoading}
            error={audit.error}
            onRetry={() => {
              void audit.refetch();
            }}
          >
            {(audit.data ?? []).length === 0 ? (
              <EmptyState icon={ScrollText} title="No audit entries yet" />
            ) : (
              <div className="overflow-x-auto">
                <table className="w-full min-w-[760px] text-left text-[13px]">
                  <thead>
                    <tr className="border-b border-border text-xs font-semibold uppercase tracking-wide text-text-faint">
                      <th className="px-4 py-2.5">When</th>
                      <th className="px-4 py-2.5">Action</th>
                      <th className="px-4 py-2.5">Target</th>
                      <th className="px-4 py-2.5">IP</th>
                      <th className="px-4 py-2.5">Reason</th>
                    </tr>
                  </thead>
                  <tbody>
                    {(audit.data ?? []).map((entry) => (
                      <tr key={entry.id} className="border-b border-border last:border-0">
                        <td className="whitespace-nowrap px-4 py-2.5 text-xs text-text-muted">
                          {formatTimestamp(entry.at)}
                        </td>
                        <td className="px-4 py-2.5">
                          <Badge tone={actionTone(entry.action)}>{entry.action}</Badge>
                        </td>
                        <td className="px-4 py-2.5 text-text-muted">
                          {entry.targetOrgId !== null ? `org #${entry.targetOrgId}` : ''}
                          {entry.targetUserId !== null ? ` user #${entry.targetUserId}` : ''}
                          {entry.targetResource ?? ''}
                          {entry.targetOrgId === null
                          && entry.targetUserId === null
                          && !entry.targetResource
                            ? '—'
                            : ''}
                        </td>
                        <td className="px-4 py-2.5 text-xs text-text-muted">{entry.ip}</td>
                        <td className="px-4 py-2.5 text-text-muted">{entry.reason ?? '—'}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            )}
          </AsyncBoundary>
        </CardBody>
      </Card>
    </>
  );
}
