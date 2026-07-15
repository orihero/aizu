import { describe, expect, test, vi } from 'vitest';
import { render, screen, waitFor } from '@testing-library/react';
import { MemoryRouter, Route, Routes } from 'react-router-dom';
import { AppProviders } from '@/app/providers';
import { buildPanelState } from '@/test/fixtures';
import { FakePanelRepository } from '@/test/fakePanelRepository';
import { HEALTH_ANCHOR_ID } from './healthAnchor';
import { ReportsPage } from './ReportsPage';

function renderReports(initialEntry: string) {
  const repository = new FakePanelRepository(buildPanelState());
  render(
    <AppProviders repository={repository}>
      <MemoryRouter initialEntries={[initialEntry]}>
        <Routes>
          <Route path="/reports" element={<ReportsPage />} />
        </Routes>
      </MemoryRouter>
    </AppProviders>,
  );
}

describe('ReportsPage system-health deep link', () => {
  test('scrolls to and highlights the health tile when arriving via #system-health', async () => {
    const scrollIntoView = vi.fn();
    Element.prototype.scrollIntoView = scrollIntoView;

    renderReports(`/reports#${HEALTH_ANCHOR_ID}`);

    await waitFor(() => { expect(scrollIntoView).toHaveBeenCalled(); });
    const tile = document.getElementById(HEALTH_ANCHOR_ID);
    expect(tile?.className).toContain('ring-offset-2');
  });

  test('leaves the health tile unhighlighted on a plain visit', async () => {
    const scrollIntoView = vi.fn();
    Element.prototype.scrollIntoView = scrollIntoView;

    renderReports('/reports');

    await screen.findByText('System health');
    expect(scrollIntoView).not.toHaveBeenCalled();
    const tile = document.getElementById(HEALTH_ANCHOR_ID);
    expect(tile?.className).not.toContain('ring-offset-2');
  });
});
