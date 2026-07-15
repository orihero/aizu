import { Navigate, Outlet } from 'react-router-dom';
import { Loader2 } from 'lucide-react';
import { useAdminAuth } from './useAdminAuth';

/** Full-screen spinner while the admin cookie session resolves. */
function AdminLoading() {
  return (
    <div
      role="status"
      aria-label="Loading"
      className="flex h-full items-center justify-center bg-bg"
    >
      <Loader2 className="size-6 animate-spin text-text-faint" aria-hidden />
    </div>
  );
}

/**
 * Gate for the admin subtree: render only for a live admin session; bounce an
 * anonymous visitor to the admin login. The server re-enforces (IP-allowlist +
 * cookie + TOTP); this only keeps the UI honest.
 */
export function RequireSuper() {
  const { status } = useAdminAuth();
  if (status === 'loading') return <AdminLoading />;
  if (status === 'anonymous') return <Navigate to="/admin/login" replace />;
  return <Outlet />;
}

/** Inverse gate for /admin/login: send an already-authenticated admin to the console. */
export function RedirectIfAdmin() {
  const { status } = useAdminAuth();
  if (status === 'loading') return <AdminLoading />;
  if (status === 'authenticated') return <Navigate to="/admin" replace />;
  return <Outlet />;
}
