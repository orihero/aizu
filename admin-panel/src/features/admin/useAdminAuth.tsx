import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useState,
} from 'react';
import { Outlet } from 'react-router-dom';
import { useQueryClient } from '@tanstack/react-query';
import { usePanelRepository } from '@/shared/api/repositoryContext';
import { ResultError, type Result } from '@/shared/lib/result';
import type { AdminLoginInput, AdminSession } from '@/shared/schemas/admin';

/** 'loading' until the admin cookie is resolved on mount; then authed/anonymous. */
export type AdminAuthStatus = 'loading' | 'authenticated' | 'anonymous';

interface AdminAuthContextValue {
  readonly session: AdminSession | null;
  readonly status: AdminAuthStatus;
  // Arrow-property signatures (not methods) so destructuring them from the hook
  // doesn't trip @typescript-eslint/unbound-method at call sites.
  readonly login: (input: AdminLoginInput) => Promise<Result<{ id: number; email: string }>>;
  readonly logout: () => Promise<void>;
  /** Re-resolve whoami — call after impersonate start/end so the banner reflects state. */
  readonly refresh: () => Promise<void>;
}

const AdminAuthContext = createContext<AdminAuthContextValue | null>(null);

/**
 * Superadmin session state. This is a SEPARATE auth plane from the org
 * `useAuth` (its own cookie `rr_admin_session`, gated by IP-allowlist + TOTP) —
 * an org role never grants it. Bootstraps from GET /api/admin/whoami on mount.
 * Mounted as the `/admin` route element so it only runs for the admin subtree.
 */
export function AdminAuthProvider() {
  const repository = usePanelRepository();
  const queryClient = useQueryClient();
  const [session, setSession] = useState<AdminSession | null>(null);
  const [status, setStatus] = useState<AdminAuthStatus>('loading');

  const resolve = useCallback(async () => {
    const result = await repository.adminWhoami();
    if (result.ok && result.value) {
      setSession(result.value);
      setStatus('authenticated');
    } else {
      setSession(null);
      setStatus('anonymous');
    }
  }, [repository]);

  useEffect(() => {
    let cancelled = false;
    void repository
      .adminWhoami()
      .then((result) => {
        if (cancelled) return;
        if (result.ok && result.value) {
          setSession(result.value);
          setStatus('authenticated');
        } else {
          setSession(null);
          setStatus('anonymous');
        }
      })
      .catch(() => {
        // The repository contract returns a Result and shouldn't throw, but never
        // strand the subtree on 'loading' if a diverging impl does.
        if (!cancelled) {
          setSession(null);
          setStatus('anonymous');
        }
      });
    return () => {
      cancelled = true;
    };
  }, [repository]);

  // Mid-session expiry: an ADMIN query that 401/403s means the admin cookie died
  // (or the IP fell out of the allowlist). Scope strictly to 'admin' query keys so
  // an anonymous ORG query 401 never tears down the admin session.
  useEffect(() => {
    const cache = queryClient.getQueryCache();
    return cache.subscribe((event) => {
      const key: unknown = event.query.queryKey;
      if (!Array.isArray(key) || key[0] !== 'admin') return;
      const error: unknown = event.query.state.error;
      if (error instanceof ResultError
        && (error.appError.status === 401 || error.appError.status === 403)) {
        setSession(null);
        setStatus('anonymous');
      }
    });
  }, [queryClient]);

  const login = useCallback(
    async (input: AdminLoginInput) => {
      const result = await repository.adminLogin(input);
      if (result.ok) {
        // login returns only {id,email}; resolve the full session (impersonation state).
        await resolve();
        await queryClient.invalidateQueries();
      }
      return result;
    },
    [repository, resolve, queryClient],
  );

  const logout = useCallback(async () => {
    await repository.adminLogout();
    setSession(null);
    setStatus('anonymous');
    queryClient.clear();
  }, [repository, queryClient]);

  const value = useMemo(
    () => ({ session, status, login, logout, refresh: resolve }),
    [session, status, login, logout, resolve],
  );

  return (
    <AdminAuthContext.Provider value={value}>
      <Outlet />
    </AdminAuthContext.Provider>
  );
}

export function useAdminAuth(): AdminAuthContextValue {
  const context = useContext(AdminAuthContext);
  if (!context) {
    throw new Error('useAdminAuth must be used inside an AdminAuthProvider');
  }
  return context;
}
