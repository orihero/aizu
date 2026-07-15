import { Activity, KeyRound, Rss, ShieldAlert } from 'lucide-react';
import type { LucideIcon } from 'lucide-react';
import { Badge, type BadgeTone } from '@/shared/ui/Badge';
import { Card, CardBody, CardHeader } from '@/shared/ui/Card';
import { cn } from '@/shared/lib/cn';
import { formatPercent } from '@/shared/lib/formatters';
import type { Health } from '@/shared/types/domain';

interface SystemHealthTileProps {
  readonly health: Health;
  /** DOM id so the halt banner can deep-link straight to this tile. */
  readonly id?: string;
  /** Brief ring pulse applied when arriving via the deep link. */
  readonly isHighlighted?: boolean;
}

const OPERATIONAL_STATES: ReadonlySet<string> = new Set(['operational', 'ok', 'healthy', 'running']);

/** Free-text indicator states map onto our badge tones via a keyword check. */
function stateTone(state: string): BadgeTone {
  const value = state.toLowerCase();
  if (OPERATIONAL_STATES.has(value)) return 'success';
  if (value.includes('halt') || value.includes('block') || value.includes('fail')) return 'danger';
  return 'warn';
}

interface IndicatorRowProps {
  readonly icon: LucideIcon;
  readonly name: string;
  readonly state: string;
}

function IndicatorRow({ icon: Icon, name, state }: IndicatorRowProps) {
  return (
    <div className="flex items-center justify-between gap-2 border-b border-border/60 py-2 last:border-0">
      <span className="flex items-center gap-2 text-xs text-text-muted">
        <Icon className="size-3.5" aria-hidden />
        {name}
      </span>
      <Badge tone={stateTone(state)}>{state}</Badge>
    </div>
  );
}

/** Compact rollup of engine health: overall pill, feed skip ratio, key signals. */
export function SystemHealthTile({ health, id, isHighlighted = false }: SystemHealthTileProps) {
  const isOperational = OPERATIONAL_STATES.has(health.overall.toLowerCase());
  return (
    <Card
      id={id}
      className={cn(
        'scroll-mt-24 transition-shadow duration-500 lg:col-span-2',
        isHighlighted && 'ring-2 ring-brand ring-offset-2 ring-offset-bg',
      )}
    >
      <CardHeader
        title="System health"
        subtitle="engine status this period"
        actions={
          <Badge tone={isOperational ? 'success' : 'danger'}>
            <Activity className="size-3" aria-hidden />
            {health.overall}
          </Badge>
        }
      />
      <CardBody>
        <div className="mb-3 flex items-baseline gap-2">
          <span className="font-head text-[28px] font-extrabold leading-none tabular">
            {formatPercent(health.feed.skipRatio)}
          </span>
          <span className="text-xs font-medium text-text-faint">feed skip ratio</span>
        </div>
        <div>
          <IndicatorRow icon={KeyRound} name="Login" state={health.login.state} />
          <IndicatorRow icon={ShieldAlert} name="Action block" state={health.actionBlock.state} />
          <IndicatorRow
            icon={Rss}
            name="Feed"
            state={health.feed.flagged ? 'flagged' : 'steady'}
          />
        </div>
      </CardBody>
    </Card>
  );
}
