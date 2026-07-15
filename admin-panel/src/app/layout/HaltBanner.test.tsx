import { describe, expect, test } from 'vitest';
import { render, screen } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import { AppProviders } from '@/app/providers';
import { buildAlert, buildPanelState } from '@/test/fixtures';
import { FakePanelRepository } from '@/test/fakePanelRepository';
import type { PanelState } from '@/shared/types/domain';
import { HaltBanner } from './HaltBanner';

function renderBanner(overrides: Partial<PanelState> = {}) {
  const repository = new FakePanelRepository(buildPanelState(overrides));
  render(
    <AppProviders repository={repository}>
      <MemoryRouter>
        <HaltBanner />
      </MemoryRouter>
    </AppProviders>,
  );
}

describe('HaltBanner', () => {
  test('renders nothing while the engine is operational', async () => {
    renderBanner();
    // Let the async panel state settle, then confirm no alert surfaced.
    await Promise.resolve();
    expect(screen.queryByRole('alert')).not.toBeInTheDocument();
  });

  test('View health deep-links to the Reports system-health tile, not the old /health route', async () => {
    const base = buildPanelState();
    renderBanner({
      HEALTH: { ...base.HEALTH, overall: 'halted' },
      ALERTS: [buildAlert({ tier: 'halt', title: 'Checkpoint Challenge', desc: 'manual login required' })],
    });
    const link = await screen.findByRole('link', { name: 'View health' });
    expect(link).toHaveAttribute('href', '/reports#system-health');
  });
});
