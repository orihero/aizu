import { useState } from 'react';
import { Archive, ArchiveRestore, CalendarClock, CalendarDays, Loader2, Pause, Play, Zap } from 'lucide-react';
import { Link } from 'react-router-dom';
import { Badge, type BadgeTone } from '@/shared/ui/Badge';
import { Card } from '@/shared/ui/Card';
import { ConfirmDialog } from '@/shared/ui/ConfirmDialog';
import { Sparkline } from '@/shared/ui/charts';
import { PlatformChip } from '@/features/leads/PlatformChip';
import { useArchiveCampaign, useCreateCampaign } from '@/shared/hooks/useWriteMutations';
import { useCan } from '@/shared/hooks/useCan';
import { formatMoney, formatNumber } from '@/shared/lib/formatters';
import { cn } from '@/shared/lib/cn';
import {
  CAMPAIGN_STATUS_LABEL,
  budgetPct,
  isArchived,
  isRunnable,
  isScheduled,
  isWarmEnough,
  scheduleSummary,
  selectIsCampaignRunning,
} from '@/shared/selectors/campaigns';
import type { Campaign, RunBlock } from '@/shared/types/domain';
import { RunDrawer } from './RunDrawer';
import { ScheduleDialog } from './ScheduleDialog';
import { WarmthBadge } from './WarmthBadge';

// Budget fill flips to amber once a campaign nears its cap.
const BUDGET_HOT_PCT = 90;

// Status filter is a free string on the domain type; map known states to a tone.
// `ended` has no dedicated faint Badge tone, so it shares `neutral`.
const STATUS_TONE: Readonly<Record<string, BadgeTone>> = {
  live: 'success',
  paused: 'warn',
  draft: 'neutral',
  ended: 'neutral',
};

function statusTone(status: string): BadgeTone {
  return STATUS_TONE[status] ?? 'neutral';
}

function statusLabel(status: string): string {
  return CAMPAIGN_STATUS_LABEL[status] ?? status;
}

interface CampaignCardProps {
  readonly campaign: Campaign;
  readonly run: RunBlock;
}

interface CardStatProps {
  readonly label: string;
  readonly value: string;
  readonly muted?: boolean;
}

function CardStat({ label, value, muted = false }: CardStatProps) {
  return (
    <div>
      <div className="text-[10.5px] font-bold uppercase tracking-wider text-text-faint">{label}</div>
      <div
        className={cn(
          'mt-1 font-head text-[25px] font-bold tabular-nums',
          muted ? 'text-text-faint' : 'text-text',
        )}
      >
        {value}
      </div>
    </div>
  );
}

function CardAction({ campaign, run }: CampaignCardProps) {
  const createCampaign = useCreateCampaign();
  const [confirmPause, setConfirmPause] = useState(false);
  // Don't let the lifecycle toggle race a live engine run of this same campaign.
  const isToggling = createCampaign.isPending || selectIsCampaignRunning(run, campaign.id);

  // Archived: the only forward actions are restore + read the past report.
  if (isArchived(campaign)) {
    return (
      <Link to="/reports" className="text-xs font-bold text-brand hover:underline">
        View report →
      </Link>
    );
  }

  if (campaign.status === 'live' || campaign.status === 'paused') {
    const isLive = campaign.status === 'live';
    const ToggleIcon = isLive ? Pause : Play;
    // Resume is safe and instant; pausing stops the campaign being picked up by
    // run-all / scheduled runs, so it goes through a confirm first.
    return (
      <>
        <button
          type="button"
          disabled={isToggling}
          onClick={() => {
            if (isLive) { setConfirmPause(true); return; }
            createCampaign.mutate({ campaignId: campaign.id, status: 'live' });
          }}
          aria-label={isLive ? 'Pause campaign' : 'Resume campaign'}
          className="grid size-8 place-items-center rounded-full text-text-faint transition hover:bg-surface-2 hover:text-text active:scale-95 disabled:cursor-not-allowed disabled:opacity-50"
        >
          <ToggleIcon className="size-4" aria-hidden />
        </button>
        <ConfirmDialog
          isOpen={confirmPause}
          title="Pause campaign?"
          message={
            <>
              Pause <b>{campaign.name}</b>? It stays in place but won&rsquo;t be picked up by
              run-all or scheduled runs until you resume it.
            </>
          }
          confirmLabel="Pause"
          isPending={createCampaign.isPending}
          onConfirm={() => {
            createCampaign.mutate(
              { campaignId: campaign.id, status: 'paused' },
              { onSuccess: () => { setConfirmPause(false); } },
            );
          }}
          onClose={() => { setConfirmPause(false); }}
        />
      </>
    );
  }

  if (campaign.status === 'draft') {
    // A draft with a brief can go live (Activate reuses the same status write as
    // Play/Pause); without a brief there's nothing the engine can run, so we only
    // offer Edit until one is added.
    const canActivate = isRunnable(campaign);
    return (
      <div className="flex items-center gap-3">
        {canActivate ? (
          <button
            type="button"
            disabled={isToggling}
            onClick={() => { createCampaign.mutate({ campaignId: campaign.id, status: 'live' }); }}
            className="text-xs font-bold text-brand transition hover:underline disabled:cursor-not-allowed disabled:opacity-50"
          >
            Activate
          </button>
        ) : null}
        <Link
          to={`/campaigns/${campaign.id}/edit`}
          className={cn(
            'text-xs font-bold transition hover:underline',
            canActivate ? 'text-text-faint hover:text-text' : 'text-brand',
          )}
        >
          Edit draft
        </Link>
      </div>
    );
  }

  return (
    <Link to="/reports" className="text-xs font-bold text-brand hover:underline">
      View report →
    </Link>
  );
}

/**
 * Per-card Run affordance: opens the RunDrawer to launch a live, time-boxed run.
 * Shown only for a runnable campaign to a user whose role permits running (the
 * bridge enforces `run_campaigns` regardless; this keeps the UI honest). Reflects
 * an in-flight run of this campaign as "Running…".
 */
function RunButton({ campaign, run }: CampaignCardProps) {
  const canTrigger = useCan('run_campaigns');
  const [isOpen, setIsOpen] = useState(false);

  if (!isRunnable(campaign) || !canTrigger) return null;

  const isRunning = selectIsCampaignRunning(run, campaign.id);
  // Hard-block harvest until the backing account is warm enough (warming PRD §7.5,
  // O2). The disabled pill explains why and deep-links the operator to fix it.
  const warmEnough = isWarmEnough(campaign);
  if (!warmEnough) {
    return (
      <div className="mt-3 flex justify-end border-t border-border pt-3">
        <span
          className="inline-flex cursor-not-allowed items-center gap-1.5 rounded-full bg-surface-2 px-3 py-1.5 text-xs font-bold text-text-faint"
          title={`Account warmth is ${campaign.warmth.score}%, below the ${campaign.warmth.gateMin}% threshold (${campaign.warmth.state}). Check Settings → Integrations.`}
        >
          ❄ Warming — not ready
        </span>
      </div>
    );
  }
  return (
    <div className="mt-3 flex justify-end border-t border-border pt-3">
      <button
        type="button"
        onClick={() => { setIsOpen(true); }}
        className="inline-flex items-center gap-1.5 rounded-full bg-surface-2 px-3 py-1.5 text-xs font-bold text-text transition hover:bg-border active:scale-95"
      >
        {isRunning
          ? <Loader2 className="size-3.5 animate-spin" aria-hidden />
          : <Zap className="size-3.5" aria-hidden />}
        {isRunning ? 'Running…' : 'Run'}
      </button>
      <RunDrawer
        campaign={campaign}
        run={run}
        isOpen={isOpen}
        onClose={() => { setIsOpen(false); }}
      />
    </div>
  );
}

/**
 * Archive / un-archive toggle (icon button). Shown only to a user who can edit
 * campaigns. Archiving a live campaign also stops its run server-side; we don't
 * let the toggle race an in-flight run of this same campaign.
 */
function ArchiveButton({ campaign, run }: CampaignCardProps) {
  const canEdit = useCan('edit_campaigns');
  const archiveCampaign = useArchiveCampaign();
  const [confirmArchive, setConfirmArchive] = useState(false);
  if (!canEdit) return null;

  const archived = isArchived(campaign);
  const isBusy = archiveCampaign.isPending || selectIsCampaignRunning(run, campaign.id);
  const Icon = archived ? ArchiveRestore : Archive;
  // Un-archive is a safe restore; archiving hides the campaign and stops any run
  // in progress, so it goes through a confirm first.
  return (
    <>
      <button
        type="button"
        disabled={isBusy}
        onClick={() => {
          if (archived) { archiveCampaign.mutate({ campaignId: campaign.id, archived: false }); return; }
          setConfirmArchive(true);
        }}
        aria-label={archived ? 'Unarchive campaign' : 'Archive campaign'}
        title={archived ? 'Unarchive campaign' : 'Archive campaign'}
        className="grid size-8 place-items-center rounded-full text-text-faint transition hover:bg-surface-2 hover:text-text active:scale-95 disabled:cursor-not-allowed disabled:opacity-50"
      >
        <Icon className="size-4" aria-hidden />
      </button>
      <ConfirmDialog
        isOpen={confirmArchive}
        title="Archive campaign?"
        message={
          <>
            Archive <b>{campaign.name}</b>? It&rsquo;s hidden from the active list and any run in
            progress is stopped. Nothing is deleted — you can unarchive it later.
          </>
        }
        confirmLabel="Archive"
        tone="danger"
        isPending={archiveCampaign.isPending}
        onConfirm={() => {
          archiveCampaign.mutate(
            { campaignId: campaign.id, archived: true },
            { onSuccess: () => { setConfirmArchive(false); } },
          );
        }}
        onClose={() => { setConfirmArchive(false); }}
      />
    </>
  );
}

/**
 * Schedule toggle (icon button) opening the ScheduleDialog. Shown only to a user
 * who can edit campaigns, and only for non-archived campaigns (an archived campaign
 * is runnable by no path, so scheduling it is meaningless).
 */
function ScheduleButton({ campaign }: { readonly campaign: Campaign }) {
  const canEdit = useCan('edit_campaigns');
  const [isOpen, setIsOpen] = useState(false);
  if (!canEdit || isArchived(campaign)) return null;

  const scheduled = isScheduled(campaign);
  return (
    <>
      <button
        type="button"
        onClick={() => { setIsOpen(true); }}
        aria-label={scheduled ? 'Edit schedule' : 'Schedule campaign'}
        title={scheduled ? 'Edit schedule' : 'Schedule campaign'}
        className={cn(
          'grid size-8 place-items-center rounded-full transition hover:bg-surface-2 active:scale-95',
          scheduled ? 'text-brand' : 'text-text-faint hover:text-text',
        )}
      >
        <CalendarClock className="size-4" aria-hidden />
      </button>
      <ScheduleDialog campaign={campaign} isOpen={isOpen} onClose={() => { setIsOpen(false); }} />
    </>
  );
}

export function CampaignCard({ campaign, run }: CampaignCardProps) {
  const pct = budgetPct(campaign);
  const isHot = pct >= BUDGET_HOT_PCT;
  const isDraft = campaign.status === 'draft';
  const archived = isArchived(campaign);
  const cadence = scheduleSummary(campaign);
  // E.7: `spent` and `leads` share this card and fail in OPPOSITE directions — a run
  // that dead-letters banks its spend to the cloud but strands its leads on the worker,
  // so the card reads "$X spent, 0 leads" with nothing to explain it. The `delivery`
  // word is read straight off the payload rather than re-derived from the two numbers,
  // so this card and the run drawer can never disagree about what the gap means. Both
  // counts fall back to `leads` when a pre-v27 bridge omits them — i.e. to exactly what
  // this card used to show.
  const leadsFound = campaign.leadsFound ?? campaign.leads;
  const leadsDelivered = campaign.leadsDelivered ?? campaign.leads;
  const notDelivered = !isDraft && campaign.delivery === 'not_delivered';

  return (
    <Card className="flex flex-col p-5">
      <div className="flex items-start justify-between gap-2">
        <Link
          to={`/campaigns/${campaign.id}/edit`}
          className="min-w-0 truncate pt-0.5 font-head text-[16.5px] font-bold tracking-tight hover:text-brand hover:underline"
          title={`Edit ${campaign.name}`}
        >
          {campaign.name}
        </Link>
        {archived ? (
          <Badge tone="neutral">Archived</Badge>
        ) : (
          <Badge tone={statusTone(campaign.status)}>{statusLabel(campaign.status)}</Badge>
        )}
      </div>

      {/* C6: one chip per platform the campaign fans out to. `platforms` is
          undefined on older payloads → fall back to the single primary platform. */}
      <div className="mt-3 flex flex-wrap gap-1.5">
        {(campaign.platforms ?? [campaign.platform]).map((p) => (
          <PlatformChip key={p} platform={p} />
        ))}
      </div>

      {isDraft ? null : (
        <div className="mt-3">
          <WarmthBadge warmth={campaign.warmth} />
        </div>
      )}

      {cadence ? (
        <div className="mt-3">
          <Badge tone="info">
            <CalendarClock className="mr-1 inline size-3 align-[-1px]" aria-hidden />
            {cadence}
          </Badge>
        </div>
      ) : null}

      <div className="mt-4 flex gap-8">
        {/* The Leads stat is what actually REACHED the account — the leads an operator
            can open. CPL stays as the server sent it: it is guarded on WON leads, so it
            reads "—" on every untriaged campaign, healthy or not, and synthesising one
            from `leadsFound` would price leads the customer cannot open. */}
        <CardStat label="Leads" value={isDraft ? '—' : formatNumber(leadsDelivered)} muted={isDraft} />
        <CardStat
          label="CPL"
          value={campaign.cpl == null ? '—' : formatMoney(campaign.cpl)}
          muted={campaign.cpl == null}
        />
      </div>

      {/* Showing the delivered count alone would deny work that happened; showing the
          found count alone would imply leads that aren't there. Both, or neither. */}
      {notDelivered ? (
        <div className="mt-2">
          <Badge
            tone="warn"
            title={`This campaign discovered ${formatNumber(leadsFound)} leads, but ${formatNumber(leadsDelivered)} reached your account — a run ended before it could hand them over.`}
          >
            {formatNumber(leadsFound)} found · not delivered
          </Badge>
        </div>
      ) : null}

      <div className="mt-4">
        <div className="h-1.5 overflow-hidden rounded-full bg-surface-2">
          <div
            className={cn('h-full rounded-full transition-all', isHot ? 'bg-warn' : 'bg-brand')}
            style={{ width: `${pct}%` }}
          />
        </div>
        <div className="mt-1.5 flex justify-between text-[11.5px] font-semibold tabular-nums text-text-faint">
          <span>
            <b className="font-bold text-text-muted">{formatMoney(campaign.spent)}</b> / {formatMoney(campaign.budgetCap)}
          </span>
          <span>{pct}%</span>
        </div>
        {/* The spend is never hidden or zeroed — it was really incurred and the
            accounting is correct. The label is what carries the caveat. */}
        {notDelivered ? (
          <p className="mt-1 text-[11px] font-medium text-warn">
            Includes spend on a run that didn’t deliver its leads.
          </p>
        ) : null}
      </div>

      <div className="mt-3">
        <Sparkline data={campaign.spark} />
      </div>

      <div className="mt-3 flex items-center justify-between gap-2 border-t border-border pt-3">
        <span className="inline-flex items-center gap-1 text-[11.5px] font-medium text-text-faint">
          <CalendarDays className="size-3" aria-hidden />
          {campaign.startedAt === '—' ? 'Not started' : `Started ${campaign.startedAt}`}
        </span>
        <div className="flex items-center gap-1.5">
          <CardAction campaign={campaign} run={run} />
          <ScheduleButton campaign={campaign} />
          <ArchiveButton campaign={campaign} run={run} />
        </div>
      </div>
      <RunButton campaign={campaign} run={run} />
    </Card>
  );
}
