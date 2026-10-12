import {
  getClaimEvidence,
  getEvidence,
  getHypotheses,
  getMatches,
  getReport,
  getReviews,
  getRun,
  getSafety,
  runGoal,
  type ClaimEvidenceRow,
  type Evidence,
  type Hypothesis,
  type MatchRow,
  type Report,
  type Review,
  type RunWithSummary,
  type SafetyDecision,
} from '@/shared/api/runs';
import {
  useRunStream,
  type StreamConnectionState,
  type StreamEvent,
} from '@/shared/hooks/use_run_stream';
import {useResetTimer, useToast} from '@/shared/hooks/timers';
import {useCallback, useEffect, useMemo, useRef, useState} from 'react';
import {HEADER_TITLE_EVENT} from '@/shared/lib/dom_events';
import {displayTitle} from '@/shared/lib/titles';
import {errorMessage} from '@/shared/lib/errors';

// Carry stream transport state with the run so reconnecting cannot look
// healthy; missing state is not evidence of a drop.
export type RunWithStreamState = RunWithSummary & {
  stream_connection?: StreamConnectionState;
};

function useRunEventStream(
  id: string | undefined,
  onDataEvents: (keys: Iterable<RunDataKey>) => void,
  onTerminal: () => void,
) {
  const {events, terminal, connection} = useRunStream(id ?? null);
  // Scan every coalesced event, not only the tail, so a final status cannot
  // hide an earlier data update.
  const processedEventCount = useRef(0);
  useEffect(() => {
    if (events.length < processedEventCount.current) {
      processedEventCount.current = 0;
    }
    const fresh = events.slice(processedEventCount.current);
    processedEventCount.current = events.length;
    const data = fresh.filter(event => event.type !== 'status');
    if (data.length === 0) return;
    // An empty key set still re-reads the run, whose summary carries the live
    // counters.
    onDataEvents(dataKeysFromEvents(data));
  }, [events, onDataEvents]);

  // Refresh immediately on stream end so a pending debounce cannot leave
  // terminal state stale.
  useEffect(() => {
    if (!terminal) return;
    onTerminal();
  }, [terminal, onTerminal]);

  return {events, terminal, connection};
}

function failedRunToast(run: RunWithSummary): string {
  const guidance = runFailureGuidance(run.failure_kind);
  if (guidance) return guidance.toast;
  return `Run failed${run.error ? `: ${run.error}` : ''}`;
}

function runEndToast(run: RunWithSummary): string | null {
  if (run.status === 'failed') return failedRunToast(run);
  if (run.status === 'blocked') {
    return `Run blocked${run.error ? `: ${run.error}` : ''}`;
  }
  return null;
}

// The end-of-run toast expires like every other toast, and a refreshed run
// object does not raise it again.
function useRunDerivedState(run: RunWithSummary | null, terminal: boolean) {
  const {toast, setToast} = useToast();
  const announced = useRef<string | null>(null);
  useEffect(() => {
    if (!terminal || !run) return;
    const toastMessage = runEndToast(run);
    const key = `${run.id}:${toastMessage}`;
    if (!toastMessage || announced.current === key) return;
    announced.current = key;
    setToast(toastMessage);
  }, [terminal, run, setToast]);

  const title = useMemo(() => {
    if (!run) return 'Goal report';
    return displayTitle(run.title, runGoal(run), goal => goal);
  }, [run]);

  useEffect(() => {
    window.dispatchEvent(new CustomEvent(HEADER_TITLE_EVENT, {detail: title}));
    return () => {
      window.dispatchEvent(new CustomEvent(HEADER_TITLE_EVENT, {detail: ''}));
    };
  }, [title]);

  return {toast: toast?.message ?? null, title};
}

export function useRunDetailData(id: string | undefined) {
  const {scheduleRefresh, ...data} = useRunDetailCollections(id);
  const {events, terminal, connection} = useRunEventStream(
    id,
    scheduleRefresh,
    data.refreshNow,
  );
  const {toast, title} = useRunDerivedState(data.run, terminal);
  const run = useMemo<RunWithStreamState | null>(
    () =>
      data.run === null ? null : {...data.run, stream_connection: connection},
    [data.run, connection],
  );
  return {...data, run, toast, title, events};
}

export function runFailureGuidance(
  failureKind: string | null | undefined,
): {message: string; toast: string} | null {
  switch (failureKind) {
    case 'llm_call_budget_exceeded':
      return {
        message:
          'The run reached its configured model-call limit before it completed. Start a new run with a narrower research goal.',
        toast: 'Run failed. See the suggested next step below.',
      };
    case 'llm_timeout':
      return {
        message:
          'The model provider did not respond within the request timeout. Try the research again later.',
        toast: 'Run failed. See the suggested next step below.',
      };
    case 'llm_timeout_unknown':
      return {
        message:
          'The provider may have accepted the request; acceptance and any charge are unconfirmed. The run was not retried automatically. Restarting or resuming may repeat provider work.',
        toast: 'Run failed. See the suggested next step below.',
      };
    default:
      return null;
  }
}

export type RunDataKey =
  | 'hypotheses'
  | 'evidence'
  | 'matches'
  | 'reviews'
  | 'claimEvidence'
  | 'safety'
  | 'report';

// Terminal full refresh covers artifacts persisted only during finalize.
const EVENT_DATA_KEYS: Record<string, readonly RunDataKey[]> = {
  literature_review: ['evidence'],
  generate: ['hypotheses'],
  reflection: ['reviews'],
  review: ['reviews'],
  meta_review: ['reviews'],
  deep_verification: ['reviews'],
  'citation.grounding': ['claimEvidence'],
  'safety.intake': ['safety'],
  'safety.final': ['safety'],
  'scientist.hypothesis': ['hypotheses', 'safety'],
  'scientist.review': ['reviews'],
  proximity: ['hypotheses'],
  ranking: ['hypotheses', 'matches'],
  evolve: ['hypotheses', 'claimEvidence'],
  report: ['report'],
};

function dataKeysFromEvents(events: readonly StreamEvent[]): Set<RunDataKey> {
  const keys = new Set<RunDataKey>();
  for (const event of events) {
    // Durable node tasks report as `scientific_task` and name the node in
    // their payload.
    const task = event.payload.task;
    for (const name of [event.type, typeof task === 'string' ? task : '']) {
      for (const key of EVENT_DATA_KEYS[name] ?? []) keys.add(key);
    }
  }
  return keys;
}

async function fetchRunData(id: string, keys?: ReadonlySet<RunDataKey>) {
  const fetchIfWanted = <T>(
    key: RunDataKey,
    fetcher: (id: string) => Promise<T>,
  ): Promise<T> | undefined =>
    !keys || keys.has(key) ? fetcher(id) : undefined;
  // Older backends may lack safety audit during rolling upgrades; the rest of
  // the report must stay readable.
  const getSafetyCompatible = (runId: string) =>
    getSafety(runId).catch((): SafetyDecision[] => []);
  const [
    run,
    hypotheses,
    evidence,
    matches,
    reviews,
    claimEvidence,
    safety,
    report,
  ] = await Promise.all([
    getRun(id),
    fetchIfWanted('hypotheses', getHypotheses),
    fetchIfWanted('evidence', getEvidence),
    fetchIfWanted('matches', getMatches),
    fetchIfWanted('reviews', getReviews),
    fetchIfWanted('claimEvidence', getClaimEvidence),
    fetchIfWanted('safety', getSafetyCompatible),
    fetchIfWanted('report', getReport),
  ]);
  return {
    run,
    hypotheses,
    evidence,
    matches,
    reviews,
    claimEvidence,
    safety,
    report,
  };
}

async function fetchRunOutcome(id: string, keys?: ReadonlySet<RunDataKey>) {
  try {
    return {data: await fetchRunData(id, keys), error: null};
  } catch (err) {
    return {
      data: null,
      error: errorMessage(err),
    };
  }
}

type RunSnapshot = Awaited<ReturnType<typeof fetchRunData>>;

type SnapshotResource = keyof RunSnapshot;

const SNAPSHOT_RESOURCES: readonly SnapshotResource[] = [
  'run',
  'hypotheses',
  'evidence',
  'matches',
  'reviews',
  'claimEvidence',
  'safety',
  'report',
];

function claimSnapshotResources(
  owners: Map<SnapshotResource, number>,
  request: number,
  keys: ReadonlySet<RunDataKey> | undefined,
) {
  for (const resource of SNAPSHOT_RESOURCES) {
    if (resource === 'run' || !keys || keys.has(resource)) {
      owners.set(resource, request);
    }
  }
}

function isShownRun(
  id: string | undefined,
  shownId: string | undefined,
): id is string {
  return id !== undefined && shownId === id;
}

function useDebouncedKeyedRefresh(
  refresh: (keys?: ReadonlySet<RunDataKey>) => Promise<void>,
) {
  const pending = useRef(new Set<RunDataKey>());
  const refreshRef = useRef(refresh);
  useEffect(() => {
    refreshRef.current = refresh;
  }, [refresh]);
  const {schedule, cancel} = useResetTimer();
  const scheduleRefresh = useCallback(
    (keys: Iterable<RunDataKey>) => {
      for (const key of keys) pending.current.add(key);
      schedule(() => {
        const keys = pending.current;
        pending.current = new Set();
        void refreshRef.current(keys);
      }, 600);
    },
    [schedule],
  );
  const resetPending = useCallback(() => {
    cancel();
    pending.current = new Set();
  }, [cancel]);
  return {scheduleRefresh, cancelPending: cancel, resetPending};
}

interface CollectionsState {
  run: RunWithSummary | null;
  hypotheses: Hypothesis[];
  evidence: Evidence[];
  matches: MatchRow[];
  reviews: Review[];
  claimEvidence: ClaimEvidenceRow[];
  safety: SafetyDecision[];
  report: Report | null;
}

const EMPTY_COLLECTIONS: CollectionsState = {
  run: null,
  hypotheses: [],
  evidence: [],
  matches: [],
  reviews: [],
  claimEvidence: [],
  safety: [],
  report: null,
};

function useRunCollections() {
  const [collections, setCollections] = useState(EMPTY_COLLECTIONS);
  const applyFetched = useCallback(
    (data: RunSnapshot | null, owns: (key: SnapshotResource) => boolean) => {
      if (!data) return;
      // Disjoint requests can both land; each overlapping resource belongs to
      // its latest read.
      const updates = Object.fromEntries(
        Object.entries(data).filter(
          ([key, value]) =>
            value !== undefined && owns(key as SnapshotResource),
        ),
      );
      setCollections(current => ({...current, ...updates}));
    },
    [],
  );
  const reset = useCallback(() => setCollections(EMPTY_COLLECTIONS), []);
  return {...collections, applyFetched, reset};
}

function useRunDetailCollections(id: string | undefined) {
  const {applyFetched, reset, ...collections} = useRunCollections();
  const [settled, setSettled] = useState<{
    id: string;
    error: string | null;
  } | null>(null);
  const current = settled?.id === id ? settled : null;
  const shownId = useRef<string | undefined>(undefined);
  const requestSequence = useRef(0);
  const resourceOwners = useRef(new Map<SnapshotResource, number>());
  const refresh = useCallback(
    async (keys?: ReadonlySet<RunDataKey>) => {
      if (!isShownRun(id, shownId.current)) return;
      const request = ++requestSequence.current;
      claimSnapshotResources(resourceOwners.current, request, keys);
      const owns = (resource: SnapshotResource) =>
        resourceOwners.current.get(resource) === request;
      const outcome = await fetchRunOutcome(id, keys);
      if (!isShownRun(id, shownId.current)) return;
      applyFetched(outcome.data, owns);
      if (owns('run')) setSettled({id, error: outcome.error});
    },
    [id, applyFetched],
  );
  const {scheduleRefresh, cancelPending, resetPending} =
    useDebouncedKeyedRefresh(refresh);
  const refreshNow = useCallback(() => {
    cancelPending();
    void refresh();
  }, [refresh, cancelPending]);
  useEffect(() => {
    shownId.current = id;
    resetPending();
    reset();
    void refresh();
    return () => {
      shownId.current = undefined;
      resourceOwners.current.clear();
      resetPending();
    };
  }, [id, refresh, resetPending, reset]);
  return {
    ...collections,
    error: current?.error ?? null,
    loaded: current !== null,
    scheduleRefresh,
    refreshNow,
  };
}
