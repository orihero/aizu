import { useNavigate } from 'react-router-dom';
import { Card } from '@/shared/ui/Card';
import { cn } from '@/shared/lib/cn';
import { can, type Action } from '@/shared/auth/roles';
import { useRole } from '@/shared/hooks/useCan';
import { TABS, type SettingsTab } from './tabs';

interface SettingsRailProps {
  readonly active: SettingsTab;
}

/**
 * Each settings sub-tab → the RBAC *view* action that gates its visibility (mirror
 * of the matrix in `roles.ts`). Visibility must follow a read/view permission, never
 * a write one: the Workspace and Integrations panels are sub-pages of Settings, so
 * they use `view_settings` (reaching /settings already requires it); the Team panel
 * has its own explicit `view_team` gate. Toggling an integration is a separate write
 * action (`toggle_integration`) enforced server-side and inside IntegrationsPanel —
 * NOT here — so a role that can view settings but not toggle still sees the tab.
 */
export const TAB_ACTION: Record<SettingsTab, Action> = {
  workspace: 'view_settings',
  team: 'view_team',
  integrations: 'view_settings',
  // Billing is sensitive (plan + spend): its own view_billing gate (owner/admin),
  // mirroring the server-side pruning of BILLING on /api/settings.
  billing: 'view_billing',
};

/** Sticky vertical tab rail. Clicking an item navigates to /settings/{tab}.
 *  Tabs are gated by the RBAC matrix via can(role, action) — mirroring the
 *  Sidebar — so the rail never offers a section the user may not open. */
export function SettingsRail({ active }: SettingsRailProps) {
  const navigate = useNavigate();
  const role = useRole();
  // Filtering by can() keeps visibility derived from the matrix (defense-in-depth).
  const visibleTabs = TABS.filter((tab) => can(role, TAB_ACTION[tab.key]));
  return (
    <Card className="sticky top-6 self-start p-2">
      <nav aria-label="Settings sections" className="flex flex-col gap-1">
        {visibleTabs.map((tab) => {
          const isActive = tab.key === active;
          const Icon = tab.icon;
          return (
            <button
              key={tab.key}
              type="button"
              aria-current={isActive ? 'page' : undefined}
              onClick={() => navigate(`/settings/${tab.key}`)}
              className={cn(
                'flex items-start gap-3 rounded-lg px-3 py-2.5 text-left transition',
                isActive
                  ? 'bg-accent text-accent-ink'
                  : 'text-text-muted hover:bg-surface-2',
              )}
            >
              <Icon className="mt-0.5 size-4 shrink-0" aria-hidden />
              <span className="min-w-0">
                <span className="block text-sm font-semibold">{tab.label}</span>
                <span
                  className={cn(
                    'block text-[11px]',
                    isActive ? 'text-accent-ink/75' : 'text-text-faint',
                  )}
                >
                  {tab.description}
                </span>
              </span>
            </button>
          );
        })}
      </nav>
    </Card>
  );
}
