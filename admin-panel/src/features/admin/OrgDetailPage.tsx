import { useState } from 'react';
import { Link, useParams } from 'react-router-dom';
import { ArrowLeft, EyeOff, UserCog } from 'lucide-react';
import { PageHeader } from '@/app/layout/PageHeader';
import { AsyncBoundary } from '@/shared/ui/AsyncBoundary';
import { Badge } from '@/shared/ui/Badge';
import { Button } from '@/shared/ui/Button';
import { Card, CardBody, CardHeader } from '@/shared/ui/Card';
import { EmptyState } from '@/shared/ui/EmptyState';
import { Modal } from '@/shared/ui/Modal';
import { cn } from '@/shared/lib/cn';
import { leadUidOf } from '@/shared/lib/leadId';
import { platformLabel } from '@/shared/lib/platformLabel';
import type { AdminOrgRun } from '@/shared/schemas/admin';
import { AdminRunLog } from './AdminRunLog';
import { formatRunDuration, formatTimestamp, runStatusTone } from './format';
import {
  useAdminOrgCampaigns,
  useAdminOrgLeads,
  useAdminOrgRuns,
  useStartImpersonation,
} from './adminHooks';
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

/**
 * That org's recent runs, newest first — the picker for the narrative feed.
 *
 * A run row is clickable in full (a `<button>`, so keyboard and screen readers get the
 * same affordance as the mouse); the selected run's log renders below the grid, where a
 * 500-row feed has the width to be legible.
 */
function RunsCard({
  orgId,
  selectedRunId,
  onSelect,
}: {
  readonly orgId: number;
  readonly selectedRunId: string | null;
  readonly onSelect: (run: AdminOrgRun) => void;
}) {
  const runs = useAdminOrgRuns(orgId);
  return (
    <Card>
      <CardHeader
        title="Runs"
        subtitle="Newest first. Open one for its full event log."
      />
      <CardBody className="px-0 py-0">
        <AsyncBoundary
          isLoading={runs.isLoading}
          error={runs.error}
          onRetry={() => {
            void runs.refetch();
          }}
        >
          {(runs.data ?? []).length === 0 ? (
            <p className="px-5 py-6 text-sm text-text-muted">No runs.</p>
          ) : (
            <ul className="divide-y divide-border">
              {(runs.data ?? []).map((run) => (
                <li key={run.runId}>
                  <button
                    type="button"
                    onClick={() => { onSelect(run); }}
                    aria-pressed={run.runId === selectedRunId}
                    className={cn(
                      'flex w-full items-center gap-3 px-5 py-3 text-left transition hover:bg-surface-2',
                      run.runId === selectedRunId && 'bg-surface-2',
                    )}
                  >
                    <div className="min-w-0 grow">
                      <div className="flex items-center gap-2">
                        <span className="truncate font-semibold text-text">
                          {run.campaignName}
                        </span>
                        {/* `mode` is honestly null for a run this bridge process no longer
                            remembers — say so rather than guessing "live". */}
                        <Badge tone="neutral">{run.mode ?? 'mode unknown'}</Badge>
                      </div>
                      <div className="mt-0.5 truncate text-xs text-text-muted">
                        {run.platforms.length > 0
                          ? run.platforms.map(platformLabel).join(', ')
                          : 'no platform recorded'}
                        {' · '}
                        {formatTimestamp(run.startedAt)}
                        {' · '}
                        {formatRunDuration(run.startedAt, run.finishedAt)}
                      </div>
                      <div className="mt-0.5 font-mono text-[10px] text-text-faint">
                        {run.runId}
                      </div>
                    </div>
                    <div className="shrink-0 text-right">
                      <Badge tone={runStatusTone(run.status)}>{run.status}</Badge>
                      <div className="mt-1 text-xs tabular-nums text-text-muted">
                        {run.leads} leads · {run.sessions} sessions
                      </div>
                    </div>
                  </button>
                </li>
              ))}
            </ul>
          )}
        </AsyncBoundary>
      </CardBody>
    </Card>
  );
}

/**
 * The org's leads, UN-REDACTED — the one surface in the product that still shows a lead's
 * real handle and the words they wrote.
 *
 * v27 stripped `username` and `text` from every org-facing payload and gave customers the
 * derived `intent` instead; here all three sit side by side, because the pairing is how an
 * operator checks that the redaction is summarising honestly rather than dropping or
 * inventing what a lead asked for. It is a full-width table for exactly that reason: two
 * prose columns that have to be read against each other.
 *
 * The gate is the admin plane itself (IP allowlist + platform-admin session), which is
 * already in front of this route — the banner is there so nobody reads a screenshot of it
 * as a customer view.
 */
function LeadsCard({ orgId }: { readonly orgId: number }) {
  const leads = useAdminOrgLeads({ orgId, page: 1, pageSize: LEADS_PREVIEW_SIZE });
  return (
    <Card className="mt-6">
      <CardHeader
        title={
          <>
            <span>Leads</span>
            <Badge tone="danger">
              <EyeOff className="size-3" aria-hidden />
              un-redacted
            </Badge>
          </>
        }
        subtitle={
          leads.data
            ? `${leads.data.total} total — showing the latest. Handle and comment text are hidden from the customer's own panel.`
            : "Handle and comment text are hidden from the customer's own panel."
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
            <div className="overflow-x-auto">
              <table className="w-full min-w-[860px] text-left text-[13px]">
                <thead>
                  <tr className="border-b border-border text-xs font-semibold uppercase tracking-wide text-text-faint">
                    <th className="px-5 py-2.5">Lead</th>
                    <th className="px-5 py-2.5">Comment (raw)</th>
                    <th className="px-5 py-2.5">Intent (what the customer sees)</th>
                    <th className="px-5 py-2.5">Status</th>
                  </tr>
                </thead>
                <tbody>
                  {/* Composite key — one org's leads span campaigns and platforms, which
                      can legitimately share a commentId. Note this is the SUPERADMIN
                      payload, so `commentId` here is still the real platform comment id
                      (the v28 opaque token is a customer-plane projection only) and the
                      collision is as live as it ever was. */}
                  {(leads.data?.leads ?? []).map((lead) => (
                    <tr key={leadUidOf(lead)} className="border-b border-border align-top last:border-0">
                      <td className="px-5 py-3">
                        <div className="font-semibold text-text">@{lead.username}</div>
                        <div className="mt-0.5 text-xs text-text-muted">
                          {platformLabel(lead.platform)}
                        </div>
                        <div className="mt-0.5 text-xs text-text-faint">
                          {formatTimestamp(lead.capturedAt)}
                        </div>
                      </td>
                      <td className="max-w-[24rem] px-5 py-3 text-text-muted">{lead.text}</td>
                      <td className="max-w-[24rem] px-5 py-3">
                        {/* '' is a real value: a pre-v27 row captured before intent existed.
                            Say so — never fall back to the raw text, which is the whole
                            thing the customer-facing column is supposed to replace. */}
                        {lead.intent === '' ? (
                          <span className="text-text-faint">Intent not captured</span>
                        ) : (
                          <span className="text-text">{lead.intent}</span>
                        )}
                      </td>
                      <td className="px-5 py-3">
                        <Badge tone="info">{lead.status}</Badge>
                        {lead.score !== null ? (
                          <div className="mt-1 text-xs tabular-nums text-text-muted">
                            score {lead.score}
                          </div>
                        ) : null}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
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
  // The whole picker row is held, not just its id: nothing in the activity feed itself
  // carries a campaign name or a start time, so the log's metadata strip has no other
  // source for them.
  const [selectedRun, setSelectedRun] = useState<AdminOrgRun | null>(null);

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
        <RunsCard
          orgId={orgId}
          selectedRunId={selectedRun?.runId ?? null}
          onSelect={setSelectedRun}
        />
      </div>
      {selectedRun ? (
        // Keyed by run id so switching runs remounts the log: the accumulator and its
        // cursor start clean rather than paging one run's feed from another's cursor.
        <AdminRunLog
          key={selectedRun.runId}
          runId={selectedRun.runId}
          run={selectedRun}
          onClose={() => { setSelectedRun(null); }}
        />
      ) : null}
      <LeadsCard orgId={orgId} />
      <ImpersonateModal
        orgId={orgId}
        isOpen={impersonating}
        onClose={() => { setImpersonating(false); }}
      />
    </>
  );
}
