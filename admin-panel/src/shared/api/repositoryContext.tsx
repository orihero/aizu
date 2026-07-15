import { createContext, useContext, type ReactNode } from 'react';
import type { PanelRepository } from './panelRepository';
import { HttpPanelRepository } from './httpPanelRepository';

const RepositoryContext = createContext<PanelRepository | null>(null);

interface RepositoryProviderProps {
  readonly repository?: PanelRepository;
  readonly children: ReactNode;
}

const defaultRepository = new HttpPanelRepository();

export function RepositoryProvider({ repository, children }: RepositoryProviderProps) {
  return (
    <RepositoryContext.Provider value={repository ?? defaultRepository}>
      {children}
    </RepositoryContext.Provider>
  );
}

export function usePanelRepository(): PanelRepository {
  const repository = useContext(RepositoryContext);
  if (!repository) {
    throw new Error('usePanelRepository must be used inside a RepositoryProvider');
  }
  return repository;
}
