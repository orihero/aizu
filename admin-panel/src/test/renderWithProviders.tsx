import { render } from '@testing-library/react';
import { MemoryRouter, Route, Routes } from 'react-router-dom';
import type { ReactElement } from 'react';
import { AppProviders } from '@/app/providers';
import type { PanelRepository } from '@/shared/api/panelRepository';

interface RenderOptions {
  readonly repository: PanelRepository;
  readonly route?: string;
  readonly path?: string;
}

export function renderWithProviders(
  ui: ReactElement,
  { repository, route = '/', path = '/' }: RenderOptions,
) {
  return render(
    <AppProviders repository={repository}>
      <MemoryRouter initialEntries={[route]}>
        <Routes>
          <Route path={path} element={ui} />
        </Routes>
      </MemoryRouter>
    </AppProviders>,
  );
}
