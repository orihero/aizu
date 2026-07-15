import { useCallback, useMemo, useState } from 'react';
import { useNavigate, useParams } from 'react-router-dom';
import { ChevronLeft, ChevronRight, Columns3, Table2 } from 'lucide-react';
import { PageHeader } from '@/app/layout/PageHeader';
import { AsyncBoundary } from '@/shared/ui/AsyncBoundary';
import { Card } from '@/shared/ui/Card';
import { useLeads } from '@/shared/hooks/useLeads';
import { useSetMatchStatus } from '@/shared/hooks/useSetMatchStatus';
import { cn } from '@/shared/lib/cn';
import { formatNumber, formatPercent } from '@/shared/lib/formatters';
import { pageCount, type LeadSortKey } from '@/shared/selectors/leads';
import type { LeadsQuery, Match } from '@/shared/types/domain';
import { LeadBoard } from './board/LeadBoard';
import { ReasonMoveModal, type PendingMove } from './board/ReasonMoveModal';
import { LeadDrawer } from './LeadDrawer';
import { LeadStatTile } from './LeadStatTile';
import { LeadsBulkBar } from './LeadsBulkBar';
import { LeadsTable } from './LeadsTable';
import { LeadsToolbar } from './LeadsToolbar';
import { useLeadFilters } from './useLeadFilters';

const PER_PAGE = 12;
// Board view shows the whole list grouped into columns; request the server's max page
// (LEADS_PAGE_SIZE_MAX) so the board is complete up to that cap.
const BOARD_PAGE_SIZE = 200;

type LeadsView = 'table' | 'board';

/** Map the table's sort column to the server's lead-sort vocabulary. */
function toServerSort(key: LeadSortKey): NonNullable<LeadsQuery['sort']> {
  return key === 'captured' ? 'capturedAt' : key;
}

/** Persisted view preference (board vs table). Defaults to table; validates input. */
function initialView(): LeadsView {
  try {
    return localStorage.getItem('leads:view') === 'board' ? 'board' : 'table';
  } catch {
    return 'table';
  }
}

function ViewToggle({ view, onChange }: { readonly view: LeadsView; readonly onChange: (v: LeadsView) => void }) {
  const buttonClass = (active: boolean) =>
    cn(
      'inline-flex items-center gap-1.5 rounded-full px-3 py-1.5 text-xs font-semibold transition',
      // bg-brand is the primary fill (ink in light, lime in dark); text-on-brand
      // is its paired legible text color (white on ink, ink on lime).
      active ? 'bg-brand text-on-brand' : 'text-text-muted hover:text-text',
    );
  return (
    <div className="inline-flex rounded-full border border-border bg-surface p-0.5">
      <button
        type="button"
        aria-pressed={view === 'table'}
        aria-label="Table view"
        onClick={() => { onChange('table'); }}
        className={buttonClass(view === 'table')}
      >
        <Table2 className="size-3.5" aria-hidden /> Table
      </button>
      <button
        type="button"
        aria-pressed={view === 'board'}
        aria-label="Board view"
        onClick={() => { onChange('board'); }}
        className={buttonClass(view === 'board')}
      >
        <Columns3 className="size-3.5" aria-hidden /> Board
      </button>
    </div>
  );
}

export function LeadsPage() {
  const navigate = useNavigate();
  const { leadId } = useParams();
  const {
    filters,
    sort,
    page,
    selected,
    setQuery,
    setStatus,
    setPlatform,
    setCampaign,
    toggleSort,
    setPage,
    toggleSelect,
    selectAll,
    clearSelection,
  } = useLeadFilters();

  const [view, setView] = useState<LeadsView>(initialView);
  const [pendingMove, setPendingMove] = useState<PendingMove | null>(null);
  const statusMutation = useSetMatchStatus();

  const changeView = useCallback((next: LeadsView) => {
    setView(next);
    try { localStorage.setItem('leads:view', next); } catch { /* private mode — ignore */ }
  }, []);

  // The server owns filtering/sorting/pagination (org-wide). Filters/sort/page each key
  // the query so every combination caches independently.
  const pageSize = view === 'board' ? BOARD_PAGE_SIZE : PER_PAGE;
  const query: LeadsQuery = useMemo(() => ({
    page,
    pageSize,
    sort: toServerSort(sort.key),
    dir: sort.dir,
    // Omit (don't set undefined) absent filters — exactOptionalPropertyTypes is on.
    ...(filters.query ? { q: filters.query } : {}),
    ...(filters.status !== 'all' ? { status: filters.status } : {}),
    ...(filters.platform !== 'all' ? { platform: filters.platform } : {}),
    ...(filters.campaign !== 'all' ? { campaign: filters.campaign } : {}),
  }), [page, pageSize, filters, sort]);
  const { data, isLoading, error, refetch } = useLeads(query);

  const items = useMemo(() => data?.items ?? [], [data]);
  const total = data?.total ?? 0;
  const threshold = data?.CONFIG.matchThreshold ?? 0;
  const platforms = data?.platforms ?? [];
  const campaigns = data?.campaigns ?? [];

  // Stat tiles read the org-wide UNfiltered stats from the server (stable as you filter).
  const stats = data?.stats;
  const newCount = stats?.counts.new ?? 0;
  const won = stats?.won ?? 0;
  const statTotal = stats?.total ?? 0;
  const wonRate = statTotal === 0 ? 0 : won / statTotal;

  const totalPages = Math.max(1, pageCount(total, pageSize));
  const safePage = Math.min(page, totalPages);

  const selectedLeads = useMemo(
    () => items.filter((m) => selected.has(m.commentId)),
    [items, selected],
  );

  // The drawer resolves the lead from the loaded page (the common path: the operator
  // clicked a visible row). A deep link to an off-page lead is out of scope for now.
  const selectedLead = useMemo(
    () => (leadId ? items.find((m) => m.commentId === leadId) ?? null : null),
    [items, leadId],
  );

  const openLead = useCallback(
    (lead: Match) => void navigate(`/leads/${lead.commentId}`),
    [navigate],
  );
  const closeDrawer = useCallback(() => void navigate('/leads'), [navigate]);

  const confirmTerminalMove = useCallback(
    (reason: string) => {
      if (!pendingMove) return;
      const { lead, target } = pendingMove;
      statusMutation.mutate({
        campaignId: lead.campaignId,
        commentId: lead.commentId,
        platform: lead.platform,
        status: target,
        note: reason,
      });
      setPendingMove(null);
    },
    [pendingMove, statusMutation],
  );

  const rangeStart = total === 0 ? 0 : (safePage - 1) * pageSize + 1;
  const rangeEnd = Math.min(safePage * pageSize, total);

  return (
    <>
      <PageHeader
        title="Leads"
        subtitle="Every scored comment the engine captured — search, qualify, and export."
        actions={<ViewToggle view={view} onChange={changeView} />}
      />
      <AsyncBoundary isLoading={isLoading} error={error} onRetry={() => void refetch()}>
        <div className="mb-4 grid grid-cols-2 gap-4 lg:grid-cols-4">
          <LeadStatTile
            label="Total leads"
            value={formatNumber(statTotal)}
            foot="captured across all campaigns"
          />
          <LeadStatTile label="New" value={formatNumber(newCount)} foot="awaiting first touch" />
          <LeadStatTile
            label="Won"
            value={formatNumber(won)}
            foot="interested or closed"
          />
          <LeadStatTile
            label="Win rate"
            value={formatPercent(wonRate)}
            foot="of all captured leads"
          />
        </div>

        <Card>
          <LeadsToolbar
            filters={filters}
            platforms={platforms}
            campaigns={campaigns}
            resultCount={total}
            filtered={items}
            onQueryChange={setQuery}
            onStatusChange={setStatus}
            onPlatformChange={setPlatform}
            onCampaignChange={setCampaign}
          />
          {view === 'table' ? (
            <>
              <LeadsTable
                rows={items}
                threshold={threshold}
                selected={selected}
                sort={sort}
                onSort={toggleSort}
                onToggleSelect={toggleSelect}
                onToggleSelectAll={selectAll}
                onOpen={openLead}
              />
              <div className="flex flex-wrap items-center justify-between gap-3 border-t border-border px-5 py-3">
                <span className="text-[11px] font-semibold tabular-nums text-text-faint">
                  Showing {formatNumber(rangeStart)}–{formatNumber(rangeEnd)} of{' '}
                  {formatNumber(total)} · page {safePage} of {totalPages}
                </span>
                <div className="flex gap-1.5">
                  <button
                    type="button"
                    onClick={() => { setPage(safePage - 1); }}
                    disabled={safePage <= 1}
                    aria-label="Previous page"
                    className="flex size-8 items-center justify-center rounded-full border border-border bg-surface text-text-muted transition hover:-translate-y-px hover:text-text disabled:cursor-not-allowed disabled:opacity-40"
                  >
                    <ChevronLeft className="size-4" aria-hidden />
                  </button>
                  <button
                    type="button"
                    onClick={() => { setPage(safePage + 1); }}
                    disabled={safePage >= totalPages}
                    aria-label="Next page"
                    className="flex size-8 items-center justify-center rounded-full border border-border bg-surface text-text-muted transition hover:-translate-y-px hover:text-text disabled:cursor-not-allowed disabled:opacity-40"
                  >
                    <ChevronRight className="size-4" aria-hidden />
                  </button>
                </div>
              </div>
            </>
          ) : (
            <div className="border-t border-border p-4">
              <LeadBoard
                leads={items}
                threshold={threshold}
                onOpen={openLead}
                onRequestTerminalMove={(lead, target) => { setPendingMove({ lead, target }); }}
              />
            </div>
          )}
        </Card>

        <LeadsBulkBar selectedLeads={selectedLeads} onClear={clearSelection} />
        <LeadDrawer lead={selectedLead} threshold={threshold} onClose={closeDrawer} />
        <ReasonMoveModal
          pending={pendingMove}
          onCancel={() => { setPendingMove(null); }}
          onConfirm={confirmTerminalMove}
          isSubmitting={statusMutation.isPending}
        />
      </AsyncBoundary>
    </>
  );
}
