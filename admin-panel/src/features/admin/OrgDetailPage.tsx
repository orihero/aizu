import { useState } from 'react';
import { Link, useParams } from 'react-router-dom';
import { ArrowLeft, UserCog } from 'lucide-react';
import { PageHeader } from '@/app/layout/PageHeader';
import { AsyncBoundary } from '@/shared/ui/AsyncBoundary';
import { Badge } from '@/shared/ui/Badge';
import { Button } from '@/shared/ui/Button';
import { Card, CardBody, CardHeader } from '@/shared/ui/Card';
import { EmptyState } from '@/shared/ui/EmptyState';
import { Modal } from '@/shared/ui/Modal';
import { platformLabel } from '@/shared/lib/platformLabel';
import { useAdminOrgCampaigns, useAdminOrgLeads, useStartImpersonation } from './adminHooks';
import { useAdminAuth } from './useAdminAuth';

const LEADS_PREVIEW_SIZE = 15;

function ImpersonateModal({
  orgId,
  isOpen,
  onClose,
}: {
  readonly orgId: number;
  readonly isOpen: boolean;
  readonly onClose: () => void;
}) {
  const impersonate = useStartImpersonation();
  const { refresh } = useAdminAuth();
  const [reason, setReason] = useState('');
  const [error, setError] = useState<string | null>(null);

  function submit() {
    setError(null);
    impersonate.mutate(
      { orgId, reason: reason.trim() },
      {
        onSuccess: () => {
          void refresh();
          setReason('');
          onClose();
        },
        onError: (e: unknown) => {
          setError(e instanceof Error ? e.message : 'Failed to start impersonation');
        },
      },
    );
  }

  const footer = (
    <>
      <Button variant="ghost" onClick={onClose} disabled={impersonate.isPending}>
        Cancel
      </Button>
      <Button
        variant="danger"
        onClick={submit}
        disabled={reason.trim() === '' || impersonate.isPending}
      >
        Start impersonation
      </Button>
    </>
  );

  return (
    <Modal isOpen={isOpen} onClose={onClose} title={`Impersonate org #${orgId}`} footer={footer}>
      {error ? (
        <div role="alert" className="mb-3 rounded-tile bg-danger-soft px-3 py-2 text-[13px] text-danger">
          {error}
        </div>
      ) : null}
      <p className="text-sm leading-relaxed text-text-muted">
        Every org-plane request will be served as this tenant until you end it. The reason is
        recorded in the tamper-evident audit log.
      </p>
      <label className="mt-4 block text-xs font-semibold text-text-muted" htmlFor="impersonate-reason">
        Reason (required)
      </label>
      <textarea
        id="impersonate-reason"
        rows={3}
        value={reason}
        onChange={(e) => { setReason(e.target.value); }}
        className="mt-1 w-full rounded-tile border border-border bg-bg px-3 py-2 text-sm text-text outline-none focus:border-brand"
      />
    </Modal>
  );
}

function CampaignsCard({ orgId }: { readonly orgId: number }) {
  const campaigns = useAdminOrgCampaigns(orgId);
  return (
    <Card>
      <CardHeader title="Campaigns" subtitle="Read-only." />
      <CardBody className="px-0 py-0">
        <AsyncBoundary
          isLoading={campaigns.isLoading}
          error={campaigns.error}
          onRetry={() => {
            void campaigns.refetch();
          }}
        >
          {(campaigns.data ?? []).length === 0 ? (
            <p className="px-5 py-6 text-sm text-text-muted">No campaigns.</p>
          ) : (
            <ul className="divide-y divide-border">
              {(campaigns.data ?? []).map((c) => (
                <li key={c.id} className="flex items-center gap-3 px-5 py-3">
                  <div className="min-w-0 grow">
                    <div className="truncate font-semibold text-text">
                      {c.displayName ?? c.id}
                    </div>
                    <div className="text-xs text-text-muted">{platformLabel(c.platform)}</div>
                  </div>
                  <Badge tone={c.archived ? 'neutral' : 'info'}>{c.status}</Badge>
                </li>
              ))}
            </ul>
          )}
        </AsyncBoundary>
      </CardBody>
    </Card>
  );
}

function LeadsCard({ orgId }: { readonly orgId: number }) {
  const leads = useAdminOrgLeads({ orgId, page: 1, pageSize: LEADS_PREVIEW_SIZE });
  return (
    <Card>
      <CardHeader
        title="Leads"
        subtitle={
          leads.data ? `${leads.data.total} total — showing the latest` : 'Read-only.'
        }
      />
      <CardBody className="px-0 py-0">
        <AsyncBoundary
          isLoading={leads.isLoading}
          error={leads.error}
          onRetry={() => {
            void leads.refetch();
          }}
        >
          {(leads.data?.leads ?? []).length === 0 ? (
            <p className="px-5 py-6 text-sm text-text-muted">No leads.</p>
          ) : (
            <ul className="divide-y divide-border">
              {(leads.data?.leads ?? []).map((lead) => (
                <li key={lead.commentId} className="px-5 py-3">
                  <div className="flex items-center gap-2">
                    <span className="font-semibold text-text">@{lead.username}</span>
                    <Badge tone="neutral">{platformLabel(lead.platform)}</Badge>
                    <Badge tone="info">{lead.status}</Badge>
                  </div>
                  <p className="mt-1 line-clamp-2 text-xs text-text-muted">{lead.text}</p>
                </li>
              ))}
            </ul>
          )}
        </AsyncBoundary>
      </CardBody>
    </Card>
  );
}

/** Cross-org drill-in (§5d) + reason-gated impersonation start (§5c). */
export function OrgDetailPage() {
  const { orgId: orgIdParam } = useParams();
  const orgId = Number(orgIdParam);
  const [impersonating, setImpersonating] = useState(false);

  if (!Number.isInteger(orgId) || orgId < 1) {
    return (
      <EmptyState
        icon={UserCog}
        title="Unknown organization"
        description="That org id is not valid."
        action={
          <Link to="/admin/orgs" className="text-sm font-semibold text-brand hover:underline">
            Back to organizations
          </Link>
        }
      />
    );
  }

  return (
    <>
      <Link
        to="/admin/orgs"
        className="mb-4 inline-flex items-center gap-1.5 text-[13px] font-semibold text-text-muted hover:text-text"
      >
        <ArrowLeft className="size-4" aria-hidden />
        Organizations
      </Link>
      <PageHeader
        title={`Org #${orgId}`}
        subtitle="Cross-tenant view."
        actions={
          <Button variant="danger" onClick={() => { setImpersonating(true); }}>
            <UserCog className="size-3.5" aria-hidden />
            Impersonate
          </Button>
        }
      />
      <div className="grid gap-6 lg:grid-cols-2">
        <CampaignsCard orgId={orgId} />
        <LeadsCard orgId={orgId} />
      </div>
      <ImpersonateModal
        orgId={orgId}
        isOpen={impersonating}
        onClose={() => { setImpersonating(false); }}
      />
    </>
  );
}
