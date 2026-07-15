import { useState } from 'react';
import { useNavigate } from 'react-router-dom';
import { ShieldCheck } from 'lucide-react';
import { useAdminAuth } from './useAdminAuth';

/** Map the admin-login failure to a human sentence (the server keeps creds opaque). */
function describeLoginError(status: number | undefined, message: string): string {
  if (status === 429) return 'Too many attempts. Locked out — try again in a few minutes.';
  if (status === 403) return 'This network is not allowed to reach the admin console.';
  if (status === 401) return 'Invalid email, password, or authenticator code.';
  return message || 'Login failed. Please try again.';
}

/**
 * Standalone login for the SEPARATE superadmin plane — email + password + a
 * 6-digit TOTP code. Not the org AuthLayout: the admin subtree is fully
 * independent of the org session.
 */
export function AdminLoginPage() {
  const { login } = useAdminAuth();
  const navigate = useNavigate();
  const [email, setEmail] = useState('');
  const [password, setPassword] = useState('');
  const [totpCode, setTotpCode] = useState('');
  const [error, setError] = useState<string | null>(null);
  const [submitting, setSubmitting] = useState(false);

  async function submit() {
    setError(null);
    setSubmitting(true);
    const result = await login({ email: email.trim(), password, totpCode: totpCode.trim() });
    setSubmitting(false);
    if (result.ok) {
      void navigate('/admin', { replace: true });
      return;
    }
    const status = result.error.kind === 'http' ? result.error.status : undefined;
    setError(describeLoginError(status, result.error.message));
  }

  return (
    <div className="flex min-h-full items-center justify-center bg-bg px-4 py-12">
      <div className="w-full max-w-[400px]">
        <div className="mb-7 flex flex-col items-center gap-3 text-center">
          <span className="flex size-12 items-center justify-center rounded-full bg-brand/10">
            <ShieldCheck className="size-6 text-brand" aria-hidden />
          </span>
          <span className="font-head text-lg font-extrabold tracking-tight text-text">
            AIZU · Superadmin
          </span>
        </div>
        <form
          onSubmit={(e) => {
            e.preventDefault();
            void submit();
          }}
          className="rounded-card border border-border bg-surface p-7 shadow-lift"
        >
          <h1 className="font-head text-xl font-extrabold tracking-tight text-text">
            Platform admin sign-in
          </h1>
          <p className="mt-1 text-[13px] text-text-muted">
            Requires an allowlisted network and your authenticator code.
          </p>

          {error ? (
            <div
              role="alert"
              className="mt-4 rounded-tile bg-danger-soft px-3 py-2 text-[13px] font-medium text-danger"
            >
              {error}
            </div>
          ) : null}

          <label className="mt-4 block text-xs font-semibold text-text-muted" htmlFor="admin-email">
            Email
          </label>
          <input
            id="admin-email"
            type="email"
            autoComplete="username"
            required
            value={email}
            onChange={(e) => { setEmail(e.target.value); }}
            className="mt-1 w-full rounded-tile border border-border bg-bg px-3 py-2 text-sm text-text outline-none focus:border-brand"
          />

          <label
            className="mt-4 block text-xs font-semibold text-text-muted"
            htmlFor="admin-password"
          >
            Password
          </label>
          <input
            id="admin-password"
            type="password"
            autoComplete="current-password"
            required
            value={password}
            onChange={(e) => { setPassword(e.target.value); }}
            className="mt-1 w-full rounded-tile border border-border bg-bg px-3 py-2 text-sm text-text outline-none focus:border-brand"
          />

          <label className="mt-4 block text-xs font-semibold text-text-muted" htmlFor="admin-totp">
            Authenticator code
          </label>
          <input
            id="admin-totp"
            type="text"
            inputMode="numeric"
            autoComplete="one-time-code"
            pattern="\d{6}"
            maxLength={6}
            required
            placeholder="123456"
            value={totpCode}
            onChange={(e) => { setTotpCode(e.target.value.replace(/\D/g, '')); }}
            className="mt-1 w-full rounded-tile border border-border bg-bg px-3 py-2 text-sm tracking-widest text-text outline-none focus:border-brand"
          />

          <button
            type="submit"
            disabled={submitting}
            className="mt-6 inline-flex w-full items-center justify-center gap-1.5 rounded-full bg-accent px-4 py-2 text-sm font-bold text-accent-ink transition hover:shadow-lift active:scale-95 disabled:cursor-not-allowed disabled:opacity-50"
          >
            {submitting ? 'Signing in…' : 'Sign in'}
          </button>
        </form>
      </div>
    </div>
  );
}
