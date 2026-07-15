import { Link } from 'react-router-dom';
import { Building2, ChevronRight } from 'lucide-react';
import { PageHeader } from '@/app/layout/PageHeader';
import { AsyncBoundary } from '@/shared/ui/AsyncBoundary';
import { Card } from '@/shared/ui/Card';
import { EmptyState } from '@/shared/ui/EmptyState';
import { formatNumber } from '@/shared/lib/formatters';
import { useAdminOrgs } from './adminHooks';

/** Cross-org index (§5d) — every tenant with member + campaign counts, drill-in on click. */
export function OrgsPage() {
  const orgs = useAdminOrgs();
  return (
    <>
      <PageHeader title="Organizations" subtitle="Every tenant on the platform." />
      <Card>
        <AsyncBoundary
          isLoading={orgs.isLoading}
          error={orgs.error}
          onRetry={() => {
            void orgs.refetch();
          }}
        >
          {(orgs.data ?? []).length === 0 ? (
            <EmptyState icon={Building2} title="No organizations" />
          ) : (
            <ul className="divide-y divide-border">
              {(orgs.data ?? []).map((org) => (
                <li key={org.id}>
                  <Link
                    to={`/admin/orgs/${org.id}`}
                    className="flex items-center gap-4 px-5 py-4 transition-colors hover:bg-surface-2"
                  >
                    <span className="flex size-9 shrink-0 items-center justify-center rounded-full bg-brand/10 text-xs font-bold uppercase text-brand">
                      {org.name.slice(0, 2)}
                    </span>
                    <div className="min-w-0 grow">
                      <div className="truncate font-semibold text-text">{org.name}</div>
                      <div className="text-xs text-text-muted">org #{org.id}</div>
                    </div>
                    <div className="shrink-0 text-right text-xs text-text-muted">
                      <div>{formatNumber(org.memberCount)} members</div>
                      <div>{formatNumber(org.campaignCount)} campaigns</div>
                    </div>
                    <ChevronRight className="size-4 shrink-0 text-text-faint" aria-hidden />
                  </Link>
                </li>
              ))}
            </ul>
          )}
        </AsyncBoundary>
      </Card>
    </>
  );
}
