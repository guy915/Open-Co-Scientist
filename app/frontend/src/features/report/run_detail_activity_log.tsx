import type {RunEventActivity} from '@/shared/api/wire_common';
import {Icon, type IconName} from '@/shared/ui/icon';
import type {
  StreamConnectionState,
  StreamEvent,
} from '@/shared/hooks/use_run_stream';
import {capitalizeTerm} from '@/shared/lib/text';
import {joinClasses} from '@/shared/ui/classes';
import {relativeTime} from '@/shared/lib/time';

const TIMELINE_RAIL_CLASSES =
  'absolute left-[1.0625rem] top-[2.375rem] bottom-1 w-px bg-cosci-border';

function LivePulse() {
  return (
    <span className="relative flex size-2.5" aria-hidden="true">
      <span className="absolute inline-flex size-full motion-safe:animate-ping rounded-full bg-th-primary" />
      <span className="relative inline-flex size-2.5 rounded-full bg-th-primary" />
    </span>
  );
}

const STREAM_STATUS_LABEL: Record<
  Exclude<StreamConnectionState, 'open'>,
  string
> = {
  connecting: 'Connecting...',
  reconnecting: 'Reconnecting...',
  disconnected: 'Stream disconnected',
  capacity:
    'Live updates are busy. Close extra run tabs; we will retry shortly.',
};

// A non-open stream must not show the live pulse.
function StreamStatusDot({
  connection,
}: {
  connection: Exclude<StreamConnectionState, 'open'>;
}) {
  return (
    <span className="relative flex size-2.5" aria-hidden="true">
      <span
        className={joinClasses(
          'inline-flex size-2.5 rounded-full',
          connection === 'connecting' ? 'bg-cosci-muted' : 'bg-th-warning',
        )}
      />
    </span>
  );
}

// Expose degraded transport through role=status where the heading otherwise
// claims a live feed.
function StreamStatusNote({
  connection,
}: {
  connection: StreamConnectionState | undefined;
}) {
  if (connection === undefined || connection === 'open') return null;
  return (
    <span
      role="status"
      className={joinClasses(
        'text-xs',
        connection === 'connecting'
          ? 'text-cosci-muted'
          : 'text-th-on-warning-container',
      )}
    >
      {STREAM_STATUS_LABEL[connection]}
    </span>
  );
}

function ActivityDisc({
  icon,
  tone,
  isLatest,
}: {
  icon: IconName;
  tone: string;
  isLatest: boolean;
}) {
  return (
    <span
      className={joinClasses(
        'relative z-1 grid size-[2.125rem] shrink-0 place-items-center',
        'rounded-full',
        isLatest ? 'animate-pulse bg-th-primary' : 'bg-cosci-hover',
      )}
    >
      <Icon
        name={icon}
        className={joinClasses(
          'text-[1.15rem]',
          isLatest ? 'text-th-primary-fg' : tone,
        )}
      />
    </span>
  );
}

function ActivityItem({
  event,
  meta,
  isLatest,
  isLast,
  now,
}: {
  event: StreamEvent;
  meta: ActivityMeta;
  isLatest: boolean;
  isLast: boolean;
  now: number;
}) {
  const detail = activityDetail(event);
  return (
    <li className="relative flex gap-4 pb-6 last:pb-0">
      {!isLast && <span aria-hidden="true" className={TIMELINE_RAIL_CLASSES} />}
      <ActivityDisc icon={meta.icon} tone={meta.tone} isLatest={isLatest} />
      <div className="min-w-0 flex-1">
        {/* Sized to the disc and centred on its axis; both paragraphs zero
            their own user-agent margins or the row drifts off-centre. */}
        <div className="flex min-h-[2.125rem] items-center justify-between gap-3">
          <p className="my-0 truncate font-medium text-cosci-fg">
            {meta.title}
          </p>
          <span className="shrink-0 text-xs text-cosci-muted">
            {relativeTime(event.created_at, now)}
          </span>
        </div>
        {detail ? (
          <p className="mb-0 mt-0.5 line-clamp-2 text-sm text-cosci-muted">
            {detail}
          </p>
        ) : null}
      </div>
    </li>
  );
}

// A group collapses to its newest step; the newest accumulating group is
// ongoing rather than a stale relative age.
function ActivityGroupCard({
  group,
  isLatest,
  isLast,
  now,
}: {
  group: ActivityGroup;
  isLatest: boolean;
  isLast: boolean;
  now: number;
}) {
  const latest = group.events[group.events.length - 1];
  if (group.events.length === 1) {
    return (
      <ActivityItem
        event={latest}
        meta={group.meta}
        isLatest={isLatest}
        isLast={isLast}
        now={now}
      />
    );
  }
  const detail = activityDetail(latest);
  return (
    <li className="relative flex gap-4 pb-6 last:pb-0">
      {!isLast && <span aria-hidden="true" className={TIMELINE_RAIL_CLASSES} />}
      <ActivityDisc
        icon={group.meta.icon}
        tone={group.meta.tone}
        isLatest={isLatest}
      />
      <div className="min-w-0 flex-1">
        <div className="flex min-h-[2.125rem] items-center justify-between gap-3">
          <p className="my-0 truncate font-medium text-cosci-fg">
            {group.meta.title}
          </p>
          <span className="shrink-0 text-xs text-cosci-muted">
            {isLatest ? 'In progress' : relativeTime(latest.created_at, now)}
          </span>
        </div>
        {detail ? (
          <p className="mb-0 mt-0.5 line-clamp-2 text-sm text-cosci-muted">
            {detail}
          </p>
        ) : null}
      </div>
    </li>
  );
}

export function ActivityLog({
  groups,
  connection,
  nowSeconds,
}: {
  groups: ActivityGroup[];
  connection: StreamConnectionState | undefined;
  nowSeconds: number;
}) {
  return (
    <section aria-label="Activity log">
      <div className="flex items-center gap-2.5">
        {connection === 'open' || connection === undefined ? (
          <LivePulse />
        ) : (
          <StreamStatusDot connection={connection} />
        )}
        {/* `my-0`: an <h3>'s 1em user-agent block margin does not collapse
            inside this flex row, so it survived as 16px of padding above
            the heading. The section's own gap already spaces the cards
            from this log, and that extra 16px landed on one side of them
            only — 44px of white below the cards against 27px above. */}
        <h3 className="my-0 text-base font-medium">Live activity</h3>
        <StreamStatusNote connection={connection} />
      </div>
      {groups.length ? (
        <ol className="ui-motion-enter-items mt-5">
          {groups.map((group, index) => (
            <ActivityGroupCard
              key={group.events[0].seq}
              group={group}
              isLatest={index === 0}
              isLast={index === groups.length - 1}
              now={nowSeconds}
            />
          ))}
        </ol>
      ) : (
        <div className="mt-4 flex items-center gap-3 rounded-md bg-cosci-hover px-4 py-3.5">
          <span className="size-2 shrink-0 motion-safe:animate-pulse rounded-full bg-cosci-muted" />
          <p className="text-sm text-cosci-muted">
            Warming up — the first steps will appear here in a moment.
          </p>
        </div>
      )}
    </section>
  );
}

interface ActivityMeta {
  title: string;
  icon: IconName;
  tone: string;
}

const SAFETY_META: ActivityMeta = {
  title: 'Safety screening',
  icon: 'encrypted',
  tone: 'text-th-warning',
};

const ACTIVITY_KEY_META: Record<
  Exclude<RunEventActivity, 'other'>,
  ActivityMeta
> = {
  planning: {
    title: 'Planning strategy',
    icon: 'assignment',
    tone: 'text-th-primary',
  },
  literature_search: {
    title: 'Reviewing literature',
    icon: 'menu_book',
    tone: 'text-cosci-teal',
  },
  drafting: {
    title: 'Generating hypotheses',
    icon: 'lightbulb',
    tone: 'text-th-primary',
  },
  review: {
    title: 'Reviewing hypotheses',
    icon: 'rate_review',
    tone: 'text-th-success',
  },
  tournament: {
    title: 'Comparing ideas',
    icon: 'emoji_events',
    tone: 'text-th-warning',
  },
  evolution: {
    title: 'Evolving hypotheses',
    icon: 'edit_square',
    tone: 'text-cosci-blue',
  },
  deduplication: {
    title: 'Mapping the idea landscape',
    icon: 'chess',
    tone: 'text-cosci-teal',
  },
  synthesis: {
    title: 'Synthesizing meta-review',
    icon: 'summarize',
    tone: 'text-th-primary',
  },
  safety: SAFETY_META,
};

const ACTIVITY_META: Record<string, ActivityMeta> = {
  bootstrap: {
    title: 'Initializing run',
    icon: 'settings',
    tone: 'text-cosci-muted',
  },
  created: {title: 'Run created', icon: 'check', tone: 'text-cosci-muted'},
  queued: {title: 'Queued', icon: 'history', tone: 'text-cosci-muted'},
  completed: {title: 'Run complete', icon: 'check', tone: 'text-th-success'},
  supervisor: ACTIVITY_KEY_META.planning,
  orchestrator: {
    title: 'Coordinating agents',
    icon: 'assignment',
    tone: 'text-th-primary',
  },
  literature_review: ACTIVITY_KEY_META.literature_search,
  generate: ACTIVITY_KEY_META.drafting,
  reflection: {
    title: 'Reflecting on ideas',
    icon: 'neurology',
    tone: 'text-th-success',
  },
  comprehensive_reflection: {
    title: 'Deep reflection',
    icon: 'neurology',
    tone: 'text-th-success',
  },
  review: ACTIVITY_KEY_META.review,
  deep_verification: {
    title: 'Verifying assumptions',
    icon: 'check',
    tone: 'text-th-success',
  },
  ranking: {
    title: 'Ranking tournament',
    icon: 'emoji_events',
    tone: 'text-th-warning',
  },
  meta_review: ACTIVITY_KEY_META.synthesis,
  research_overview: {
    title: 'Building research overview',
    icon: 'stars',
    tone: 'text-th-primary',
  },
  evolve: ACTIVITY_KEY_META.evolution,
  proximity: ACTIVITY_KEY_META.deduplication,
  safety_screen: SAFETY_META,
  safety: SAFETY_META,
};

function activityPhase(event: StreamEvent): string {
  if (event.type === 'scientific_task') {
    return String(event.payload.task ?? '');
  }
  if (event.type === 'lifecycle') {
    return String(event.payload.event ?? '');
  }
  return event.type.split('.')[0] ?? event.type;
}

function legacyActivityMeta(phase: string): ActivityMeta {
  const known = ACTIVITY_META[phase];
  if (known) return known;
  const words = phase.replaceAll('_', ' ').replaceAll('.', ' ');
  return {
    title: capitalizeTerm(words),
    icon: 'history',
    tone: 'text-cosci-muted',
  };
}

function isClassifiedActivity(
  value: unknown,
): value is Exclude<RunEventActivity, 'other'> {
  return typeof value === 'string' && value in ACTIVITY_KEY_META;
}

interface ResolvedActivity {
  key: string;
  meta: ActivityMeta;
}

function resolveActivity(event: StreamEvent): ResolvedActivity {
  const activity = event.payload.activity;
  if (isClassifiedActivity(activity)) {
    return {key: activity, meta: ACTIVITY_KEY_META[activity]};
  }
  const phase = activityPhase(event);
  return {key: phase, meta: legacyActivityMeta(phase)};
}

function activityDetail(event: StreamEvent): string {
  const detail = event.payload.message || event.payload.reason;
  return detail ? String(detail) : '';
}

export interface ActivityGroup {
  key: string;
  meta: ActivityMeta;
  events: StreamEvent[];
}

// Group adjacent events only so a return to an earlier phase stays visible.
function groupConsecutiveActivity(events: StreamEvent[]): ActivityGroup[] {
  const groups: ActivityGroup[] = [];
  for (const event of events) {
    const resolved = resolveActivity(event);
    const current = groups.at(-1);
    if (current && current.key === resolved.key) {
      current.events.push(event);
    } else {
      groups.push({key: resolved.key, meta: resolved.meta, events: [event]});
    }
  }
  return groups;
}

export function windowedActivityGroups(
  events: StreamEvent[],
  limit: number,
): ActivityGroup[] {
  return groupConsecutiveActivity(events).slice(-limit).reverse();
}
