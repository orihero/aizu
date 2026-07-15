import { useState, type ReactNode } from 'react';
import { useNavigate } from 'react-router-dom';
import { AlertCircle, Loader2 } from 'lucide-react';
import { cn } from '@/shared/lib/cn';
import type { Result } from '@/shared/lib/result';
import { roleHome } from '@/shared/auth/roles';
import type { AuthUser } from '@/shared/types/domain';
import { AUTH_INPUT_CLASS, AUTH_INPUT_ERROR_CLASS } from './authStyles';
import { PasswordField } from './PasswordField';

// Mirrors the bridge's server-side rules so the client fails fast with a clear
// message before a round-trip (the server re-validates regardless).
const EMAIL_RE = /^[^@\s]+@[^@\s]+\.[^@\s]+$/;
const MIN_PASSWORD_LENGTH = 8;

/** What the form collects — company fields only apply to self-serve signup. */
export interface AuthFormValues {
  readonly email: string;
  readonly password: string;
  readonly companyName?: string;
  readonly companyLogo?: string;
  readonly companyDescription?: string;
}

interface AuthFormProps {
  readonly mode: 'login' | 'signup';
  readonly heading: string;
  readonly subheading: string;
  readonly submitLabel: string;
  readonly onSubmit: (values: AuthFormValues) => Promise<Result<AuthUser>>;
  readonly footer: ReactNode;
  /** Render the company name/logo/description fields (self-serve signup only). */
  readonly showCompanyFields?: boolean;
  /** Pre-filled, read-only email (an invite that pins the address). */
  readonly fixedEmail?: string;
}

/** The shared auth form behind the login, signup and invite-accept pages. */
export function AuthForm({
  mode,
  heading,
  subheading,
  submitLabel,
  onSubmit,
  footer,
  showCompanyFields = false,
  fixedEmail,
}: AuthFormProps) {
  const navigate = useNavigate();
  const [email, setEmail] = useState(fixedEmail ?? '');
  const [password, setPassword] = useState('');
  const [companyName, setCompanyName] = useState('');
  const [companyLogo, setCompanyLogo] = useState('');
  const [companyDescription, setCompanyDescription] = useState('');
  const [emailError, setEmailError] = useState<string>();
  const [passwordError, setPasswordError] = useState<string>();
  const [companyError, setCompanyError] = useState<string>();
  const [formError, setFormError] = useState<string>();
  const [submitting, setSubmitting] = useState(false);

  function validate(): boolean {
    let valid = true;
    const trimmed = email.trim();
    if (!trimmed) {
      setEmailError('Email is required');
      valid = false;
    } else if (!EMAIL_RE.test(trimmed)) {
      setEmailError('Enter a valid email address');
      valid = false;
    } else {
      setEmailError(undefined);
    }
    if (!password) {
      setPasswordError('Password is required');
      valid = false;
    } else if (mode === 'signup' && password.length < MIN_PASSWORD_LENGTH) {
      setPasswordError(`Password must be at least ${MIN_PASSWORD_LENGTH} characters`);
      valid = false;
    } else {
      setPasswordError(undefined);
    }
    if (showCompanyFields && !companyName.trim()) {
      setCompanyError('Company name is required');
      valid = false;
    } else {
      setCompanyError(undefined);
    }
    return valid;
  }

  async function submit() {
    setFormError(undefined);
    if (!validate()) return;
    setSubmitting(true);
    const values: AuthFormValues = { email: email.trim().toLowerCase(), password };
    if (showCompanyFields) {
      Object.assign(values, {
        companyName: companyName.trim(),
        ...(companyLogo.trim() ? { companyLogo: companyLogo.trim() } : {}),
        ...(companyDescription.trim() ? { companyDescription: companyDescription.trim() } : {}),
      });
    }
    const result = await onSubmit(values);
    setSubmitting(false);
    if (result.ok) {
      void navigate(roleHome(result.value.role), { replace: true });
      return;
    }
    setFormError(result.error.message);
  }

  return (
    <form
      onSubmit={(event) => {
        event.preventDefault();
        void submit();
      }}
      noValidate
      className="flex flex-col gap-5"
    >
      <div className="flex flex-col gap-1.5">
        <h1 className="font-head text-xl font-extrabold tracking-tight text-text">{heading}</h1>
        <p className="text-[13px] text-text-muted">{subheading}</p>
      </div>

      {formError ? (
        <div
          role="alert"
          className="flex items-start gap-2 rounded-xl border border-danger/30 bg-danger-soft px-3 py-2.5 text-[12.5px] font-medium text-danger"
        >
          <AlertCircle className="mt-px size-4 shrink-0" aria-hidden />
          <span>{formError}</span>
        </div>
      ) : null}

      <div className="flex flex-col gap-1.5">
        <label htmlFor="auth-email" className="text-xs font-semibold text-text-muted">
          Email
        </label>
        <input
          id="auth-email"
          type="email"
          value={email}
          onChange={(event) => {
            setEmail(event.target.value);
          }}
          autoComplete="email"
          inputMode="email"
          placeholder="you@company.com"
          disabled={submitting || fixedEmail !== undefined}
          readOnly={fixedEmail !== undefined}
          aria-invalid={emailError ? true : undefined}
          aria-describedby={emailError ? 'auth-email-error' : undefined}
          className={cn(AUTH_INPUT_CLASS, emailError && AUTH_INPUT_ERROR_CLASS)}
        />
        {emailError ? (
          <p id="auth-email-error" className="text-[11px] font-medium text-danger">
            {emailError}
          </p>
        ) : null}
      </div>

      <PasswordField
        id="auth-password"
        value={password}
        onChange={setPassword}
        autoComplete={mode === 'signup' ? 'new-password' : 'current-password'}
        {...(passwordError ? { error: passwordError } : {})}
        {...(mode === 'signup' ? { hint: 'At least 8 characters.' } : {})}
        disabled={submitting}
      />

      {showCompanyFields ? (
        <div className="flex flex-col gap-4 border-t border-border pt-4">
          <div className="flex flex-col gap-1.5">
            <label htmlFor="auth-company" className="text-xs font-semibold text-text-muted">
              Company name
            </label>
            <input
              id="auth-company"
              type="text"
              value={companyName}
              onChange={(event) => {
                setCompanyName(event.target.value);
              }}
              placeholder="Acme Inc."
              disabled={submitting}
              aria-invalid={companyError ? true : undefined}
              aria-describedby={companyError ? 'auth-company-error' : undefined}
              className={cn(AUTH_INPUT_CLASS, companyError && AUTH_INPUT_ERROR_CLASS)}
            />
            {companyError ? (
              <p id="auth-company-error" className="text-[11px] font-medium text-danger">
                {companyError}
              </p>
            ) : null}
          </div>
          <div className="flex flex-col gap-1.5">
            <label htmlFor="auth-logo" className="text-xs font-semibold text-text-muted">
              Logo URL <span className="font-normal text-text-faint">(optional)</span>
            </label>
            <input
              id="auth-logo"
              type="url"
              value={companyLogo}
              onChange={(event) => {
                setCompanyLogo(event.target.value);
              }}
              placeholder="https://…/logo.png"
              disabled={submitting}
              className={AUTH_INPUT_CLASS}
            />
          </div>
          <div className="flex flex-col gap-1.5">
            <label htmlFor="auth-desc" className="text-xs font-semibold text-text-muted">
              What does your company do? <span className="font-normal text-text-faint">(optional)</span>
            </label>
            <textarea
              id="auth-desc"
              value={companyDescription}
              onChange={(event) => {
                setCompanyDescription(event.target.value);
              }}
              placeholder="A short description…"
              rows={2}
              disabled={submitting}
              className={cn(AUTH_INPUT_CLASS, 'resize-none')}
            />
          </div>
        </div>
      ) : null}

      <button
        type="submit"
        disabled={submitting}
        aria-busy={submitting}
        className="inline-flex w-full items-center justify-center gap-2 rounded-xl bg-brand px-4 py-2.5 text-sm font-bold text-on-brand shadow-tile transition hover:bg-brand-deep active:scale-[0.99] disabled:cursor-not-allowed disabled:opacity-60"
      >
        {submitting ? <Loader2 className="size-4 animate-spin" aria-hidden /> : null}
        {submitLabel}
      </button>

      <p className="text-center text-[13px] text-text-muted">{footer}</p>
    </form>
  );
}
