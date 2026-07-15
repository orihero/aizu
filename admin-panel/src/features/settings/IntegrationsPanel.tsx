import { Plug } from 'lucide-react';
import { Card, CardBody } from '@/shared/ui/Card';
import { EmptyState } from '@/shared/ui/EmptyState';
import type { Integration } from '@/shared/types/domain';
import { IntegrationCardShell } from './integrations/IntegrationCardShell';
import { YouTubeConnectCard } from './integrations/YouTubeConnectCard';
import { TelegramConnectCard } from './integrations/TelegramConnectCard';
import { RedditConnectCard } from './integrations/RedditConnectCard';

/** Instagram stays single-tenant (warmed-Chrome / CDP, admin-managed), so it is
 * shown read-only here — not a self-serve connect. */
function ManagedCard({ integration }: { readonly integration: Integration }) {
  return (
    <IntegrationCardShell integration={integration}>
      <p className="text-xs text-text-muted">
        Managed by your administrator. Contact support to change this connection.
      </p>
    </IntegrationCardShell>
  );
}

function IntegrationCard({ integration }: { readonly integration: Integration }) {
  switch (integration.platform) {
    case 'youtube':
      return <YouTubeConnectCard integration={integration} />;
    case 'telegram':
      return <TelegramConnectCard integration={integration} />;
    case 'reddit':
      return <RedditConnectCard integration={integration} />;
    default:
      return <ManagedCard integration={integration} />;
  }
}

export function IntegrationsPanel({
  integrations,
}: {
  readonly integrations: readonly Integration[];
}) {
  if (integrations.length === 0) {
    return (
      <Card>
        <CardBody>
          <EmptyState
            icon={Plug}
            title="No integrations"
            description="Connected platforms will appear here once the engine reports them."
          />
        </CardBody>
      </Card>
    );
  }
  return (
    <div className="grid gap-4 sm:grid-cols-2">
      {integrations.map((integration) => (
        <IntegrationCard key={integration.id} integration={integration} />
      ))}
    </div>
  );
}
