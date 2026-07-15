import type { Integration } from '@/shared/types/domain';

/** True when the engine flagged this connection as needing re-auth at run time
 * (Phase 4: a revoked/expired credential flips detail to "needs reconnect"). */
export function needsReconnect(integration: Integration): boolean {
  return integration.detail.toLowerCase().includes('needs reconnect');
}
