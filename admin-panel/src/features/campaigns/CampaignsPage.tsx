import { Plus, Target } from 'lucide-react';
import { Link } from 'react-router-dom';
import { PageHeader } from '@/app/layout/PageHeader';
import { useCampaigns } from '@/shared/hooks/useCampaigns';
import { usePersistedQueryState } from '@/shared/hooks/usePersistedQueryState';
import { useSettings } from '@/shared/hooks/useSettings';
import { useCan } from '@/shared/hooks/useCan';
import { AsyncBoundary } from '@/shared/ui/AsyncBoundary';
import { Card, CardBody } from '@/shared/ui/Card';
import { Chip } from '@/shared/ui/Chip';
import { EmptyState } from '@/shared/ui/EmptyState';
import { formatNumber } from '@/shared/lib/formatters';
import {
  type CampaignStatusFilter,
  selectCampaignsByStatus,
  selectStatusFilterCounts,
} from '@/shared/selectors/campaigns';
import type { Campaign, RunBlock } from '@/shared/types/domain';
import { CampaignCard } from './CampaignCard';

const FILTERS: readonly { readonly key: CampaignStatusFilter; readonly label: string }[] = [
  { key: 'all', label: 'All' },
  { key: 'live', label: 'Live' },
  { key: 'paused', label: 'Paused' },
  { key: 'draft', label: 'Draft' },
  { key: 'ended', label: 'Ended' },
  { key: 'archived', label: 'Archived' },
];

const FILTER_KEYS: readonly CampaignStatusFilter[] = FILTERS.map((f) => f.key);

const NEW_CAMPAIGN_CLASS =
  'inline-flex items-center gap-1.5 rounded-full bg-accent px-3.5 py-1.5 text-xs font-bold text-accent-ink transition hover:-translate-y-px hover:shadow-lift active:scale-95';

function asCampaignFilter(raw: unknown): CampaignStatusFilter | null {
  return FILTER_KEYS.includes(raw as CampaignStatusFilter) ? (raw as CampaignStatusFilter) : null;
}

/**
 * The New-campaign action plus this org's campaign allowance (v27 plan limits).
 *
 * Rendered only for a role that can author campaigns — the same owner/admin set that
 * receives BILLING on `/api/settings`, so the quota is always answerable here and its
 * absence (a fetch still in flight) degrades to the plain button rather than to a
 * disabled one. The server's 402 is the real gate; this exists so a full plan is
 * visible BEFORE the form is filled in, instead of arriving as a silent rejection.
 *
 * `campaignCap === null` means UNLIMITED, not zero — a falsy check here would disable
 * New campaign for every paying org on Starter and above.
 */
function NewCampaignAction() {
  const { data: settings } = useSettings();
  const billing = settings?.BILLING;
  const cap = billing?.campaignCap ?? null;
  const used = billing?.campaignsUsed ?? 0;
  const capped = billing !== undefined && cap !== null && used >= cap;
  const planName = billing
    ? (billing.tiers.find((t) => t.tier === billing.tier)?.displayName ?? billing.tier)
    : null;

  return (
    <div className="flex flex-col items-end gap-1">
      <div className="flex items-center gap-3">
        {billing ? (
          <span className="text-[11.5px] font-semibold tabular-nums text-text-faint">
            {cap === null
              ? `${formatNumber(used)} campaigns · unlimited`
              : `${formatNumber(used)} of ${formatNumber(cap)} campaigns used`}
          </span>
        ) : null}
        {capped ? (
          // A disabled <span>, not a Link: the route itself still works (the form 402s on
          // submit), but offering it here would send the operator down a path we already
          // know ends in a rejection.
          <span
            aria-disabled="true"
            className={`${NEW_CAMPAIGN_CLASS} pointer-events-none cursor-not-allowed opacity-50`}
          >
            <Plus className="size-4" aria-hidden />
            New campaign
          </span>
        ) : (
          <Link to="/campaigns/new" className={NEW_CAMPAIGN_CLASS}>
            <Plus className="size-4" aria-hidden />
            New campaign
          </Link>
        )}
      </div>
      {capped ? (
        <p className="max-w-[22rem] text-right text-[11.5px] font-medium text-warn">
          {planName ?? 'Your plan'} includes {formatNumber(cap)}
          {cap === 1 ? ' campaign' : ' campaigns'}. Archive one, or{' '}
          <Link to="/settings/billing" className="font-bold text-brand hover:underline">
            upgrade your plan
          </Link>{' '}
          to add another.
        </p>
      ) : null}
    </div>
  );
}

interface CampaignsViewProps {
  readonly campaigns: readonly Campaign[];
  readonly run: RunBlock;
}

function CampaignsView({ campaigns, run }: CampaignsViewProps) {
  const [filter, setFilter] = usePersistedQueryState<CampaignStatusFilter>({
    paramKey: 'status',
    storageKey: 'campaigns:filter',
    defaultValue: 'all',
    parse: asCampaignFilter,
    serialize: (value) => (value === 'all' ? null : value),
    validate: asCampaignFilter,
  });
  const counts = selectStatusFilterCounts(campaigns);
  const visible = selectCampaignsByStatus(campaigns, filter);

  return (
    <>
      <div className="mb-5 inline-flex flex-wrap gap-2 rounded-full border border-border p-1">
        {FILTERS.map(({ key, label }) => (
          <Chip key={key} isActive={filter === key} onClick={() => { setFilter(key); }} count={counts[key]}>
            {label}
          </Chip>
        ))}
      </div>

      {visible.length === 0 ? (
        <Card>
          <CardBody>
            <EmptyState
              icon={Target}
              title="No campaigns match"
              description="Try a different status, or start a new campaign brief for the engine to run."
            />
          </CardBody>
        </Card>
      ) : (
        <div className="grid grid-cols-3 gap-4 max-xl:grid-cols-2 max-md:grid-cols-1">
          {visible.map((campaign) => (
            <CampaignCard key={campaign.id} campaign={campaign} run={run} />
          ))}
        </div>
      )}
    </>
  );
}

export function CampaignsPage() {
  const { data: state, isLoading, error, refetch } = useCampaigns();
  // Viewers can read campaigns but not author or run them — hide the write controls
  // (the server enforces this regardless; this keeps the UI honest). Gating the whole
  // action component also keeps the BILLING fetch inside it off a viewer's page, where
  // /api/settings would 403.
  const canEdit = useCan('edit_campaigns');

  return (
    <>
      <PageHeader
        title="Campaigns"
        subtitle="Briefs the engine runs every session — pause, resume, or draft a new one."
        actions={canEdit ? <NewCampaignAction /> : undefined}
      />
      <AsyncBoundary isLoading={isLoading} error={error} onRetry={() => void refetch()}>
        {state ? <CampaignsView campaigns={state.CAMPAIGNS} run={state.RUN} /> : null}
      </AsyncBoundary>
    </>
  );
}
