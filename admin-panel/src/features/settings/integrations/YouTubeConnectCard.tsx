import { useState } from 'react';
import { Button } from '@/shared/ui/Button';
import { Field, INPUT_CLASS } from '@/features/settings/SettingsField';
import { useConnectYouTube, useToggleIntegration } from '@/shared/hooks/useWriteMutations';
import type { Integration } from '@/shared/types/domain';
import { IntegrationCardShell } from './IntegrationCardShell';
import { needsReconnect } from './integrationStatus';

/** YouTube self-serve connect: the customer pastes their Data API key, the server
 * validates it with one live call, then stores it encrypted per-org. Reading
 * public data needs no OAuth, so a key is enough for v1. */
export function YouTubeConnectCard({ integration }: { readonly integration: Integration }) {
  const connect = useConnectYouTube();
  const disconnect = useToggleIntegration();
  const [apiKey, setApiKey] = useState('');
  const showForm = !integration.connected || needsReconnect(integration);

  const submit = () => {
    const key = apiKey.trim();
    if (!key) return;
    connect.mutate(key, { onSuccess: () => { setApiKey(''); } });
  };

  return (
    <IntegrationCardShell integration={integration}>
      {showForm ? (
        <>
          <Field
            label="YouTube Data API key"
            htmlFor="yt-api-key"
            hint="Find it in Google Cloud Console → APIs & Services → Credentials."
          >
            <input
              id="yt-api-key"
              type="password"
              autoComplete="off"
              placeholder="AIza…"
              className={INPUT_CLASS}
              value={apiKey}
              onChange={(e) => { setApiKey(e.target.value); }}
              disabled={connect.isPending}
            />
          </Field>
          <div className="mt-auto flex items-center gap-3 pt-1">
            <Button onClick={submit} disabled={connect.isPending || !apiKey.trim()}>
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
