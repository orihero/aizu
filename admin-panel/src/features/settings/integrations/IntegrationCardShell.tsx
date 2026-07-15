import type { ReactNode } from 'react';
import { Card } from '@/shared/ui/Card';
import { Badge } from '@/shared/ui/Badge';
import type { Integration } from '@/shared/types/domain';
import { needsReconnect } from './integrationStatus';

/** Shared chrome for every integration card: name, platform, status badge, then
 * a platform-specific body (form / wizard / managed note). Keeps each platform
 * card focused on its own connect flow. */
export function IntegrationCardShell({
  integration,
  children,
}: {
  readonly integration: Integration;
  readonly children: ReactNode;
}) {
  const reconnect = needsReconnect(integration);
  const tone = reconnect ? 'warn' : integration.connected ? 'success' : 'neutral';
  const label = reconnect ? 'Needs reconnect' : integration.connected ? 'Connected' : 'Not connected';
  return (
    <Card className="flex flex-col gap-3 p-5">
      <div className="flex items-start justify-between gap-3">
        <div className="min-w-0">
          <div className="text-sm font-semibold text-text">{integration.name}</div>
          <div className="mt-0.5 text-[11px] uppercase tracking-wide text-text-faint">
            {integration.platform}
          </div>
        </div>
        <Badge tone={tone}>{label}</Badge>
      </div>
      {children}
    </Card>
  );
}
