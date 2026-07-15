import { NavLink } from 'react-router-dom';
import {
  BarChart3,
  LayoutDashboard,
  LogOut,
  Megaphone,
  Moon,
  Settings,
  Sun,
  Users,
} from 'lucide-react';
import type { LucideIcon } from 'lucide-react';
import { BrandMark } from '@/shared/ui/BrandMark';
import { cn } from '@/shared/lib/cn';
import { can, type Action } from '@/shared/auth/roles';
import { useAuth } from '@/shared/hooks/useAuth';
import { useLeads } from '@/shared/hooks/useLeads';
import { useTheme } from '@/shared/hooks/useTheme';

interface NavEntry {
  readonly to: string;
  readonly label: string;
  readonly icon: LucideIcon;
  // The view action that gates this item — mirrors the route guards.
  readonly action: Action;
}

const NAV: readonly NavEntry[] = [
  { to: '/dashboard', label: 'Dashboard', icon: LayoutDashboard, action: 'view_dashboard' },
  { to: '/campaigns', label: 'Campaigns', icon: Megaphone, action: 'view_campaigns' },
  { to: '/leads', label: 'Leads', icon: Users, action: 'view_leads' },
  { to: '/reports', label: 'Reports', icon: BarChart3, action: 'view_reports' },
  { to: '/settings', label: 'Settings', icon: Settings, action: 'view_settings' },
];

function NavItem({ entry, count }: { readonly entry: NavEntry; readonly count?: number }) {
  const Icon = entry.icon;
  return (
    <NavLink
      to={entry.to}
      className={({ isActive }) =>
        cn(
          'flex items-center gap-3 rounded-xl px-3 py-2.5 text-[13px] font-semibold transition-colors',
          isActive
            ? 'bg-accent text-accent-ink'
            : 'text-nav-muted hover:bg-nav-hover hover:text-nav-text',
        )
      }
    >
      <Icon className="size-[18px] shrink-0" aria-hidden />
      <span className="grow">{entry.label}</span>
      {count !== undefined && count > 0 ? (
        <span className="tabular rounded-full bg-white/15 px-1.5 text-[10px] font-bold">
          {count}
        </span>
      ) : null}
    </NavLink>
  );
}

export function Sidebar() {
  // The leads endpoint carries both the product name (CONFIG) and the org-wide "new"
  // count (stats) — and every role may read it, so it's safe chrome for all users.
  const { data } = useLeads({ page: 1, pageSize: 1 });
  const { theme, toggleTheme } = useTheme();
  const { user, logout } = useAuth();

  const role = user?.role ?? null;
  const visibleNav = NAV.filter((entry) => can(role, entry.action));
  const newLeads = data?.stats.counts.new ?? 0;
  const productName = data?.CONFIG.productName ?? 'AIZU';

  return (
    <aside className="flex w-[244px] shrink-0 flex-col rounded-[26px] bg-nav px-3.5 py-5 text-nav-text shadow-lift">
      <div className="flex items-center gap-2.5 px-2.5 pb-4">
        <BrandMark tone="rail" className="size-8 shrink-0" />
        <span className="font-head text-[15px] font-extrabold tracking-tight">
          {productName}
        </span>
      </div>

      <nav className="flex flex-col gap-1">
        {visibleNav.map((entry) => (
          <NavItem
            key={entry.to}
            entry={entry}
            {...(entry.to === '/leads' ? { count: newLeads } : {})}
          />
        ))}
      </nav>

      <div className="mt-auto flex flex-col gap-3 pt-4">
        <div className="flex items-center gap-2.5 rounded-xl border border-nav-border px-3 py-2">
          <span className="flex size-7 shrink-0 items-center justify-center rounded-full bg-brand text-[11px] font-bold uppercase text-on-brand">
            {(user?.email ?? '?').slice(0, 2)}
          </span>
          <span className="grow truncate text-[12px] font-semibold text-nav-text" title={user?.email}>
            {user?.email ?? 'Signed out'}
          </span>
          <button
            type="button"
            onClick={toggleTheme}
            aria-label="Toggle theme"
            className="rounded-lg p-1.5 text-nav-muted transition-colors hover:bg-nav-hover hover:text-nav-text"
          >
            {theme === 'dark' ? <Sun className="size-4" aria-hidden /> : <Moon className="size-4" aria-hidden />}
          </button>
          <button
            type="button"
            onClick={() => { void logout(); }}
            aria-label="Log out"
            className="-mr-1 rounded-lg p-1.5 text-nav-muted transition-colors hover:bg-nav-hover hover:text-nav-text"
          >
            <LogOut className="size-4" aria-hidden />
          </button>
        </div>
      </div>
    </aside>
  );
}
