import { Navigate, Outlet } from 'react-router-dom';
import { Loader2 } from 'lucide-react';
import { useAuth } from '@/shared/hooks/useAuth';
import { can, roleHome, type Action } from '@/shared/auth/roles';

/** Full-screen spinner shown while the cookie session is being resolved. */
function AuthLoading() {
  return (
    <div role="status" aria-label="Loading" className="flex h-full items-center justify-center bg-bg">
      <Loader2 className="size-6 animate-spin text-text-faint" aria-hidden />
    </div>
  );
}

/** Gate for the app routes: render only for a live session; bounce anonymous to /login. */
export function RequireAuth() {
  const { status } = useAuth();
  if (status === 'loading') return <AuthLoading />;
  if (status === 'anonymous') return <Navigate to="/login" replace />;
  return <Outlet />;
}

/** Inverse gate for /login + /signup: send an already-authenticated user to the app. */
export function RedirectIfAuthed() {
  const { status, user } = useAuth();
  if (status === 'loading') return <AuthLoading />;
  if (status === 'authenticated') return <Navigate to={roleHome(user?.role)} replace />;
  return <Outlet />;
}

/**
 * Role gate for an app route: render only if the user's role permits `action`,
 * else bounce to their role-appropriate home (a member lands on /leads, others on
 * /dashboard). The server re-enforces; this keeps the UI honest.
 */
export function RequireCan({ action }: { readonly action: Action }) {
  const { user } = useAuth();
  if (!can(user?.role, action)) return <Navigate to={roleHome(user?.role)} replace />;
  return <Outlet />;
}

/** Sends the user to the route their role lands on (used by '/' and the catch-all). */
export function RoleHome() {
  const { user } = useAuth();
  return <Navigate to={roleHome(user?.role)} replace />;
}
