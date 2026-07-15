import { ArrowLeft, Target } from 'lucide-react';
import { Link, useNavigate, useParams } from 'react-router-dom';
import { PageHeader } from '@/app/layout/PageHeader';
import { useCampaigns } from '@/shared/hooks/useCampaigns';
import { useCreateCampaign } from '@/shared/hooks/useWriteMutations';
import { AsyncBoundary } from '@/shared/ui/AsyncBoundary';
import { Card, CardBody } from '@/shared/ui/Card';
import { EmptyState } from '@/shared/ui/EmptyState';
import type { Campaign } from '@/shared/types/domain';
import { CampaignForm } from './CampaignForm';
import { CampaignRunHistory } from './CampaignRunHistory';
import { useCampaignForm } from './useCampaignForm';

const EDIT_NOTE =
  'Edits save this campaign’s brief and ops fields to the engine database, and the live run reads them directly — not from any .md file. The engine id is fixed.';

const BACK_LINK_CLASS =
  'inline-flex items-center gap-1.5 rounded-full border border-border bg-surface px-3.5 py-1.5 text-xs font-bold text-text shadow-tile transition hover:-translate-y-px hover:shadow-lift active:scale-95';

const DEFAULT_GOAL_TARGET = 200;

/** Inner form — mounts only once the campaign is loaded, so the form hook seeds
 *  from real values (it reads its initial state once). */
function EditForm({ campaign }: { readonly campaign: Campaign }) {
  const navigate = useNavigate();
  const upsert = useCreateCampaign();   // POST /api/campaign is an upsert (create + edit)
  const brief = campaign.briefForm;
  const api = useCampaignForm({
    campaignId: campaign.id,
    name: campaign.name,
    objective: brief?.goal || campaign.goalType || 'lead',
    budgetCap: campaign.budgetCap,
    goalTarget: campaign.goalTarget ?? DEFAULT_GOAL_TARGET,
    status: campaign.status,
    // Brief fields — arrays join to comma strings for the text inputs.
    platform: brief?.platform ?? campaign.platform,
    threshold: brief?.threshold ?? campaign.threshold,
    languages: (brief?.languageMix ?? campaign.languages).join(', '),
    relevanceDef: brief?.relevanceDef ?? '',
    matchDef: brief?.matchDef ?? '',
    extractDef: brief?.extractDef ?? '',
    relevancePrompt: brief?.relevancePrompt ?? '',
    matchPrompt: brief?.matchPrompt ?? '',
    visionPrompt: brief?.visionPrompt ?? '',
    seedHashtags: (brief?.seedHashtags ?? []).join(', '),
    seedAccounts: (brief?.seedAccounts ?? []).join(', '),
    seedChannels: (brief?.seedChannels ?? []).join(', '),
    // Multi-platform fan-out: convert the wire channels (seed ARRAYS) into the
    // form's ChannelFormEntry (comma strings). Absent → [] (single-platform), so a
    // multi-channel brief survives the edit round-trip instead of collapsing to [].
    channels: (brief?.channels ?? []).map((ch) => ({
      platform: ch.platform,
      seedHashtags: ch.seedHashtags.join(', '),
      seedAccounts: ch.seedAccounts.join(', '),
      seedChannels: ch.seedChannels.join(', '),
    })),
  });

  return (
    <CampaignForm
      api={api}
      onSubmit={() => {
        upsert.mutate(api.toInput(), { onSuccess: () => { void navigate('/campaigns'); } });
      }}
      isPending={upsert.isPending}
      isError={upsert.isError}
      errorMessage={upsert.error instanceof Error ? upsert.error.message : undefined}
      submitLabel="Save changes"
      note={EDIT_NOTE}
    />
  );
}

export function CampaignEditPage() {
  const { campaignId } = useParams();
  // /api/campaigns is org-wide; pick this campaign's card and filter the pooled
  // SESSIONS (the recent-runs log shown alongside) to the campaign in the URL.
  const { data: state, isLoading, error, refetch } = useCampaigns();
  const campaign = state?.CAMPAIGNS.find((c) => c.id === campaignId);
  const sessions = state?.SESSIONS.filter((s) => s.campaignId === campaignId) ?? [];

  return (
    <>
      <PageHeader
        title="Edit campaign"
        subtitle={campaign ? campaign.name : 'Update a campaign’s budget and goal.'}
        actions={
          <Link to="/campaigns" className={BACK_LINK_CLASS}>
            <ArrowLeft className="size-4" aria-hidden />
            Back to campaigns
          </Link>
        }
      />
      <AsyncBoundary isLoading={isLoading} error={error} onRetry={() => void refetch()}>
        {state ? (
          campaign ? (
            <div className="grid grid-cols-1 items-start gap-6 xl:grid-cols-[minmax(0,42rem)_minmax(0,1fr)]">
              <EditForm campaign={campaign} />
              <CampaignRunHistory sessions={sessions} />
            </div>
          ) : (
            <Card>
              <CardBody>
                <EmptyState
                  icon={Target}
                  title="Campaign not found"
                  description="It may have been removed. Head back to the campaigns list."
                />
              </CardBody>
            </Card>
          )
        ) : null}
      </AsyncBoundary>
    </>
  );
}
