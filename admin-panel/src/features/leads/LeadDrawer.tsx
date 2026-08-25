import { useCallback, useEffect, useRef, useState } from 'react';
import { Link } from 'react-router-dom';
import { Eye, ExternalLink, Trash2 } from 'lucide-react';
import { Button } from '@/shared/ui/Button';
import { Drawer } from '@/shared/ui/Drawer';
import { LeadDetails } from '@/shared/ui/LeadDetails';
import { ScorePill } from '@/shared/ui/ScorePill';
import { usePanelRepository } from '@/shared/api/repositoryContext';
import { useSetMatchStatus } from '@/shared/hooks/useSetMatchStatus';
import { useAddLeadNote, useDeleteLeadNote, isTempNoteId } from '@/shared/hooks/useLeadNotes';
import { useAuth } from '@/shared/hooks/useAuth';
import { useCan } from '@/shared/hooks/useCan';
import { cn } from '@/shared/lib/cn';
import { reelUrl } from '@/shared/lib/reelUrl';
import {
  LEAD_INTENT_PLACEHOLDER,
  LEAD_STATUS_LABEL,
  LEAD_STATUS_ORDER,
  isTerminalStatus,
  leadIntentLabel,
  selectLeadTimeline,
} from '@/shared/selectors/leads';
import type { AppError } from '@/shared/lib/result';
import type { Match, MatchStatus, RevealedLead } from '@/shared/types/domain';
import { describeRevealError } from './describeRevealError';
import { LeadStatusPill } from './LeadStatusPill';
import { PlatformChip } from './PlatformChip';
import { ReasonMoveModal, type PendingMove } from './board/ReasonMoveModal';

/** Where the reveal's plan-limit upgrade CTA points (mirrors `RunDrawer`). */
const BILLING_PATH = '/settings/billing';

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

/* ---- Section F: reveal-on-demand ---------------------------------------- */

/**
 * What the drawer currently knows about ONE lead's raw identity.
 *
 * This lives in component state and NOWHERE else: it is not a React Query key, it is
 * never folded back into the cached leads pages, and it never reaches localStorage.
 * Closing the drawer forgets it and reopening re-reveals — which re-audits. Cache it
 * anywhere and "anonymized by default" quietly decays into "anonymized until first
 * viewed", which is not the promise the product makes.
 */
type RevealState =
  | { readonly kind: 'hidden' }
  | { readonly kind: 'revealing' }
  | { readonly kind: 'revealed'; readonly source: RevealedLead }
  // The typed AppError, not a flattened string: the reveal's 402 (the period reveal
  // allowance is spent) has to be told apart from its 403/404/transport failures by
  // STATUS, and a message string cannot carry one.
  | { readonly kind: 'failed'; readonly error: AppError };

function useLeadReveal(lead: Match | null) {
  const repository = usePanelRepository();
  const [state, setState] = useState<RevealState>({ kind: 'hidden' });

  // The lead the drawer is showing RIGHT NOW, readable from an async continuation:
  // a reveal that resolves after the operator moved on must be dropped, not painted
  // onto whichever lead happens to be on screen.
  const leadId = lead?.id ?? null;
  const activeIdRef = useRef<string | null>(leadId);
  activeIdRef.current = leadId;

  // Re-hide on every lead change AND on close (lead -> null): each open starts hidden.
  useEffect(() => { setState({ kind: 'hidden' }); }, [leadId]);

  const reveal = useCallback(async () => {
    if (!lead) return;
    setState({ kind: 'revealing' });
    const result = await repository.revealLead({
      campaignId: lead.campaignId,
      platform: lead.platform,
      commentId: lead.commentId,
    });
    if (activeIdRef.current !== lead.id) return; // answered for a lead we left
    if (!result.ok) {
      setState({ kind: 'failed', error: result.error });
      return;
    }
    // The bridge echoes the composite lead uid back. Refuse an answer that is not for
    // the lead we asked about rather than trusting response ordering.
    if (result.value.id !== lead.id) {
      setState({
        kind: 'failed',
        error: { kind: 'unknown', message: 'The reveal answered for a different lead.' },
      });
      return;
    }
    setState({ kind: 'revealed', source: result.value });
  }, [lead, repository]);

  return { state, reveal };
}

/**
 * A refused reveal. The period reveal allowance (402) gets plain copy and the upgrade
 * link — the same treatment the run gate's 402 gets, and deliberately not a generic
 * "Reveal failed", which would read as a broken button rather than a plan that ran out.
 * The role/ownership refusals keep the server's own wording and get no upgrade CTA.
 */
function RevealError({ error }: { readonly error: AppError }) {
  const described = describeRevealError(error);
  return (
    <div
      role="alert"
      className={cn(
        'mt-2 space-y-1 rounded-lg px-3 py-2 text-xs',
        described.upgrade ? 'bg-warn-soft text-warn' : 'bg-danger-soft text-danger',
      )}
    >
      <p className="font-medium">{described.message}</p>
      {described.detail ? <p className="text-text-faint">{described.detail}</p> : null}
      {described.upgrade ? (
        <Link to={BILLING_PATH} className="inline-block font-bold text-brand hover:underline">
          Upgrade plan →
        </Link>
      ) : null}
    </div>
  );
}

/**
 * The ONE place in the customer app where a lead's handle, their comment and the post
 * it sits on can appear — after an explicit, server-audited click, for this lead, for
 * as long as this drawer stays open. Viewers get no control (`reveal_lead` excludes
 * them) and the bridge refuses them regardless: UI gating is UX, the server is the gate.
 */
function RevealSection({ lead }: { readonly lead: Match }) {
  const canReveal = useCan('reveal_lead');
  const { state, reveal } = useLeadReveal(lead);

  if (state.kind === 'revealed') {
    return (
      <dl className="space-y-2 text-xs">
        <div className="flex items-baseline justify-between gap-3">
          <dt className="shrink-0 text-text-muted">Handle</dt>
          <dd className="min-w-0 truncate font-semibold">{state.source.username}</dd>
        </div>
        <div>
          <dt className="mb-1 text-text-muted">Comment</dt>
          <dd>
            <blockquote className="rounded-lg border-l-2 border-brand bg-surface-2 px-3 py-2 text-sm">
              {state.source.text}
            </blockquote>
          </dd>
        </div>
        <div className="flex items-baseline justify-between gap-3">
          <dt className="shrink-0 text-text-muted">Post</dt>
          <dd className="min-w-0 text-right">
            <ReelLink platform={state.source.platform} reelId={state.source.reelId} />
          </dd>
        </div>
      </dl>
    );
  }

  if (!canReveal) {
    return (
      <p className="text-xs text-text-faint">
        Leads are anonymized. Revealing who wrote one needs an owner, admin, or member
        account.
      </p>
    );
  }

  return (
    <>
      <p className="mb-2 text-xs text-text-muted">
        The handle, the comment, and the post stay hidden until you ask for them.
        Revealing is recorded against your account, and it is not stored — reopening this
        lead asks again.
      </p>
      <Button
        type="button"
        variant="ghost"
        onClick={() => { void reveal(); }}
        disabled={state.kind === 'revealing'}
      >
        <Eye className="size-3.5" aria-hidden />
        {state.kind === 'revealing' ? 'Revealing…' : 'Reveal source'}
      </Button>
      {state.kind === 'failed' ? <RevealError error={state.error} /> : null}
    </>
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
            // No avatar and no handle: the lead's own words are the title now.
            <div className="min-w-0">
              <div
                className={cn(
                  'line-clamp-2 text-sm font-semibold',
                  leadIntentLabel(lead) === LEAD_INTENT_PLACEHOLDER && 'font-normal italic text-text-faint',
                )}
              >
                {leadIntentLabel(lead)}
              </div>
              <div className="mt-1 flex flex-wrap gap-1.5">
                <PlatformChip platform={lead.platform} />
                <LeadStatusPill status={lead.status} />
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
            <Section title="Why it matched">
              <div className="flex items-center gap-2">
                <ScorePill score={lead.score} threshold={threshold} />
                <span className="text-xs text-text-muted">{lead.reason}</span>
              </div>
            </Section>

            <Section title="Lead details">
              <LeadDetails extracted={lead.extracted} />
            </Section>

            <Section title="Original comment">
              {/* Keyed on the lead id so switching leads remounts with a clean, hidden
                  state instead of carrying one lead's revealed identity into another. */}
              <RevealSection key={lead.id} lead={lead} />
            </Section>

            <Section title="Activity">
              <ActivityTimeline lead={lead} />
              {canEdit ? <NoteComposer lead={lead} /> : null}
            </Section>

            {/* Provenance only. The post link lives behind the reveal: a reel id is one
                hand-built URL away from the comment and the handle on it. */}
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
