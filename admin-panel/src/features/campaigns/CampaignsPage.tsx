import { Plus, Target } from 'lucide-react';
import { Link } from 'react-router-dom';
import { PageHeader } from '@/app/layout/PageHeader';
import { useCampaigns } from '@/shared/hooks/useCampaigns';
import { usePersistedQueryState } from '@/shared/hooks/usePersistedQueryState';
import { useCan } from '@/shared/hooks/useCan';
import { AsyncBoundary } from '@/shared/ui/AsyncBoundary';
import { Card, CardBody } from '@/shared/ui/Card';
import { Chip } from '@/shared/ui/Chip';
import { EmptyState } from '@/shared/ui/EmptyState';
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

function asCampaignFilter(raw: unknown): CampaignStatusFilter | null {
  return FILTER_KEYS.includes(raw as CampaignStatusFilter) ? (raw as CampaignStatusFilter) : null;
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
  // (the server enforces this regardless; this keeps the UI honest).
  const canEdit = useCan('edit_campaigns');

  return (
    <>
      <PageHeader
        title="Campaigns"
        subtitle="Briefs the engine runs every session — pause, resume, or draft a new one."
        actions={
          canEdit ? (
            <Link
              to="/campaigns/new"
              className="inline-flex items-center gap-1.5 rounded-full bg-accent px-3.5 py-1.5 text-xs font-bold text-accent-ink transition hover:-translate-y-px hover:shadow-lift active:scale-95"
            >
              <Plus className="size-4" aria-hidden />
              New campaign
            </Link>
          ) : undefined
        }
      />
      <AsyncBoundary isLoading={isLoading} error={error} onRetry={() => void refetch()}>
        {state ? <CampaignsView campaigns={state.CAMPAIGNS} run={state.RUN} /> : null}
      </AsyncBoundary>
    </>
  );
}
