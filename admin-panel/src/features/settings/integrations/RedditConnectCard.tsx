import { useState } from 'react';
import { Button } from '@/shared/ui/Button';
import { Field, INPUT_CLASS } from '@/features/settings/SettingsField';
import { useConnectReddit, useToggleIntegration } from '@/shared/hooks/useWriteMutations';
import type { Integration } from '@/shared/types/domain';
import { IntegrationCardShell } from './IntegrationCardShell';
import { needsReconnect } from './integrationStatus';

/** Reddit self-serve connect: the customer pastes their app's client_id +
 * client_secret + a descriptive user_agent, the server mints an app-only OAuth
 * token (client_credentials) to validate, then stores them encrypted per-org.
 * Reading public data needs only this app token, so it is a single-step connect
 * (no redirect dance). */
export function RedditConnectCard({ integration }: { readonly integration: Integration }) {
  const connect = useConnectReddit();
  const disconnect = useToggleIntegration();
  const [clientId, setClientId] = useState('');
  const [clientSecret, setClientSecret] = useState('');
  const [userAgent, setUserAgent] = useState('');
  const showForm = !integration.connected || needsReconnect(integration);

  const canSubmit = Boolean(clientId.trim() && clientSecret.trim() && userAgent.trim());
  const submit = () => {
    if (!canSubmit) return;
    connect.mutate(
      { clientId: clientId.trim(), clientSecret: clientSecret.trim(), userAgent: userAgent.trim() },
      { onSuccess: () => { setClientId(''); setClientSecret(''); setUserAgent(''); } },
    );
  };

  return (
    <IntegrationCardShell integration={integration}>
      {showForm ? (
        <>
          <Field
            label="Client ID"
            htmlFor="reddit-client-id"
            hint="From reddit.com/prefs/apps → your script/web app."
          >
            <input
              id="reddit-client-id"
              type="text"
              autoComplete="off"
              placeholder="abc123…"
              className={INPUT_CLASS}
              value={clientId}
              onChange={(e) => { setClientId(e.target.value); }}
              disabled={connect.isPending}
            />
          </Field>
          <Field label="Client Secret" htmlFor="reddit-client-secret">
            <input
              id="reddit-client-secret"
              type="password"
              autoComplete="off"
              placeholder="••••••••"
              className={INPUT_CLASS}
              value={clientSecret}
              onChange={(e) => { setClientSecret(e.target.value); }}
              disabled={connect.isPending}
            />
          </Field>
          <Field
            label="User Agent"
            htmlFor="reddit-user-agent"
            hint="A descriptive string, e.g. aizu:lead-agent:v1 (by /u/you)."
          >
            <input
              id="reddit-user-agent"
              type="text"
              autoComplete="off"
              placeholder="aizu:lead-agent:v1 (by /u/you)"
              className={INPUT_CLASS}
              value={userAgent}
              onChange={(e) => { setUserAgent(e.target.value); }}
              disabled={connect.isPending}
            />
          </Field>
          <div className="mt-auto flex items-center gap-3 pt-1">
            <Button onClick={submit} disabled={connect.isPending || !canSubmit}>
              {connect.isPending ? 'Validating…' : 'Connect'}
            </Button>
            {connect.isError ? (
              <span className="text-xs font-medium text-danger">
                {connect.error instanceof Error ? connect.error.message : 'Connection failed.'}
              </span>
            ) : null}
          </div>
        </>
      ) : (
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
      )}
    </IntegrationCardShell>
  );
}
