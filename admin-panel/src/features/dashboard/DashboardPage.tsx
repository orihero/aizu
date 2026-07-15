import type { ReactNode } from 'react';
import {
  Activity,
  AlertTriangle,
  Clock,
  DollarSign,
  Film,
  KanbanSquare,
  Megaphone,
  PhoneOff,
  Target,
  TrendingUp,
  Users,
} from 'lucide-react';
import type { Dashboard, DashboardPeriodKey, Match } from '@/shared/types/domain';
import { PageHeader } from '@/app/layout/PageHeader';
import { AsyncBoundary } from '@/shared/ui/AsyncBoundary';
import { CardBody, CardHeader } from '@/shared/ui/Card';
import { CountUp } from '@/shared/ui/CountUp';
import { useDashboard } from '@/shared/hooks/useDashboard';
import { usePersistedQueryState } from '@/shared/hooks/usePersistedQueryState';
import { formatMoney, formatNumber, formatPercent } from '@/shared/lib/formatters';
import { selectIsAnyRunActive } from '@/shared/selectors/campaigns';
import {
  asDashboardPeriod,
  selectChannelData,
  selectFunnelStages,
  selectNeedsAttention,
  selectPeriod,
  selectPipelineStages,
  selectStatusDistribution,
  selectTeamActivity,
  selectWinRate,
} from '@/shared/selectors/dashboard';
import {
  DonutChart,
  FunnelChartView,
  GoalGauge,
  GroupedBarChart,
  HeatGrid,
  MiniBars,
  Sparkline,
} from '@/shared/ui/charts';
import { BentoLabel, BentoTile } from './BentoTile';
import { DeltaChip } from './DeltaChip';
import { LiveTickerTile } from './LiveTickerTile';
import { PeriodSelector } from './PeriodSelector';
import { TopCampaignsTile } from './TopCampaignsTile';

export function DashboardPage() {
  const { data, isLoading, error, refetch } = useDashboard();
  const [period, setPeriod] = usePersistedQueryState<DashboardPeriodKey>({
    paramKey: 'period',
    storageKey: 'dashboard:period',
    defaultValue: 'week',
    parse: asDashboardPeriod,
    serialize: (value) => (value === 'week' ? null : value),
    validate: asDashboardPeriod,
  });

  return (
    <>
      <PageHeader
        title="Dashboard"
        subtitle="Lead flow at a glance."
        actions={<PeriodSelector value={period} onChange={setPeriod} />}
      />
      <AsyncBoundary isLoading={isLoading} error={error} onRetry={() => void refetch()}>
        {data ? (
          <DashboardGrid
            data={data.DASHBOARD}
            matches={data.MATCHES}
            period={period}
            isLive={selectIsAnyRunActive(data.RUN)}
          />
        ) : null}
      </AsyncBoundary>
    </>
  );
}

interface DashboardGridProps {
  readonly data: Dashboard;
  readonly matches: readonly Match[];
  readonly period: DashboardPeriodKey;
  readonly isLive: boolean;
}

function DashboardGrid({ data, matches, period, isLive }: DashboardGridProps) {
  const p = selectPeriod(data, period);
  const channelData = selectChannelData(p.channels);
  const funnelStages = selectFunnelStages(p.funnel);
  // Lead-pipeline stats are derived from the current MATCHES (status + history).
  const statusDistribution = selectStatusDistribution(matches);
  const pipelineStages = selectPipelineStages(matches);
  const winRate = selectWinRate(matches);
  const teamActivity = selectTeamActivity(matches);
  const attention = selectNeedsAttention(matches);
  const totalLeads = matches.length;

  return (
    <div className="dash-grid grid grid-cols-4 gap-4">
      {/* Hero: leads this period */}
      <BentoTile tone="brand" span={2}>
        <div className="flex items-start justify-between">
          <BentoLabel className="flex items-center gap-2 text-white/70">
            Leads · {period}
            {isLive ? <span className="live-dot" aria-label="live" /> : null}
          </BentoLabel>
          <DeltaChip delta={p.leads.delta} onColor />
        </div>
        <div className="mt-3 font-head text-[44px] font-extrabold leading-none tabular">
          <CountUp value={p.leads.value} format={formatNumber} />
        </div>
        <div className="mt-4">
          {p.leads.spark.length > 0 ? (
            <Sparkline data={p.leads.spark} color="#ffffff" />
          ) : (
            <p className="py-4 text-[12px] text-white/70">No leads yet.</p>
          )}
        </div>
      </BentoTile>

      {/* Goal gauge */}
      <BentoTile tone="lime">
        <BentoLabel className="text-accent-ink/70">Goal</BentoLabel>
        <div className="mt-1">
          <GoalGauge
            pct={p.goal.pct}
            color="var(--color-accent-ink)"
            label={`${formatNumber(p.goal.current)} / ${formatNumber(p.goal.target)}`}
          />
        </div>
      </BentoTile>

      {/* CPL */}
      <BentoTile tone="ink">
        <div className="flex items-center gap-2">
          <DollarSign className="size-3.5 opacity-70" aria-hidden />
          <BentoLabel>Cost per lead</BentoLabel>
        </div>
        <div className="mt-3 font-head text-[34px] font-extrabold leading-none tabular">
          {p.cpl.value === null ? '—' : <CountUp value={p.cpl.value} format={formatMoney} />}
        </div>
        <div className="mt-4">
          {p.cpl.history.length > 0 ? (
            <MiniBars data={p.cpl.history} />
          ) : (
            <p className="py-4 text-[12px] text-nav-muted">No history yet.</p>
          )}
        </div>
      </BentoTile>

      {/* Conversion KPI */}
      <BentoTile>
        <BentoTileBody>
          <div className="flex items-center gap-2 text-[11px] font-semibold uppercase tracking-wide text-text-faint">
            <TrendingUp className="size-3.5" aria-hidden />
            Conversion
          </div>
          <div className="mt-3 font-head text-[30px] font-extrabold leading-none tabular tracking-tight">
            <CountUp value={p.conversion.value} format={formatPercent} />
          </div>
          <div className="mt-2">
            <DeltaChip delta={p.conversion.delta} />
          </div>
        </BentoTileBody>
      </BentoTile>

      {/* Active campaigns KPI */}
      <BentoTile>
        <BentoTileBody>
          <div className="flex items-center gap-2 text-[11px] font-semibold uppercase tracking-wide text-text-faint">
            <Megaphone className="size-3.5" aria-hidden />
            Active campaigns
          </div>
          <div className="mt-3 font-head text-[30px] font-extrabold leading-none tabular tracking-tight">
            <CountUp value={p.activeCampaigns} format={formatNumber} />
          </div>
        </BentoTileBody>
      </BentoTile>

      {/* Videos watched KPI */}
      <BentoTile span={2}>
        <BentoTileBody>
          <div className="flex items-center gap-2 text-[11px] font-semibold uppercase tracking-wide text-text-faint">
            <Film className="size-3.5" aria-hidden />
            Videos watched · {period}
          </div>
          <div className="mt-3 font-head text-[30px] font-extrabold leading-none tabular tracking-tight">
            <CountUp value={p.funnel.reels} format={formatNumber} />
          </div>
          <div className="mt-3 flex gap-5 text-[12px] text-text-muted">
            <span>
              <CountUp
                value={p.funnel.relevant}
                format={formatNumber}
                className="font-semibold text-text-strong tabular"
              />{' '}
              relevant
            </span>
            <span>
              <CountUp
                value={p.funnel.scored}
                format={formatNumber}
                className="font-semibold text-text-strong tabular"
              />{' '}
              scored
            </span>
          </div>
        </BentoTileBody>
      </BentoTile>

      {/* Leads by channel */}
      <BentoTile span={2}>
        <CardHeader
          title={
            <>
              <Activity className="size-4 text-text-faint" aria-hidden />
              Leads by channel
            </>
          }
        />
        <CardBody>
          {channelData.length > 0 ? (
            <GroupedBarChart
              data={channelData}
              currentLabel="This period"
              previousLabel="Previous"
            />
          ) : (
            <p className="py-10 text-center text-sm text-text-muted">No channel data yet.</p>
          )}
        </CardBody>
      </BentoTile>

      {/* Funnel */}
      <BentoTile span={2}>
        <CardHeader
          title={
            <>
              <Target className="size-4 text-text-faint" aria-hidden />
              Capture funnel
            </>
          }
        />
        <CardBody>
          {p.funnel.reels > 0 ? (
            <FunnelChartView stages={funnelStages} />
          ) : (
            <p className="py-10 text-center text-sm text-text-muted">No funnel data yet.</p>
          )}
        </CardBody>
      </BentoTile>

      {/* Best hour heatmap */}
      <BentoTile span={2}>
        <CardHeader
          title={
            <>
              <Clock className="size-4 text-text-faint" aria-hidden />
              Best hour to capture
            </>
          }
        />
        <CardBody>
          {p.bestHour.length > 0 ? (
            <HeatGrid values={p.bestHour} />
          ) : (
            <p className="py-10 text-center text-sm text-text-muted">No hourly data yet.</p>
          )}
        </CardBody>
      </BentoTile>

      {/* Top campaigns */}
      <BentoTile span={2}>
        <TopCampaignsTile campaigns={p.topCampaigns} />
      </BentoTile>

      {/* Live ticker */}
      <BentoTile span={2}>
        <LiveTickerTile entries={p.ticker} isLive={isLive} />
      </BentoTile>

      {/* Lead status distribution */}
      <BentoTile span={2}>
        <CardHeader
          title={
            <>
              <KanbanSquare className="size-4 text-text-faint" aria-hidden />
              Lead status
            </>
          }
        />
        <CardBody>
          {totalLeads > 0 ? (
            <DonutChart
              data={statusDistribution}
              centerValue={formatNumber(totalLeads)}
              centerLabel="leads"
            />
          ) : (
            <p className="py-10 text-center text-sm text-text-muted">No leads yet.</p>
          )}
        </CardBody>
      </BentoTile>

      {/* Pipeline conversion + win rate */}
      <BentoTile span={2}>
        <CardHeader
          title={
            <>
              <TrendingUp className="size-4 text-text-faint" aria-hidden />
              Pipeline · {formatPercent(winRate)} win rate
            </>
          }
        />
        <CardBody>
          {totalLeads > 0 ? (
            <FunnelChartView stages={pipelineStages} />
          ) : (
            <p className="py-10 text-center text-sm text-text-muted">No pipeline data yet.</p>
          )}
        </CardBody>
      </BentoTile>

      {/* Team activity */}
      <BentoTile span={2}>
        <CardHeader
          title={
            <>
              <Users className="size-4 text-text-faint" aria-hidden />
              Status changes by teammate
            </>
          }
        />
        <CardBody>
          {teamActivity.length > 0 ? (
            <GroupedBarChart data={teamActivity} currentLabel="Changes" previousLabel="" />
          ) : (
            <p className="py-10 text-center text-sm text-text-muted">No status changes yet.</p>
          )}
        </CardBody>
      </BentoTile>

      {/* Needs attention */}
      <BentoTile span={2}>
        <CardHeader
          title={
            <>
              <AlertTriangle className="size-4 text-text-faint" aria-hidden />
              Needs attention
            </>
          }
        />
        <CardBody>
          <div className="grid grid-cols-3 gap-3">
            <AttentionStat icon={<Clock className="size-3.5" aria-hidden />} label="Stuck" value={attention.stuckInProgress} />
            <AttentionStat icon={<PhoneOff className="size-3.5" aria-hidden />} label="No connect" value={attention.couldntConnect} />
            <AttentionStat icon={<AlertTriangle className="size-3.5" aria-hidden />} label="Idle" value={attention.noActivity} />
          </div>
        </CardBody>
      </BentoTile>
    </div>
  );
}

function AttentionStat({
  icon,
  label,
  value,
}: {
  readonly icon: ReactNode;
  readonly label: string;
  readonly value: number;
}) {
  return (
    <div className="rounded-card border border-border bg-surface px-3 py-3">
      <div className="flex items-center gap-1.5 text-[11px] font-semibold uppercase tracking-wide text-text-faint">
        {icon}
        {label}
      </div>
      <div className="mt-2 font-head text-[26px] font-extrabold leading-none tabular">
        <CountUp value={value} format={formatNumber} />
      </div>
    </div>
  );
}

/** Padding wrapper for surface KPI tiles (Card has no built-in padding). */
function BentoTileBody({ children }: { readonly children: ReactNode }) {
  return <div className="px-5 py-5">{children}</div>;
}
