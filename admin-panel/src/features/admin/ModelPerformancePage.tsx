import { Bar, BarChart, CartesianGrid, Cell, ResponsiveContainer, Tooltip, XAxis, YAxis } from 'recharts';
import { Gauge } from 'lucide-react';
import { PageHeader } from '@/app/layout/PageHeader';
import { AsyncBoundary } from '@/shared/ui/AsyncBoundary';
import { Badge } from '@/shared/ui/Badge';
import { Card, CardBody, CardHeader } from '@/shared/ui/Card';
import { EmptyState } from '@/shared/ui/EmptyState';
import { useChartPalette } from '@/shared/hooks/useChartPalette';
import { usePrefersReducedMotion } from '@/shared/hooks/usePrefersReducedMotion';
import { useTooltipProps } from '@/shared/ui/charts/chartTooltip';
import type { ModelComparisonModelStats } from '@/shared/schemas/admin';
import { formatTimestamp } from './format';
import { ModelComparisonCard } from './ModelComparisonCard';
import { useModelComparisonStats } from './adminHooks';

interface ModelBarChartProps {
  readonly stats: readonly ModelComparisonModelStats[];
  readonly valueKey: 'avgLatencyMs' | 'avgUsd';
  readonly valueFormatter: (v: number) => string;
}

/** One metric, one bar per model — the primary model's bar is accented. */
function ModelBarChart({ stats, valueKey, valueFormatter }: ModelBarChartProps) {
  const palette = useChartPalette();
  const tooltip = useTooltipProps();
  const reduced = usePrefersReducedMotion();
  const data = stats.map((s) => ({ model: s.model, value: s[valueKey] ?? 0, isPrimary: s.isPrimary }));
  return (
    <div style={{ height: 200 }}>
      <ResponsiveContainer>
        <BarChart data={data} margin={{ top: 8, right: 8, bottom: 0, left: -16 }}>
          <CartesianGrid stroke={palette.grid} vertical={false} />
          <XAxis
            dataKey="model"
            tick={{ fill: palette.tick, fontSize: 10 }}
            tickLine={false}
            axisLine={{ stroke: palette.grid }}
          />
          <YAxis tick={{ fill: palette.tick, fontSize: 10 }} tickLine={false} axisLine={false} />
          <Tooltip {...tooltip} cursor={{ fill: palette.grid, fillOpacity: 0.4 }}
            formatter={(v: number) => valueFormatter(v)} />
          <Bar dataKey="value" radius={[4, 4, 0, 0]} isAnimationActive={!reduced} animationDuration={800}>
            {data.map((d) => (
              <Cell key={d.model} fill={d.isPrimary ? palette.brand : palette.info} />
            ))}
          </Bar>
        </BarChart>
      </ResponsiveContainer>
    </div>
  );
}

function fmtMs(v: number): string {
  return `${Math.round(v)}ms`;
}

function fmtUsd(v: number): string {
  return `$${v.toFixed(4)}`;
}

function fmtPct(v: number | null): string {
  return v === null ? '—' : `${Math.round(v * 100)}%`;
}

/** Superadmin "Model Performance" page (v17): the fan-out on/off switch + aggregate
 *  per-model stats (latency, cost, agreement with the production model) and a raw
 *  recent-calls log — reads from model_comparison_log via the admin stats route. */
export function ModelPerformancePage() {
  const { data, isLoading, error, refetch } = useModelComparisonStats();
  const stats = data?.stats ?? [];
  const recent = data?.recent ?? [];

  return (
    <>
      <PageHeader
        title="Model performance"
        subtitle="Latency, cost, and agreement across every model compared against production."
      />
      <ModelComparisonCard />

      <AsyncBoundary
        isLoading={isLoading}
        error={error}
        onRetry={() => {
          void refetch();
        }}
      >
        {stats.length === 0 ? (
          <Card className="mt-6">
            <CardBody>
              <EmptyState icon={Gauge} title="No comparison calls logged yet"
                description="Turn on model comparison above and run a live campaign to populate this page." />
            </CardBody>
          </Card>
        ) : (
          <>
            <div className="mt-6 grid gap-4 sm:grid-cols-2">
              <Card>
                <CardHeader title="Avg latency" subtitle="Per model, across every logged call." />
                <CardBody>
                  <ModelBarChart stats={stats} valueKey="avgLatencyMs" valueFormatter={fmtMs} />
                </CardBody>
              </Card>
              <Card>
                <CardHeader title="Avg cost" subtitle="Per model, across every logged call." />
                <CardBody>
                  <ModelBarChart stats={stats} valueKey="avgUsd" valueFormatter={fmtUsd} />
                </CardBody>
              </Card>
            </div>

            <Card className="mt-6">
              <CardBody className="px-0 py-0">
                <div className="overflow-x-auto">
                  <table className="w-full min-w-[760px] text-left text-[13px]">
                    <thead>
                      <tr className="border-b border-border text-xs font-semibold uppercase tracking-wide text-text-faint">
                        <th className="px-4 py-2.5">Model</th>
                        <th className="px-4 py-2.5">Calls</th>
                        <th className="px-4 py-2.5">Avg latency</th>
                        <th className="px-4 py-2.5">Avg cost</th>
                        <th className="px-4 py-2.5">Avg score</th>
                        <th className="px-4 py-2.5">Agreement</th>
                        <th className="px-4 py-2.5">Leads found</th>
                        <th className="px-4 py-2.5">Errors</th>
                      </tr>
                    </thead>
                    <tbody>
                      {stats.map((s) => (
                        <tr key={s.model} className="border-b border-border last:border-0">
                          <td className="px-4 py-2.5 font-semibold text-text">
                            {s.model} {s.isPrimary ? <Badge tone="info">production</Badge> : null}
                          </td>
                          <td className="px-4 py-2.5 text-text-muted">{s.calls}</td>
                          <td className="px-4 py-2.5 text-text-muted">
                            {s.avgLatencyMs === null ? '—' : fmtMs(s.avgLatencyMs)}
                          </td>
                          <td className="px-4 py-2.5 text-text-muted">
                            {s.avgUsd === null ? '—' : fmtUsd(s.avgUsd)}
                          </td>
                          <td className="px-4 py-2.5 text-text-muted">
                            {s.avgScore === null ? '—' : s.avgScore.toFixed(2)}
                          </td>
                          <td className="px-4 py-2.5 text-text-muted">{fmtPct(s.agreementRate)}</td>
                          <td className="px-4 py-2.5 text-text-muted">{s.leadsFound}</td>
                          <td className="px-4 py-2.5">
                            {s.errors > 0 ? <Badge tone="warn">{s.errors}</Badge> : '—'}
                          </td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              </CardBody>
            </Card>

            <Card className="mt-6">
              <CardHeader title="Recent calls" subtitle="Newest first, most recent 200." />
              <CardBody className="px-0 py-0">
                <div className="max-h-[420px] overflow-y-auto overflow-x-auto">
                  <table className="w-full min-w-[760px] text-left text-[13px]">
                    <thead>
                      <tr className="border-b border-border text-xs font-semibold uppercase tracking-wide text-text-faint">
                        <th className="px-4 py-2.5">When</th>
                        <th className="px-4 py-2.5">Campaign</th>
                        <th className="px-4 py-2.5">Model</th>
                        <th className="px-4 py-2.5">Label</th>
                        <th className="px-4 py-2.5">Score</th>
                        <th className="px-4 py-2.5">Latency</th>
                        <th className="px-4 py-2.5">Cost</th>
                        <th className="px-4 py-2.5">Error</th>
                      </tr>
                    </thead>
                    <tbody>
                      {recent.map((c, i) => (
                        <tr key={`${c.campaign_id}-${c.model}-${c.created_at}-${i}`}
                          className="border-b border-border last:border-0">
                          <td className="whitespace-nowrap px-4 py-2.5 text-xs text-text-muted">
                            {formatTimestamp(c.created_at)}
                          </td>
                          <td className="px-4 py-2.5 text-text-muted">{c.campaign_id}</td>
                          <td className="px-4 py-2.5 text-text">
                            {c.model} {c.is_primary ? <Badge tone="info">prod</Badge> : null}
                          </td>
                          <td className="px-4 py-2.5 text-text-muted">{c.label ?? '—'}</td>
                          <td className="px-4 py-2.5 text-text-muted">
                            {c.score === null ? '—' : c.score.toFixed(2)}
                          </td>
                          <td className="px-4 py-2.5 text-text-muted">
                            {c.latency_ms === null ? '—' : fmtMs(c.latency_ms)}
                          </td>
                          <td className="px-4 py-2.5 text-text-muted">
                            {c.usd === null ? '—' : fmtUsd(c.usd)}
                          </td>
                          <td className="px-4 py-2.5 text-danger">{c.error ?? '—'}</td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              </CardBody>
            </Card>
          </>
        )}
      </AsyncBoundary>
    </>
  );
}
