import { useMutation, useQueryClient } from '@tanstack/react-query';
import { usePanelRepository } from '@/shared/api/repositoryContext';
import { unwrap } from '@/shared/lib/result';
import { useAuth } from '@/shared/hooks/useAuth';
import {
  invalidateLeadSurfaces,
  patchLeadsPages,
  restoreLeadsPages,
  type LeadsSnapshot,
} from '@/shared/hooks/leadsCache';
import type {
  AddLeadNoteInput,
  AuthUser,
  DeleteLeadNoteInput,
  LeadNote,
  Match,
} from '@/shared/types/domain';

/** A locally-created note awaiting server reconciliation carries a temp- id. */
export function isTempNoteId(id: string): boolean {
  return id.startsWith('temp-');
}

function withNoteAppended(items: readonly Match[], input: AddLeadNoteInput, note: LeadNote): Match[] {
  return items.map((m) =>
    m.commentId === input.commentId &&
    m.campaignId === input.campaignId &&
    (input.platform === undefined || m.platform === input.platform)
      ? { ...m, notes: [...m.notes, note] }
      : m,
  );
}

function withNoteRemoved(items: readonly Match[], noteId: string): Match[] {
  return items.map((m) => ({ ...m, notes: m.notes.filter((n) => n.id !== noteId) }));
}

function optimisticNote(input: AddLeadNoteInput, user: AuthUser | null): LeadNote {
  return {
    id: `temp-${crypto.randomUUID()}`,
    body: input.body,
    authorEmail: user?.email ?? null,
    authorId: user?.id ?? null,
    createdAt: 'just now',
    // Sort newest-last in the timeline until the server timestamp lands.
    createdAtTs: Date.now() / 1000,
  };
}

/**
 * Add a free-form note to a lead. Optimistically appends it (attributed to the
 * signed-in user with a temp id) across every cached leads page, rolls back on failure,
 * and re-syncs from the server — which assigns the real id and timestamp.
 */
export function useAddLeadNote() {
  const repository = usePanelRepository();
  const queryClient = useQueryClient();
  const { user } = useAuth();

  return useMutation({
    mutationFn: async (input: AddLeadNoteInput) => { unwrap(await repository.addLeadNote(input)); },
    onMutate: async (input): Promise<{ snapshot: LeadsSnapshot }> => {
      const note = optimisticNote(input, user);
      const snapshot = await patchLeadsPages(queryClient, (items) => withNoteAppended(items, input, note));
      return { snapshot };
    },
    onError: (_error, _input, context) => {
      if (context) restoreLeadsPages(queryClient, context.snapshot);
    },
    onSettled: () => { invalidateLeadSurfaces(queryClient); },
  });
}

/** Delete one of the current user's own notes (server enforces author-only). */
export function useDeleteLeadNote() {
  const repository = usePanelRepository();
  const queryClient = useQueryClient();

  return useMutation({
    mutationFn: async (input: DeleteLeadNoteInput) => { unwrap(await repository.deleteLeadNote(input)); },
    onMutate: async (input): Promise<{ snapshot: LeadsSnapshot }> => {
      const snapshot = await patchLeadsPages(queryClient, (items) => withNoteRemoved(items, input.noteId));
      return { snapshot };
    },
    onError: (_error, _input, context) => {
      if (context) restoreLeadsPages(queryClient, context.snapshot);
    },
    onSettled: () => { invalidateLeadSurfaces(queryClient); },
  });
}
