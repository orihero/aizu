import { useState } from 'react';
import { CalendarClock, Loader2 } from 'lucide-react';
import { Button } from '@/shared/ui/Button';
import { Drawer } from '@/shared/ui/Drawer';
import { cn } from '@/shared/lib/cn';
import { useScheduleCampaign } from '@/shared/hooks/useWriteMutations';
import { isScheduled } from '@/shared/selectors/campaigns';
import type { Campaign, ScheduleKind } from '@/shared/types/domain';

const KINDS: readonly { readonly key: ScheduleKind; readonly label: string }[] = [
  { key: 'daily', label: 'Daily' },
  { key: 'weekdays', label: 'Weekdays' },
  { key: 'weekly', label: 'Weekly' },
];

const DOW = [
  { value: 0, label: 'Mon' }, { value: 1, label: 'Tue' }, { value: 2, label: 'Wed' },
  { value: 3, label: 'Thu' }, { value: 4, label: 'Fri' }, { value: 5, label: 'Sat' },
  { value: 6, label: 'Sun' },
] as const;

interface ScheduleDialogProps {
  readonly campaign: Campaign;
  readonly isOpen: boolean;
  readonly onClose: () => void;
}

function errorMessage(error: unknown): string {
  return error instanceof Error ? error.message : 'Something went wrong.';
}

/**
 * Arm/replace or clear a campaign's recurring run schedule. Fixed cadence only
 * (Daily / Weekdays / Weekly + HH:MM) — no raw cron. The server is authoritative
 * for the next fire; this just collects the cadence.
 */
export function ScheduleDialog({ campaign, isOpen, onClose }: ScheduleDialogProps) {
  const schedule = useScheduleCampaign();
  const [kind, setKind] = useState<ScheduleKind>(
    (campaign.scheduleKind || 'daily') as ScheduleKind,
  );
  const [dow, setDow] = useState<number>(campaign.scheduleDow ?? 0);
  const [time, setTime] = useState(
    `${String(campaign.scheduleHour ?? 9).padStart(2, '0')}:${String(campaign.scheduleMinute ?? 0).padStart(2, '0')}`,
  );

  const [hourStr = '', minStr = ''] = time.split(':');
  const hh = Number.parseInt(hourStr, 10);
  const mm = Number.parseInt(minStr, 10);
  const timeValid =
    Number.isInteger(hh) && Number.isInteger(mm) && hh >= 0 && hh <= 23 && mm >= 0 && mm <= 59;

  const onSave = () => {
    if (!timeValid) return;
    schedule.mutate(
      {
        campaignId: campaign.id, enabled: true, kind, hour: hh, minute: mm,
        ...(kind === 'weekly' ? { dow } : {}),
      },
      { onSuccess: onClose },
    );
  };

  const onClear = () => {
    schedule.mutate({ campaignId: campaign.id, enabled: false }, { onSuccess: onClose });
  };

  const pill = (active: boolean) =>
    cn(
      'rounded-full px-3.5 py-1.5 text-xs font-bold transition active:scale-95',
      active ? 'bg-accent text-accent-ink' : 'bg-surface-2 text-text hover:bg-border',
    );

  const footer = (
    <>
      {isScheduled(campaign) ? (
        <Button variant="ghost" onClick={onClear} disabled={schedule.isPending}>
          Clear schedule
        </Button>
      ) : (
        <Button variant="ghost" onClick={onClose}>Cancel</Button>
      )}
      <Button onClick={onSave} disabled={!timeValid || schedule.isPending}>
        {schedule.isPending
          ? <Loader2 className="size-3.5 animate-spin" aria-hidden />
          : <CalendarClock className="size-3.5" aria-hidden />}
        Save schedule
      </Button>
    </>
  );

  const title = (
    <>
      <h2 className="truncate font-head text-lg font-bold">Schedule {campaign.name}</h2>
      <p className="text-xs font-medium text-text-faint">Recurring run · fixed cadence, no cron</p>
    </>
  );

  return (
    <Drawer
      isOpen={isOpen}
      onClose={onClose}
      title={title}
      footer={footer}
    >
      <div className="space-y-4">
        <div>
          <span className="text-xs font-bold uppercase tracking-wide text-text-faint">Repeat</span>
          <div className="mt-2 flex flex-wrap gap-2">
            {KINDS.map(({ key, label }) => (
              <button key={key} type="button" onClick={() => { setKind(key); }} className={pill(kind === key)}>
                {label}
              </button>
            ))}
          </div>
        </div>

        {kind === 'weekly' ? (
          <div>
            <span className="text-xs font-bold uppercase tracking-wide text-text-faint">Day</span>
            <div className="mt-2 flex flex-wrap gap-2">
              {DOW.map(({ value, label }) => (
                <button key={value} type="button" onClick={() => { setDow(value); }} className={pill(dow === value)}>
                  {label}
                </button>
              ))}
            </div>
          </div>
        ) : null}

        <div>
          <label htmlFor="schedule-time" className="text-xs font-bold uppercase tracking-wide text-text-faint">
            Time (Asia/Tashkent)
          </label>
          <input
            id="schedule-time"
            type="time"
            value={time}
            onChange={(e) => { setTime(e.target.value); }}
            className="mt-1 block w-36 rounded-tile border border-border bg-surface px-3 py-2 text-sm tabular-nums outline-none focus:border-accent"
          />
          {!timeValid ? (
            <p className="mt-1 text-xs font-medium text-danger">Enter a valid time (HH:MM).</p>
          ) : (
            <p className="mt-1 text-xs text-text-faint">
              Runs at this local time. It still respects the 8am–9pm daily window and the warmth gate.
            </p>
          )}
        </div>

        {schedule.isError ? (
          <p className="text-xs font-medium text-danger">{errorMessage(schedule.error)}</p>
        ) : null}
      </div>
    </Drawer>
  );
}
