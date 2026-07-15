import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { useState, type ReactNode } from 'react';
import { RepositoryProvider } from '@/shared/api/repositoryContext';
import { AuthProvider } from '@/shared/hooks/useAuth';
import { ThemeProvider } from '@/shared/hooks/useTheme';
import type { PanelRepository } from '@/shared/api/panelRepository';

interface AppProvidersProps {
  readonly children: ReactNode;
  /** Test seam — production uses the HTTP repository by default. */
  readonly repository?: PanelRepository;
}

const QUERY_RETRIES = 1;

export function AppProviders({ children, repository }: AppProvidersProps) {
  const [queryClient] = useState(
    () =>
      new QueryClient({
        defaultOptions: {
          queries: { retry: QUERY_RETRIES, refetchOnWindowFocus: true },
        },
      }),
  );
  return (
    <ThemeProvider>
      <QueryClientProvider client={queryClient}>
        <RepositoryProvider {...(repository ? { repository } : {})}>
          <AuthProvider>{children}</AuthProvider>
        </RepositoryProvider>
      </QueryClientProvider>
    </ThemeProvider>
  );
}
