import { useParams } from 'react-router-dom';
import { PageHeader } from '@/app/layout/PageHeader';
import { AsyncBoundary } from '@/shared/ui/AsyncBoundary';
import { useSettings } from '@/shared/hooks/useSettings';
import { can, type Role } from '@/shared/auth/roles';
import { useRole } from '@/shared/hooks/useCan';
import type { SettingsPayload } from '@/shared/types/domain';
import { resolveTab, TABS, type SettingsTab } from './tabs';
import { SettingsRail, TAB_ACTION } from './SettingsRail';
import { WorkspacePanel } from './WorkspacePanel';
import { TeamPanel } from './TeamPanel';
import { IntegrationsPanel } from './IntegrationsPanel';
import { BillingPanel } from './BillingPanel';
import { Card, CardBody } from '@/shared/ui/Card';

function ActivePanel({ tab, data }: { readonly tab: SettingsTab; readonly data: SettingsPayload }) {
  if (tab === 'team') return <TeamPanel members={data.TEAM} invites={data.INVITES} />;
  if (tab === 'integrations') return <IntegrationsPanel integrations={data.INTEGRATIONS} />;
  if (tab === 'billing') {
    // BILLING is role-pruned server-side; a permitted role always receives it, but
    // guard so a pruned payload degrades to a message rather than a crash.
    return data.BILLING ? (
      <BillingPanel billing={data.BILLING} />
    ) : (
      <Card>
        <CardBody className="text-sm text-text-muted">Billing is not available for your role.</CardBody>
      </Card>
    );
  }
  return <WorkspacePanel config={data.CONFIG} />;
}

/** First tab the role may open, falling back to 'workspace' when none qualify. */
function firstPermittedTab(role: Role | null): SettingsTab {
  return TABS.find((tab) => can(role, TAB_ACTION[tab.key]))?.key ?? 'workspace';
}

export function SettingsPage() {
  const { tab } = useParams();
  const role = useRole();
  const requestedTab = resolveTab(tab);
  // Never render a panel the role may not see: fall back to the first permitted tab.
  const activeTab = can(role, TAB_ACTION[requestedTab])
    ? requestedTab
    : firstPermittedTab(role);
  const { data, isLoading, error, refetch } = useSettings();

  return (
    <>
      <PageHeader title="Settings" subtitle="Workspace, team, integrations and billing." />
      <AsyncBoundary isLoading={isLoading} error={error} onRetry={() => void refetch()}>
        {data ? (
          <div className="grid items-start gap-6 lg:grid-cols-[16rem_1fr]">
            <SettingsRail active={activeTab} />
            <div className="min-w-0">
              <ActivePanel tab={activeTab} data={data} />
            </div>
          </div>
        ) : null}
      </AsyncBoundary>
    </>
  );
}
