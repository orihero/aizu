import { describe, expect, test, vi } from 'vitest';
import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { buildMatch, buildPanelState } from '@/test/fixtures';
import { FakePanelRepository } from '@/test/fakePanelRepository';
import { renderWithProviders } from '@/test/renderWithProviders';
import { ThemeProvider } from '@/shared/hooks/useTheme';
import { LeadsPage } from '../LeadsPage';
import { ReasonMoveModal } from './ReasonMoveModal';

describe('LeadBoard view', () => {
  test('switching to Board view renders all six pipeline columns', async () => {
    const user = userEvent.setup();
    const repo = new FakePanelRepository(
      buildPanelState({ MATCHES: [buildMatch({ commentId: 'c1', status: 'new' })] }),
    );
    renderWithProviders(<LeadsPage />, { repository: repo, route: '/leads', path: '/leads' });

    await user.click(await screen.findByRole('button', { name: 'Board view' }));

    for (const label of ['New', 'In Progress', 'Interested', 'Closed', "Couldn't Connect", 'Archived']) {
      expect(screen.getByRole('region', { name: `${label} column` })).toBeInTheDocument();
    }
  });
});

describe('ReasonMoveModal', () => {
  const pending = { lead: buildMatch({ intent: 'Wants a demo next week' }), target: 'closed' as const };

  function renderModal(overrides: Partial<Parameters<typeof ReasonMoveModal>[0]> = {}) {
    const onConfirm = vi.fn();
    const onCancel = vi.fn();
    render(
      <ThemeProvider>
        <ReasonMoveModal
          pending={pending}
          onConfirm={onConfirm}
          onCancel={onCancel}
          isSubmitting={false}
          {...overrides}
        />
      </ThemeProvider>,
    );
    return { onConfirm, onCancel };
  }

  test('confirm is disabled until a non-empty reason is entered', async () => {
    const user = userEvent.setup();
    const { onConfirm } = renderModal();

    const confirm = screen.getByRole('button', { name: 'Confirm move' });
    expect(confirm).toBeDisabled();

    await user.type(screen.getByRole('textbox'), '   '); // whitespace only stays disabled
    expect(confirm).toBeDisabled();

    await user.type(screen.getByRole('textbox'), 'no budget');
    expect(confirm).toBeEnabled();
    await user.click(confirm);
    expect(onConfirm).toHaveBeenCalledWith('no budget');
  });

  test('cancel aborts without confirming', async () => {
    const user = userEvent.setup();
    const { onCancel, onConfirm } = renderModal();
    await user.click(screen.getByRole('button', { name: 'Cancel' }));
    expect(onCancel).toHaveBeenCalledOnce();
    expect(onConfirm).not.toHaveBeenCalled();
  });
});
