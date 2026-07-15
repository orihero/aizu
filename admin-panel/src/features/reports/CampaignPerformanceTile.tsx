import { useMemo } from 'react';
import { ChevronDown, ChevronUp } from 'lucide-react';
import { Badge, type BadgeTone } from '@/shared/ui/Badge';
import { Card, CardBody, CardHeader } from '@/shared/ui/Card';
import { cn } from '@/shared/lib/cn';
import { formatMoney, formatNumber } from '@/shared/lib/formatters';
import { usePersistedQueryState } from '@/shared/hooks/usePersistedQueryState';
import type { ReportsPeriod } from '@/shared/types/domain';
import { TileEmpty } from './TileEmpty';

type PerCampaign = ReportsPeriod['perCampaign'][number];
type SortKey = 'leads' | 'cpl' | 'spend';
type SortDir = 'asc' | 'desc';

/** A null key means the natural (unsorted) order — the default. */
interface CampaignSort {
  readonly key: SortKey | null;
  readonly dir: SortDir;
}

const SORT_KEYS: readonly SortKey[] = ['leads', 'cpl', 'spend'];
const DEFAULT_SORT: CampaignSort = { key: null, dir: 'desc' };

function isSortKey(value: unknown): value is SortKey {
  return SORT_KEYS.includes(value as SortKey);
}

function isDir(value: unknown): value is SortDir {
  return value === 'asc' || value === 'desc';
}

/** "key:dir" → CampaignSort (URL form). */
function parseSort(raw: string): CampaignSort | null {
  const [key, dir] = raw.split(':');
  return isSortKey(key) && isDir(dir) ? { key, dir } : null;
}

/** localStorage shape guard. */
function validateSort(raw: unknown): CampaignSort | null {
  if (typeof raw !== 'object' || raw === null) return null;
  const o = raw as Record<string, unknown>;
  if (!isDir(o.dir)) return null;
  if (o.key === null) return { key: null, dir: o.dir };
  return isSortKey(o.key) ? { key: o.key, dir: o.dir } : null;
}

interface CampaignPerformanceTileProps {
  readonly period: ReportsPeriod;
}

const STATUS_TONES: Readonly<Record<string, BadgeTone>> = {
  active: 'success',
  live: 'success',
  paused: 'warn',
  ended: 'neutral',
  archived: 'neutral',
};

function statusTone(status: string): BadgeTone {
  return STATUS_TONES[status.toLowerCase()] ?? 'neutral';
}

/** Sort comparator that always pushes null CPL to the bottom of the list. */
function compare(a: PerCampaign, b: PerCampaign, key: SortKey, dir: SortDir): number {
  const av = a[key];
  const bv = b[key];
  if (av === null && bv === null) return 0;
  if (av === null) return 1;
  if (bv === null) return -1;
  return dir === 'asc' ? av - bv : bv - av;
}

interface SortHeaderProps {
  readonly label: string;
  readonly sortKey: SortKey;
  readonly active: SortKey | null;
  readonly dir: SortDir;
  readonly onSort: (key: SortKey) => void;
}

function SortHeader({ label, sortKey, active, dir, onSort }: SortHeaderProps) {
  const isActive = active === sortKey;
  const Icon = dir === 'asc' ? ChevronUp : ChevronDown;
  return (
    <th className="px-5 py-2.5 text-right font-semibold">
      <button
        type="button"
        onClick={() => {
          onSort(sortKey);
        }}
        className={cn(
          'ml-auto inline-flex items-center gap-1 transition-colors hover:text-text',
          isActive && 'text-text',
        )}
      >
        {label}
        <Icon className={cn('size-3', isActive ? 'opacity-100' : 'opacity-0')} aria-hidden />
      </button>
    </th>
  );
}

/** Campaign rows with sortable numeric headers (leads / cpl / spend). */
export function CampaignPerformanceTile({ period }: CampaignPerformanceTileProps) {
  const [sort, setSort] = usePersistedQueryState<CampaignSort>({
    paramKey: 'csort',
    storageKey: 'reports:campaignSort',
    defaultValue: DEFAULT_SORT,
    parse: parseSort,
    serialize: (value) => (value.key === null ? null : `${value.key}:${value.dir}`),
    validate: validateSort,
  });
  const { key: sortKey, dir: sortDir } = sort;

  const rows = useMemo(() => {
    const list = [...period.perCampaign];
    if (sortKey === null) return list;
    return list.sort((a, b) => compare(a, b, sortKey, sortDir));
  }, [period.perCampaign, sortKey, sortDir]);

  function handleSort(key: SortKey) {
    if (key === sortKey) {
      setSort({ key, dir: sortDir === 'asc' ? 'desc' : 'asc' });
      return;
    }
    setSort({ key, dir: key === 'cpl' ? 'asc' : 'desc' });
  }

  return (
    <Card className="col-span-full">
      <CardHeader
        title="Campaign performance"
        subtitle={`${period.perCampaign.length} ${period.perCampaign.length === 1 ? 'campaign' : 'campaigns'}`}
      />
      <CardBody className="px-0 py-0">
        {rows.length > 0 ? (
          <div className="overflow-x-auto">
            <table className="w-full text-left text-xs">
              <thead>
                <tr className="border-b border-border text-[10px] uppercase tracking-wider text-text-faint">
                  <th className="px-5 py-2.5 font-semibold">Campaign</th>
                  <th className="px-5 py-2.5 font-semibold">Status</th>
                  <SortHeader
                    label="Leads"
                    sortKey="leads"
                    active={sortKey}
                    dir={sortDir}
                    onSort={handleSort}
                  />
                  <SortHeader
                    label="CPL"
                    sortKey="cpl"
                    active={sortKey}
                    dir={sortDir}
                    onSort={handleSort}
                  />
                  <SortHeader
                    label="Spend"
                    sortKey="spend"
                    active={sortKey}
                    dir={sortDir}
                    onSort={handleSort}
                  />
                </tr>
              </thead>
              <tbody>
                {rows.map((campaign) => (
                  <tr key={campaign.id} className="border-b border-border/60 last:border-0">
                    <td className="px-5 py-3 font-medium">{campaign.name}</td>
                    <td className="px-5 py-3">
                      <Badge tone={statusTone(campaign.status)}>{campaign.status}</Badge>
                    </td>
                    <td className="px-5 py-3 text-right font-semibold tabular-nums">
                      {formatNumber(campaign.leads)}
                    </td>
                    <td className="px-5 py-3 text-right tabular-nums">
                      {campaign.cpl === null ? '—' : formatMoney(campaign.cpl)}
                    </td>
                    <td className="px-5 py-3 text-right tabular-nums">
                      {formatMoney(campaign.spend)}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        ) : (
          <div className="px-5 py-4">
            <TileEmpty />
          </div>
        )}
      </CardBody>
    </Card>
  );
}
