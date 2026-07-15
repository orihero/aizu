import { Trophy } from 'lucide-react';
import type { TopCampaign } from '@/shared/types/domain';
import { CardBody, CardHeader } from '@/shared/ui/Card';
import { formatMoney, formatNumber } from '@/shared/lib/formatters';
import { platformColor } from '@/shared/ui/charts';

interface TopCampaignsTileProps {
  readonly campaigns: readonly TopCampaign[];
}

function PlatformChip({ platform }: { readonly platform: string }) {
  const color = platformColor(platform, 'var(--color-text-faint)');
  return (
    <span className="inline-flex items-center gap-1.5 text-[11.5px] font-medium text-text-muted">
      <span
        className="size-2 rounded-full"
        style={{ background: color }}
        aria-hidden
      />
      {platform}
    </span>
  );
}

/** Ranked campaigns by leads with platform, lead count, and cost per lead. */
export function TopCampaignsTile({ campaigns }: TopCampaignsTileProps) {
  return (
    <>
      <CardHeader
        title={
          <>
            <Trophy className="size-4 text-text-faint" aria-hidden />
            Top campaigns
          </>
        }
      />
      <CardBody className="px-0 py-0">
        {campaigns.length === 0 ? (
          <p className="px-5 py-8 text-center text-sm text-text-muted">No campaigns yet.</p>
        ) : (
          <table className="w-full text-sm">
            <thead>
              <tr className="border-b border-border text-[11px] font-semibold uppercase tracking-wide text-text-faint">
                <th className="px-5 py-2.5 text-left font-semibold">Campaign</th>
                <th className="px-3 py-2.5 text-left font-semibold">Platform</th>
                <th className="px-3 py-2.5 text-right font-semibold">Leads</th>
                <th className="px-5 py-2.5 text-right font-semibold">CPL</th>
              </tr>
            </thead>
            <tbody>
              {campaigns.map((c) => (
                <tr key={c.id} className="border-b border-border last:border-0">
                  <td className="max-w-0 truncate px-5 py-3 font-medium text-text">{c.name}</td>
                  <td className="px-3 py-3">
                    <PlatformChip platform={c.platform} />
                  </td>
                  <td className="px-3 py-3 text-right tabular font-semibold text-text">
                    {formatNumber(c.leads)}
                  </td>
                  <td className="px-5 py-3 text-right tabular text-text-muted">
                    {c.cpl === null ? '—' : formatMoney(c.cpl)}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </CardBody>
    </>
  );
}
