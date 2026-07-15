import { useAuth } from '@/shared/hooks/useAuth';
import { can, type Action, type Role } from '@/shared/auth/roles';

/** The signed-in user's role, or null when anonymous. */
export function useRole(): Role | null {
  return useAuth().user?.role ?? null;
}

/**
 * Whether the current user may perform `action` (mirrors the server gate).
 * UI-only: the bridge re-enforces every action — this just hides/disables controls.
 */
export function useCan(action: Action): boolean {
  return can(useRole(), action);
}
