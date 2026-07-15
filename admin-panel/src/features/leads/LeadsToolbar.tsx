import { Search } from 'lucide-react';
import { formatNumber } from '@/shared/lib/formatters';
import { LEAD_STATUS_LABEL, LEAD_STATUS_ORDER, type LeadFilters } from '@/shared/selectors/leads';
import type { Match, MatchStatus } from '@/shared/types/domain';
import { LeadsExportMenu } from './LeadsExportMenu';

/** A campaign option for the filter dropdown ({ id, name } from the leads payload). */
export interface LeadCampaignOption {
  readonly id: string;
  readonly name: string;
}

interface LeadsToolbarProps {
  readonly filters: LeadFilters;
  readonly platforms: readonly string[];
  readonly campaigns: readonly LeadCampaignOption[];
  readonly resultCount: number;
  /** The currently-filtered leads — the basis for the export. */
  readonly filtered: readonly Match[];
  readonly onQueryChange: (query: string) => void;
  readonly onStatusChange: (status: MatchStatus | 'all') => void;
  readonly onPlatformChange: (platform: string) => void;
  readonly onCampaignChange: (campaign: string) => void;
}

const SELECT_CLASS =
  'rounded-xl border border-border bg-surface px-2.5 py-1.5 text-xs font-medium text-text-muted outline-none transition-colors focus:border-brand/50';

export function LeadsToolbar({
  filters,
  platforms,
  campaigns,
  resultCount,
  filtered,
  onQueryChange,
  onStatusChange,
  onPlatformChange,
  onCampaignChange,
}: LeadsToolbarProps) {
  return (
    <div className="flex flex-wrap items-center gap-3 border-b border-border px-5 py-3">
      <label className="flex flex-1 items-center gap-2 rounded-xl border border-border bg-surface px-3 py-1.5 transition-colors focus-within:border-brand/50">
        <Search className="size-3.5 text-text-faint" aria-hidden />
        <input
          type="search"
          value={filters.query}
          onChange={(event) => { onQueryChange(event.target.value); }}
          placeholder="Search name, comment, or details…"
          className="w-full min-w-40 bg-transparent text-xs outline-none placeholder:text-text-faint"
        />
      </label>

      <select
        value={filters.campaign}
        onChange={(event) => { onCampaignChange(event.target.value); }}
        className={SELECT_CLASS}
        aria-label="Filter by campaign"
      >
        <option value="all">All campaigns</option>
        {campaigns.map((campaign) => (
          <option key={campaign.id} value={campaign.id}>
            {campaign.name}
          </option>
        ))}
      </select>

      <select
        value={filters.platform}
        onChange={(event) => { onPlatformChange(event.target.value); }}
        className={`${SELECT_CLASS} capitalize`}
        aria-label="Filter by platform"
      >
        <option value="all">All platforms</option>
        {platforms.map((platform) => (
          <option key={platform} value={platform}>
            {platform}
          </option>
        ))}
      </select>

      <select
        value={filters.status}
        onChange={(event) => { onStatusChange(event.target.value as MatchStatus | 'all'); }}
        className={SELECT_CLASS}
        aria-label="Filter by status"
      >
        <option value="all">All statuses</option>
        {LEAD_STATUS_ORDER.map((status) => (
          <option key={status} value={status}>
            {LEAD_STATUS_LABEL[status]}
          </option>
        ))}
      </select>

      <span className="whitespace-nowrap text-xs font-semibold tabular-nums text-text-muted">
        {formatNumber(resultCount)} {resultCount === 1 ? 'result' : 'results'}
      </span>

      <LeadsExportMenu leads={filtered} variant="toolbar" />
    </div>
  );
}
