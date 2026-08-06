import { describe, expect, test } from 'vitest';
import { screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import type { Role } from '@/shared/auth/roles';
import type { Match } from '@/shared/types/domain';
import { FakePanelRepository } from '@/test/fakePanelRepository';
import { buildMatch, buildPanelState } from '@/test/fixtures';
import { renderWithProviders } from '@/test/renderWithProviders';
import { LeadsBulkBar } from './LeadsBulkBar';

/**
 * Bulk status changes (incl. archive) are owner/admin only — the bar's "Set
 * status" menu is gated on the `bulk_edit_leads` permission. Terminal statuses
 * (archived/closed/couldnt_connect) route through a shared-reason modal before
 * the bulk write fires.
 */

const SELECTED: readonly Match[] = [
  buildMatch({ id: 'c1', commentId: 'c1' }),
  buildMatch({ id: 'c2', commentId: 'c2' }),
];

function renderBarAs(role: Role) {
  const repository = new FakePanelRepository(buildPanelState());
  repository.currentUser = {
    id: 1,
    email: 'user@aizu.test',
    role,
    orgId: 1,
    org: { id: 1, name: 'Test Co', logo: null, description: null },
  };
  renderWithProviders(<LeadsBulkBar selectedLeads={SELECTED} onClear={() => {}} />, {
    repository,
    route: '/leads',
    path: '/leads',
  });
  return repository;
}

describe('LeadsBulkBar role gating', () => {
  test('owner sees the Set status menu', async () => {
    renderBarAs('owner');
    expect(await screen.findByRole('button', { name: /Set status/ })).toBeInTheDocument();
  });

  test('admin sees the Set status menu', async () => {
    renderBarAs('admin');
    expect(await screen.findByRole('button', { name: /Set status/ })).toBeInTheDocument();
  });

  test('member cannot bulk-change status but can still export', async () => {
    renderBarAs('member');
    // Export (read) stays available for every role…
    expect(await screen.findByRole('button', { name: /Export/ })).toBeInTheDocument();
    // …but the status menu is gone.
    expect(screen.queryByRole('button', { name: /Set status/ })).not.toBeInTheDocument();
  });

  test('viewer cannot bulk-change status', async () => {
    renderBarAs('viewer');
    expect(await screen.findByRole('button', { name: /Export/ })).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: /Set status/ })).not.toBeInTheDocument();
  });
});

describe('LeadsBulkBar status changes', () => {
  test('a non-terminal status writes immediately with no reason', async () => {
    const user = userEvent.setup();
    const repository = renderBarAs('owner');

    await user.click(await screen.findByRole('button', { name: /Set status/ }));
    await user.click(await screen.findByRole('menuitem', { name: /In Progress/ }));

    await waitFor(() => { expect(repository.bulkWrites).toHaveLength(1); });
    expect(repository.bulkWrites[0]).toMatchObject({ status: 'in_progress' });
    expect(repository.bulkWrites[0]?.note).toBeUndefined();
    expect(repository.bulkWrites[0]?.items.map((i) => i.commentId)).toEqual(['c1', 'c2']);
  });

  test('archive opens a reason modal and writes the shared reason', async () => {
    const user = userEvent.setup();
    const repository = renderBarAs('owner');

    await user.click(await screen.findByRole('button', { name: /Set status/ }));
    await user.click(await screen.findByRole('menuitem', { name: /Archived/ }));

    // The terminal move is held until a reason is supplied — no write yet.
    expect(repository.bulkWrites).toHaveLength(0);

    await user.type(
      await screen.findByPlaceholderText(/Why are these leads/),
      'end of campaign cleanup',
    );
    await user.click(screen.getByRole('button', { name: /Move 2 leads/ }));

    await waitFor(() => { expect(repository.bulkWrites).toHaveLength(1); });
    expect(repository.bulkWrites[0]).toMatchObject({
      status: 'archived',
      note: 'end of campaign cleanup',
    });
  });
});
