import { useState } from 'react';
import { ExternalLink, Trash2 } from 'lucide-react';
import { Avatar } from '@/shared/ui/Avatar';
import { Button } from '@/shared/ui/Button';
import { Drawer } from '@/shared/ui/Drawer';
import { LeadDetails } from '@/shared/ui/LeadDetails';
import { ScorePill } from '@/shared/ui/ScorePill';
import { useSetMatchStatus } from '@/shared/hooks/useSetMatchStatus';
import { useAddLeadNote, useDeleteLeadNote, isTempNoteId } from '@/shared/hooks/useLeadNotes';
import { useAuth } from '@/shared/hooks/useAuth';
import { useCan } from '@/shared/hooks/useCan';
import { reelUrl } from '@/shared/lib/reelUrl';
import {
  LEAD_STATUS_LABEL,
  LEAD_STATUS_ORDER,
  isTerminalStatus,
  selectLeadTimeline,
} from '@/shared/selectors/leads';
import type { Match, MatchStatus } from '@/shared/types/domain';
import { LeadStatusPill } from './LeadStatusPill';
import { PlatformChip } from './PlatformChip';
import { ReasonMoveModal, type PendingMove } from './board/ReasonMoveModal';

interface LeadDrawerProps {
  readonly lead: Match | null;
  readonly threshold: number;
  readonly onClose: () => void;
}

function Section({ title, children }: { readonly title: string; readonly children: React.ReactNode }) {
  return (
    <section className="mb-5">
      <h3 className="mb-2 text-[10px] font-bold uppercase tracking-wider text-text-faint">{title}</h3>
      {children}
    </section>
  );
}

/**
 * Link out to the source reel on its platform. Falls back to the plain reel id
 * when the platform exposes no derivable per-reel URL (e.g. Telegram).
 */
function ReelLink({ platform, reelId }: { readonly platform: string; readonly reelId: string }) {
  const href = reelUrl(platform, reelId);
  if (!href) {
    return <span className="font-mono text-[11px] text-text-muted">{reelId}</span>;
  }
  return (
    <a
      href={href}
      target="_blank"
      rel="noopener noreferrer"
      className="inline-flex max-w-full items-center gap-1 font-mono text-[11px] text-brand hover:underline"
    >
      <span className="truncate">{reelId}</span>
      <ExternalLink className="size-3 shrink-0" aria-hidden />
    </a>
  );
}

/** The merged status-change + note activity feed, oldest first. */
function ActivityTimeline({ lead }: { readonly lead: Match }) {
  const { user } = useAuth();
  const deleteNote = useDeleteLeadNote();
  const items = selectLeadTimeline(lead);

  if (items.length === 0) {
    return <p className="text-xs italic text-text-faint">No activity yet.</p>;
  }

  return (
    <ol className="space-y-2">
      {items.map((item) =>
        item.kind === 'status' ? (
          <li key={`s-${item.ts}-${item.change.toStatus}`} className="text-xs">
            <span className="font-semibold text-text">
              {item.change.fromStatus ? `${LEAD_STATUS_LABEL[item.change.fromStatus]} → ` : ''}
              {LEAD_STATUS_LABEL[item.change.toStatus]}
            </span>
            <span className="text-text-faint">
              {' · '}{item.change.by ?? 'system'}{' · '}{item.change.at}
            </span>
            {item.change.note ? (
              <p className="mt-0.5 rounded-card bg-surface-2 px-2 py-1 text-[11px] text-text-muted">
                {item.change.note}
              </p>
            ) : null}
          </li>
        ) : (
          <li key={`n-${item.note.id}`} className="rounded-card border border-border bg-surface px-2.5 py-1.5 text-xs">
            <div className="flex items-start justify-between gap-2">
              <p className="min-w-0 text-text">{item.note.body}</p>
              {user && item.note.authorId === user.id && !isTempNoteId(item.note.id) ? (
                <button
                  type="button"
                  aria-label={`Delete note from ${item.note.createdAt}`}
                  onClick={() => { deleteNote.mutate({ noteId: item.note.id }); }}
                  className="shrink-0 text-text-faint transition hover:text-danger"
                >
                  <Trash2 className="size-3.5" aria-hidden />
                </button>
              ) : null}
            </div>
            <p className="mt-0.5 text-[10px] text-text-faint">
              {item.note.authorEmail ?? 'unknown'} · {item.note.createdAt}
            </p>
          </li>
        ),
      )}
    </ol>
  );
}

/** Add-note composer — any lead-editor can post a note. */
function NoteComposer({ lead }: { readonly lead: Match }) {
  const addNote = useAddLeadNote();
  const [body, setBody] = useState('');
  const trimmed = body.trim();

  const submit = () => {
    if (!trimmed) return;
    addNote.mutate({ campaignId: lead.campaignId, commentId: lead.commentId, platform: lead.platform, body: trimmed });
    setBody('');
  };

  return (
    <div className="mt-2">
      <textarea
        value={body}
        onChange={(e) => { setBody(e.target.value); }}
        onKeyDown={(e) => { if ((e.metaKey || e.ctrlKey) && e.key === 'Enter') submit(); }}
        rows={2}
        placeholder="Add a note…"
        className="w-full resize-none rounded-card border border-border bg-surface px-3 py-2 text-xs outline-none transition-colors focus:border-brand/50"
      />
      <div className="mt-1.5 flex justify-end">
        <Button type="button" variant="default" onClick={submit} disabled={trimmed === '' || addNote.isPending}>
          Add note
        </Button>
      </div>
    </div>
  );
}

export function LeadDrawer({ lead, threshold, onClose }: LeadDrawerProps) {
  const setStatus = useSetMatchStatus();
  const canEdit = useCan('edit_leads');
  // A terminal status picked from the drawer waits here for its reason note.
  const [pendingStatus, setPendingStatus] = useState<MatchStatus | null>(null);

  const applyStatus = (status: MatchStatus) => {
    if (!lead || status === lead.status) return;
    if (isTerminalStatus(status)) {
      setPendingStatus(status);
      return;
    }
    setStatus.mutate({ campaignId: lead.campaignId, commentId: lead.commentId, platform: lead.platform, status });
  };

  const confirmTerminal = (reason: string) => {
    if (!lead || !pendingStatus) return;
    setStatus.mutate({
      campaignId: lead.campaignId,
      commentId: lead.commentId,
      platform: lead.platform,
      status: pendingStatus,
      note: reason,
    });
    setPendingStatus(null);
  };

  return (
    <>
      <Drawer
        isOpen={lead !== null}
        onClose={onClose}
        title={
          lead ? (
            <div className="flex items-center gap-3">
              <Avatar username={lead.username} size="lg" />
              <div className="min-w-0">
                <div className="truncate text-sm font-semibold">{lead.username}</div>
                <div className="mt-0.5 flex flex-wrap gap-1.5">
                  <PlatformChip platform={lead.platform} />
                  <LeadStatusPill status={lead.status} />
                </div>
              </div>
            </div>
          ) : null
        }
        footer={
          lead && canEdit ? (
            <label className="flex w-full items-center gap-2 text-xs font-semibold text-text-muted">
              Set status
              <select
                value={lead.status}
                onChange={(e) => { applyStatus(e.target.value as MatchStatus); }}
                className="grow rounded-xl border border-border bg-surface px-2.5 py-1.5 text-xs font-medium text-text outline-none transition-colors focus:border-brand/50"
                aria-label="Set lead status"
              >
                {LEAD_STATUS_ORDER.map((status) => (
                  <option key={status} value={status}>
                    {LEAD_STATUS_LABEL[status]}
                  </option>
                ))}
              </select>
            </label>
          ) : null
        }
      >
        {lead ? (
          <>
            <Section title="Comment">
              <blockquote className="rounded-lg border-l-2 border-brand bg-surface-2 px-3 py-2 text-sm">
                {lead.text}
              </blockquote>
            </Section>

            <Section title="Why it matched">
              <div className="flex items-center gap-2">
                <ScorePill score={lead.score} threshold={threshold} />
                <span className="text-xs text-text-muted">{lead.reason}</span>
              </div>
            </Section>

            <Section title="Lead details">
              <LeadDetails extracted={lead.extracted} />
            </Section>

            <Section title="Activity">
              <ActivityTimeline lead={lead} />
              {canEdit ? <NoteComposer lead={lead} /> : null}
            </Section>

            <Section title="Source">
              <dl className="space-y-1.5 text-xs">
                <div className="flex items-baseline justify-between gap-3">
                  <dt className="shrink-0 text-text-muted">Campaign</dt>
                  <dd className="font-mono text-[11px]">{lead.campaignId}</dd>
                </div>
                <div className="flex items-baseline justify-between gap-3">
                  <dt className="shrink-0 text-text-muted">Captured</dt>
                  <dd className="tabular-nums">
                    {lead.capturedAt.date} {lead.capturedAt.time}
                  </dd>
                </div>
                <div className="flex items-baseline justify-between gap-3">
                  <dt className="shrink-0 text-text-muted">comment_id</dt>
                  <dd className="font-mono text-[11px]">{lead.commentId}</dd>
                </div>
                <div className="flex items-baseline justify-between gap-3">
                  <dt className="shrink-0 text-text-muted">Reel</dt>
                  <dd className="min-w-0 text-right">
                    <ReelLink platform={lead.platform} reelId={lead.reelId} />
                  </dd>
                </div>
              </dl>
            </Section>

            {setStatus.isError ? (
              <p role="alert" className="rounded-lg bg-danger-soft px-3 py-2 text-xs text-danger">
                Status write failed: {setStatus.error.message}
              </p>
            ) : null}
          </>
        ) : null}
      </Drawer>

      <ReasonMoveModal
        pending={lead && pendingStatus ? ({ lead, target: pendingStatus } satisfies PendingMove) : null}
        onCancel={() => { setPendingStatus(null); }}
        onConfirm={confirmTerminal}
        isSubmitting={setStatus.isPending}
      />
    </>
  );
}
