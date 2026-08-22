import type { PreflightFailure, PreflightSummary } from '@/shared/schemas/admin';

/**
 * Operator-facing copy for the worker launch preflight (engine `worker/preflight.py`).
 *
 * The wire carries `{id, severity, status, detail}` and deliberately NOT `title`/`remedy`
 * (spec §5.1): shipping them would double the register body and, worse, would put the text
 * an admin reads under the control of the worker that is reporting the problem. So the
 * console resolves both locally, from the id, here.
 *
 * `detail` is the one worker-authored string in play. It is rendered as TEXT, never markup
 * — this is the superadmin console, which is the worst possible place to relearn the
 * E1/E2/F18 lesson about an off-cloud caller's string reaching a privileged surface.
 */

/** Human title per check id. `login.<platform>` is the one open-ended shape. */
const TITLES: Readonly<Record<string, string>> = {
  state_dir_writable: 'Worker state directory is writable',
  token_persistence: 'Worker identity can be stored',
  dispatch_credential: 'Enrolment credential present',
  capabilities: 'Platforms this box advertises',
  llm_backend: 'LLM backend for live runs',
  playwright: 'Playwright is available',
  cdp_reachable: 'Warmed Chrome is reachable',
  cdp_port_drift: 'CDP port matches the running Chrome',
  cdp_attachable: 'Chrome accepts a DevTools attach',
  chrome_profile: 'Chrome profile directory',
  preflight_error: 'The preflight could not complete',
};

/**
 * What to actually do about it. Only used for rows the box genuinely CHECKED and found
 * broken — see `remedyFor`, which withholds these on an `unknown` row.
 */
const REMEDIES: Readonly<Record<string, string>> = {
  state_dir_writable:
    'AIZU_WORKER_STATE points at a directory this worker cannot write. Fix the path or its permissions — the machine id, the single-flight locks and the encrypted token blob all live there.',
  token_persistence:
    'This box cannot store its worker identity, so it re-registers (and rotates its token) on every restart. Set AIZU_SECRET_KEY in its worker-secrets.env, or AIZU_TOKEN_BACKEND=keyring on a box whose keychain is pre-authorised.',
  dispatch_credential:
    'The box has no usable enrolment credential. Mint a per-worker token here (Fleet → Add worker) and paste it into the desktop app: Setup → Connect.',
  capabilities:
    'This box advertises no platforms, so the fleet can never dispatch a job to it — and it is why the tenant readiness banner cannot count it. Set AIZU_WORKER_PLATFORMS=all (or e.g. instagram,x,linkedin) on the box.',
  llm_backend:
    'No LLM backend on this box: every live run would fail at setup and dead-letter at attempt 5. Set OPENROUTER_API_KEY (or AIZU_LLM_BASE_URL for a local model). A box that ONLY runs warming jobs can instead set AIZU_WORKER_WARMING_ONLY=1, which turns this row amber — do not set it on a box that also leases harvest work.',
  playwright:
    'Playwright is missing from the worker interpreter, so no CDP job can run. Reinstall the worker package with its browser extra — this is broken packaging, not a config mistake.',
  cdp_reachable:
    'Nothing answers CDP at the configured port. If the detail names another port, repoint AIZU_CDP_URL (or cdp_port in the desktop config) at the Chrome that is actually running; otherwise start the warmed Chrome. The box re-checks every 30s and resumes on its own.',
  cdp_port_drift:
    'The worker adopted a Chrome on the other well-known port. Pin it so this cannot drift again: set cdp_port in the desktop config (Setup → Chrome), or AIZU_CDP_URL for a hand-run sidecar.',
  cdp_attachable:
    'Chrome answers on the CDP port but refuses a DevTools attach — the "degraded Chrome" case, where every job nacks on a box that looks perfectly healthy. Quit that Chrome completely (do not just reload a tab) and relaunch the warmed profile.',
  chrome_profile:
    'This box still holds the Chrome profile it warmed before aizu gave each browser brand its own profile directory. Nothing launches it any more — the box signs in fresh under AIZU_CHROME_PROFILE/<brand> — which is why a box that was signed in last week can read as signed out. It has been left exactly as it was: only someone at the box knows which browser warmed it, and opening it with the other brand DELETES every saved login in it, so aizu never guesses. The remedy is on the box: move that directory into the matching per-brand subdirectory (the worker logs both candidate paths), or sign in again and ignore it. Nothing is blocked either way, which is why the row is amber and not red.',
  preflight_error:
    'The preflight itself raised, so this box is running UNBLOCKED at full capabilities. Treat the health of this box as unverified until the next report arrives.',
};

const LOGIN_PREFIX = 'login.';

/** `login.instagram` → `instagram`, else null. */
function loginPlatform(id: string): string | null {
  return id.startsWith(LOGIN_PREFIX) ? id.slice(LOGIN_PREFIX.length) || null : null;
}

export function titleFor(id: string): string {
  const platform = loginPlatform(id);
  if (platform) return `Chrome is signed in to ${platform}`;
  return TITLES[id] ?? id;
}

/**
 * The remedy, or null when we should not offer one.
 *
 * `failed[]` holds every non-passing row, which mixes two very different things:
 *   - `fail`    — the box checked and it is broken. The remedy applies.
 *   - `unknown` — the box could NOT check (Chrome down, Playwright missing, version skew).
 *
 * Keying remedy on the id alone collapses those, and the collapse is not academic: the
 * commonest red state of all is Chrome being down, which marks every `login.*` row
 * `unknown`. Rendering "finish the login in the real Chrome" there would send an admin to
 * fix a login that was never the problem, on a PC nobody can SSH into. So an `unknown` row
 * gets no instruction — its `detail` already says why the check could not run, and the
 * check that IS red is the one carrying the real remedy.
 */
export function remedyFor(row: PreflightFailure): string | null {
  if (row.status === 'unknown') return null;
  const platform = loginPlatform(row.id);
  if (platform) {
    return `Chrome on that box is not signed in to ${platform} (or the session expired). On the box: Setup → Sign in → "Open login tab" for ${platform}, then finish the login and any 2FA in the real Chrome window.`;
  }
  return REMEDIES[row.id] ?? null;
}

/** `unknown` reads as "could not check", never as a verdict. */
export function statusLabel(status: PreflightFailure['status']): string {
  return status === 'unknown' ? 'could not check' : status;
}

export type HealthTone = 'success' | 'warn' | 'danger' | 'neutral';

export interface HealthSummary {
  readonly tone: HealthTone;
  readonly label: string;
  /** Long-form hover text; null when there is nothing more to say. */
  readonly title: string | null;
}

/**
 * The Health cell's verdict.
 *
 * A `null` summary is "—" / unknown, NEVER green: a box whose preflight the server has
 * never stored (a pre-v23 sidecar, or a report dropped for being malformed) has not been
 * cleared of anything, and this whole feature exists because boxes that read healthy could
 * not work.
 */
export function healthSummary(preflight: PreflightSummary): HealthSummary {
  if (!preflight) {
    return { tone: 'neutral', label: '—', title: 'No preflight reported by this worker yet.' };
  }
  if (preflight.blocking) {
    const firstFatal = preflight.failed.find((r) => r.severity === 'fatal');
    return {
      tone: 'danger',
      label: firstFatal ? `parked · ${firstFatal.id}` : 'parked',
      title: 'This box is registered and visible but takes no work until its failing checks clear. It re-checks every 30s and resumes on its own.',
    };
  }
  // Enforcement off is its own amber state: a fleet quietly running unenforced is exactly
  // the invisible condition this work exists to abolish (§8.7).
  if (!preflight.enforced) {
    return {
      tone: 'warn',
      label: 'unenforced',
      title: 'AIZU_PREFLIGHT_ENFORCE=0 on this box — fatal checks are reported but do NOT park it. Unset it once the box is fixed.',
    };
  }
  if (!preflight.ok) {
    const n = preflight.failed.length;
    return {
      tone: 'warn',
      label: `${n} ${n === 1 ? 'warning' : 'warnings'}`,
      title: 'This box can take work, but some checks did not pass.',
    };
  }
  return { tone: 'success', label: 'ready', title: null };
}
