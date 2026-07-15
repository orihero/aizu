import { useState } from 'react';
import { Button } from '@/shared/ui/Button';
import { Field, INPUT_CLASS } from '@/features/settings/SettingsField';
import {
  useStartTelegramLogin,
  useToggleIntegration,
  useVerifyTelegramLogin,
} from '@/shared/hooks/useWriteMutations';
import type { Integration } from '@/shared/types/domain';
import { IntegrationCardShell } from './IntegrationCardShell';
import { needsReconnect } from './integrationStatus';

type Step = 'phone' | 'code';

/** Telegram has no Bot-API path to third-party channels, so connecting means a
 * one-time MTProto user login: phone → code (+ 2FA password). The code step is a
 * Telegram anti-abuse control and can't be skipped; everything after produces a
 * reusable session stored server-side. */
export function TelegramConnectCard({ integration }: { readonly integration: Integration }) {
  const start = useStartTelegramLogin();
  const verify = useVerifyTelegramLogin();
  const disconnect = useToggleIntegration();

  const [step, setStep] = useState<Step>('phone');
  const [phone, setPhone] = useState('');
  const [code, setCode] = useState('');
  const [password, setPassword] = useState('');
  const [token, setToken] = useState('');
  const [needsPassword, setNeedsPassword] = useState(false);

  const reset = () => {
    setStep('phone'); setCode(''); setPassword(''); setToken(''); setNeedsPassword(false);
  };

  const sendCode = () => {
    const p = phone.trim();
    if (!p) return;
    start.mutate(p, {
      onSuccess: (res) => { setToken(res.token); setStep('code'); },
    });
  };

  const submitCode = () => {
    if (!code.trim()) return;
    verify.mutate(
      { token, code: code.trim(), ...(password.trim() ? { password: password.trim() } : {}) },
      // 2FA on → reveal the password field and stay on the code step; else done.
      { onSuccess: (res) => { if (res.needsPassword) { setNeedsPassword(true); } else { reset(); } } },
    );
  };

  if (integration.connected && !needsReconnect(integration)) {
    return (
      <IntegrationCardShell integration={integration}>
        <div className="mt-auto flex items-center gap-3 pt-1">
          <Button
            variant="ghost"
            onClick={() => { disconnect.mutate({ integration, connected: false }); }}
            disabled={disconnect.isPending}
          >
            {disconnect.isPending ? 'Saving…' : 'Disconnect'}
          </Button>
          {disconnect.isError ? (
            <span className="text-xs font-medium text-danger">
              {disconnect.error instanceof Error ? disconnect.error.message : 'Update failed.'}
            </span>
          ) : null}
        </div>
      </IntegrationCardShell>
    );
  }

  return (
    <IntegrationCardShell integration={integration}>
      {step === 'phone' ? (
        <>
          <Field label="Phone number" htmlFor="tg-phone" hint="Include the country code, e.g. +14155550142.">
            <input
              id="tg-phone"
              type="tel"
              placeholder="+1…"
              className={INPUT_CLASS}
              value={phone}
              onChange={(e) => { setPhone(e.target.value); }}
              disabled={start.isPending}
            />
          </Field>
          <div className="mt-auto flex items-center gap-3 pt-1">
            <Button onClick={sendCode} disabled={start.isPending || !phone.trim()}>
              {start.isPending ? 'Sending…' : 'Send code'}
            </Button>
            {start.isError ? (
              <span className="text-xs font-medium text-danger">
                {start.error instanceof Error ? start.error.message : 'Could not send the code.'}
              </span>
            ) : null}
          </div>
        </>
      ) : (
        <>
          <Field label="Login code" htmlFor="tg-code" hint="Telegram just sent this to your app.">
            <input
              id="tg-code"
              type="text"
              inputMode="numeric"
              autoComplete="one-time-code"
              placeholder="12345"
              className={INPUT_CLASS}
              value={code}
              onChange={(e) => { setCode(e.target.value); }}
              disabled={verify.isPending}
            />
          </Field>
          {needsPassword ? (
            <Field label="Two-factor password" htmlFor="tg-password" hint="Your Telegram cloud password.">
              <input
                id="tg-password"
                type="password"
                autoComplete="off"
                className={INPUT_CLASS}
                value={password}
                onChange={(e) => { setPassword(e.target.value); }}
                disabled={verify.isPending}
              />
            </Field>
          ) : null}
          <div className="mt-auto flex items-center gap-3 pt-1">
            <Button onClick={submitCode} disabled={verify.isPending || !code.trim()}>
              {verify.isPending ? 'Verifying…' : 'Verify'}
            </Button>
            <Button variant="ghost" onClick={reset} disabled={verify.isPending}>
              Back
            </Button>
            {verify.isError ? (
              <span className="text-xs font-medium text-danger">
                {verify.error instanceof Error ? verify.error.message : 'Verification failed.'}
              </span>
            ) : null}
          </div>
        </>
      )}
    </IntegrationCardShell>
  );
}
