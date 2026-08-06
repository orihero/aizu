import { RouterProvider } from 'react-router-dom';
import type { PanelRepository } from '@/shared/api/panelRepository';
import { AppProviders } from './providers';
import { router } from './router';

interface AppProps {
  /** Demo-capture seam — omitted in production (defaults to the HTTP repository). */
  readonly repository?: PanelRepository;
}

export function App({ repository }: AppProps = {}) {
  return (
    <AppProviders {...(repository ? { repository } : {})}>
      <RouterProvider router={router} />
    </AppProviders>
  );
}
