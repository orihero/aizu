import { useState } from 'react';
import { History } from 'lucide-react';
import { Card, CardBody, CardHeader } from '@/shared/ui/Card';
import { Badge, type BadgeTone } from '@/shared/ui/Badge';
import { EmptyState } from '@/shared/ui/EmptyState';
import { Drawer } from '@/shared/ui/Drawer';
import { formatMoney, formatNumber } from '@/shared/lib/formatters';
import { useRunActivity } from '@/shared/hooks/useRunActivity';
import type { Session } from '@/shared/types/domain';
import { RunActivityFeed } from './RunActivityFeed';

type SessionFlag = Session['flag'];

// One run's status flag → a badge tone + label. Maps the engine's session status
// (see panel.py `_session_flag`): '' = ran to completion, 'live' = in flight,
// 'halted' = stopped by a guard, 'resumed' = continued a prior session.
const FLAG_META: Readonly<Record<SessionFlag, { tone: BadgeTone; label: string }>> = {
  '': { tone: 'success', label: 'Completed' },
  live: { tone: 'info', label: 'Running' },
  halted: { tone: 'danger', label: 'Halted' },
  resumed: { tone: 'neutral', label: 'Resumed' },
};

const ROW_BASE =
  'w-full rounded-tile border border-border bg-surface-2/40 p-3 text-left';

function RunMetrics({ session }: { readonly session: Session }) {
  return (
    <div className="mt-1.5 flex flex-wrap items-center gap-x-3 gap-y-1 text-[11px] text-text-muted">
      <span>{session.durationMin} min</span>
      <span>
        <strong className="text-text">{formatNumber(session.matches)}</strong> leads
      </span>
      <span>{formatNumber(session.reelsSeen)} reels</span>
      <span>{formatMoney(session.spendUsd)}</span>
    </div>
  );
}

function RunRow({
  session,
  onOpen,
}: {
  readonly session: Session;
  readonly onOpen: (session: Session) => void;
}) {
  const meta = FLAG_META[session.flag];
  const header = (
    <div className="flex items-center justify-between gap-2">
      <span className="text-[13px] font-bold text-text">
        {session.date} · {session.start}
      </span>
      <Badge tone={meta.tone}>{meta.label}</Badge>
    </div>
  );

  // Only a run correlated to a RunManager run has a recorded event log to open.
  if (session.runId === null) {
    return (
      <li className={ROW_BASE}>
        {header}
        <RunMetrics session={session} />
        <span className="mt-1.5 block text-[10px] font-semibold uppercase tracking-wide text-text-faint">
          No log available
        </span>
      </li>
    );
  }

  return (
    <li>
      <button
        type="button"
        onClick={() => { onOpen(session); }}
        className={`${ROW_BASE} transition hover:-translate-y-px hover:border-brand/40 hover:bg-surface-2 hover:shadow-tile`}
      >
        {header}
        <RunMetrics session={session} />
      </button>
    </li>
  );
}

interface CampaignRunHistoryProps {
  readonly sessions: readonly Session[];
}

/**
 * Recent runs of one campaign, shown beside the campaign details. Each session
 * is one discovery run; newest is on top. A run with a known `runId` opens a
 * drawer with its recorded step-by-step activity feed (`/api/run/activity`).
 */
export function CampaignRunHistory({ sessions }: CampaignRunHistoryProps) {
  const [selected, setSelected] = useState<Session | null>(null);
  // `_build_sessions` returns oldest-first; reverse a copy so newest is on top
  // without mutating the prop array.
  const ordered = [...sessions].reverse();
  const { activity, isError } = useRunActivity(selected?.runId ?? null);

  return (
    <>
      <Card>
        <CardHeader
          title={<><History className="size-4 text-text-muted" aria-hidden /> Recent runs</>}
          subtitle={sessions.length > 0 ? `${sessions.length} total` : undefined}
        />
        <CardBody>
          {ordered.length === 0 ? (
            <EmptyState
              icon={History}
              title="No runs yet"
              description="Run this campaign to see its history here."
            />
          ) : (
            <ul className="flex max-h-[70vh] flex-col gap-2 overflow-y-auto">
              {ordered.map((session) => (
                <RunRow key={session.id} session={session} onOpen={setSelected} />
              ))}
            </ul>
          )}
        </CardBody>
      </Card>

      <Drawer
        isOpen={selected !== null}
        onClose={() => { setSelected(null); }}
        title={
          <div>
            <div className="text-[13px] font-bold text-text">Run log</div>
            {selected ? (
              <div className="text-xs text-text-muted">
                {selected.date} · {selected.start}
              </div>
            ) : null}
          </div>
        }
      >
        <RunActivityFeed activity={activity} isError={isError} />
      </Drawer>
    </>
  );
}
