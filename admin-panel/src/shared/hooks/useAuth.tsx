import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useState,
  type ReactNode,
} from 'react';
import { useQueryClient } from '@tanstack/react-query';
import { usePanelRepository } from '@/shared/api/repositoryContext';
import { ResultError, type Result } from '@/shared/lib/result';
import type { AuthUser, Credentials, SignupCredentials } from '@/shared/types/domain';

/** 'loading' until the cookie session is resolved on mount; then authed/anonymous. */
export type AuthStatus = 'loading' | 'authenticated' | 'anonymous';

interface AuthContextValue {
  readonly user: AuthUser | null;
  readonly status: AuthStatus;
  // Arrow-property signatures (not methods) so destructuring them from the hook
  // doesn't trip @typescript-eslint/unbound-method at every call site.
  readonly login: (credentials: Credentials) => Promise<Result<AuthUser>>;
  readonly signup: (credentials: SignupCredentials) => Promise<Result<AuthUser>>;
  readonly logout: () => Promise<void>;
}

const AuthContext = createContext<AuthContextValue | null>(null);

/**
 * Holds the signed-in user and bootstraps the session from the HttpOnly cookie
 * via GET /api/auth/me on mount. login/signup flip the status and refresh cached
 * queries; logout clears them so no previous-user data lingers.
 */
export function AuthProvider({ children }: { readonly children: ReactNode }) {
  const repository = usePanelRepository();
  const queryClient = useQueryClient();
  const [user, setUser] = useState<AuthUser | null>(null);
  const [status, setStatus] = useState<AuthStatus>('loading');

  useEffect(() => {
    let cancelled = false;
    void repository
      .getCurrentUser()
      .then((result) => {
        if (cancelled) return;
        if (result.ok && result.value) {
          setUser(result.value);
          setStatus('authenticated');
        } else {
          setUser(null);
          setStatus('anonymous');
        }
      })
      .catch(() => {
        // The repository contract returns a Result and shouldn't throw, but a
        // diverging implementation must never strand the app on 'loading'.
        if (!cancelled) {
          setUser(null);
          setStatus('anonymous');
        }
      });
    return () => {
      cancelled = true;
    };
  }, [repository]);

  // Mid-session expiry: once the app is running, a gated query that 401s means
  // the cookie died. Drop to anonymous so RequireAuth redirects to /login rather
  // than leaving the user stuck on a broken/stale view.
  useEffect(() => {
    const cache = queryClient.getQueryCache();
    return cache.subscribe((event) => {
      const error: unknown = event.query.state.error;
      if (error instanceof ResultError && error.appError.status === 401) {
        setUser(null);
        setStatus('anonymous');
      }
    });
  }, [queryClient]);

  const login = useCallback(
    async (credentials: Credentials) => {
      const result = await repository.login(credentials);
      if (result.ok) {
        setUser(result.value);
        setStatus('authenticated');
        await queryClient.invalidateQueries();
      }
      return result;
    },
    [repository, queryClient],
  );

  const signup = useCallback(
    async (credentials: SignupCredentials) => {
      const result = await repository.signup(credentials);
      if (result.ok) {
        setUser(result.value);
        setStatus('authenticated');
        await queryClient.invalidateQueries();
      }
      return result;
    },
    [repository, queryClient],
  );

  const logout = useCallback(async () => {
    await repository.logout();
    setUser(null);
    setStatus('anonymous');
    // Drop every cached query so the next session starts from a clean slate.
    queryClient.clear();
  }, [repository, queryClient]);

  const value = useMemo(
    () => ({ user, status, login, signup, logout }),
    [user, status, login, signup, logout],
  );

  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>;
}

export function useAuth(): AuthContextValue {
  const context = useContext(AuthContext);
  if (!context) {
    throw new Error('useAuth must be used inside an AuthProvider');
  }
  return context;
}
