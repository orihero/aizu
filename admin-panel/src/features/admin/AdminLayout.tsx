import { NavLink, Outlet } from 'react-router-dom';
import { Building2, Gauge, LogOut, ScrollText, Server, ShieldCheck } from 'lucide-react';
import type { LucideIcon } from 'lucide-react';
import { cn } from '@/shared/lib/cn';
import { useAdminAuth } from './useAdminAuth';
import { ImpersonationBanner } from './ImpersonationBanner';

interface AdminNavEntry {
  readonly to: string;
  readonly label: string;
  readonly icon: LucideIcon;
  /** Only the fleet index matches exactly; the others match their subtree. */
  readonly end?: boolean;
}

const ADMIN_NAV: readonly AdminNavEntry[] = [
  { to: '/admin', label: 'Fleet', icon: Server, end: true },
  { to: '/admin/orgs', label: 'Organizations', icon: Building2 },
  { to: '/admin/audit', label: 'Audit log', icon: ScrollText },
  { to: '/admin/model-performance', label: 'Model performance', icon: Gauge },
];

function AdminNavItem({ entry }: { readonly entry: AdminNavEntry }) {
  const Icon = entry.icon;
  return (
    <NavLink
      to={entry.to}
      end={entry.end ?? false}
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
    </NavLink>
  );
}

/**
 * Chrome for the superadmin subtree — its own rail (Fleet / Orgs / Audit),
 * deliberately NOT the org Sidebar, so org-scoping can't leak into the plane.
 * The impersonation banner sits above every admin page.
 */
export function AdminLayout() {
  const { session, logout } = useAdminAuth();
  return (
    <div className="flex h-full gap-5 overflow-hidden bg-bg p-5">
      <aside className="flex w-[244px] shrink-0 flex-col rounded-[26px] bg-nav px-3.5 py-5 text-nav-text shadow-lift">
        <div className="flex items-center gap-2.5 px-2.5 pb-4">
          <span className="flex size-8 shrink-0 items-center justify-center rounded-full bg-brand/20">
            <ShieldCheck className="size-4 text-accent" aria-hidden />
          </span>
          <span className="font-head text-[15px] font-extrabold tracking-tight">Superadmin</span>
        </div>

        <nav className="flex flex-col gap-1">
          {ADMIN_NAV.map((entry) => (
            <AdminNavItem key={entry.to} entry={entry} />
          ))}
        </nav>

        <div className="mt-auto flex flex-col gap-3 pt-4">
          <div className="flex items-center gap-2.5 rounded-xl border border-nav-border px-3 py-2">
            <span className="flex size-7 shrink-0 items-center justify-center rounded-full bg-brand text-[11px] font-bold uppercase text-on-brand">
              {(session?.email ?? '?').slice(0, 2)}
            </span>
            <span
              className="grow truncate text-[12px] font-semibold text-nav-text"
              title={session?.email}
            >
              {session?.email ?? 'Signed out'}
            </span>
            <button
              type="button"
              onClick={() => {
                void logout();
              }}
              aria-label="Log out"
              className="-mr-1 rounded-lg p-1.5 text-nav-muted transition-colors hover:bg-nav-hover hover:text-nav-text"
            >
              <LogOut className="size-4" aria-hidden />
            </button>
          </div>
        </div>
      </aside>

      <main className="min-w-0 grow overflow-y-auto">
        <div className="mx-auto max-w-[1380px] pb-12">
          <ImpersonationBanner />
          <Outlet />
        </div>
      </main>
    </div>
  );
}
