import type {RunWithSummary} from '@/shared/api/runs';
import * as runsApi from '@/shared/api/runs';
import type {StreamEvent} from '@/shared/hooks/use_run_stream';
import {deferred} from '@/shared/testing/deferred';
import {makeRunWithSummary} from '@/shared/testing/fixtures';
import {act, renderHook} from '@testing-library/react';
import {afterEach, beforeEach, expect, it, vi} from 'vitest';
import {useRunDetailData} from './run_detail_data';

const stream = vi.hoisted(() => ({
  events: [] as StreamEvent[],
  terminal: false,
  connection: 'open' as const,
}));

vi.mock('@/shared/hooks/use_run_stream', () => ({useRunStream: () => stream}));
vi.mock('@/shared/api/runs', async importActual => {
  const actual = await importActual<typeof import('@/shared/api/runs')>();
  return {
    ...actual,
    getRun: vi.fn(),
    getHypotheses: vi.fn(),
    getEvidence: vi.fn(),
    getMatches: vi.fn(),
    getReviews: vi.fn(),
    getClaimEvidence: vi.fn(),
    getSafety: vi.fn(),
    getReport: vi.fn(),
  };
});

function run(id = 'run-1', status: RunWithSummary['status'] = 'running') {
  return makeRunWithSummary({id, status});
}

async function load() {
  const hook = renderHook(({id}) => useRunDetailData(id), {
    initialProps: {id: 'run-1'},
  });
  await act(async () => {});
  expect(hook.result.current.loaded).toBe(true);
  return hook;
}

async function emit(rerender: (props: {id: string}) => void, type: string) {
  stream.events = [
    ...stream.events,
    {seq: stream.events.length + 1, type, payload: {}},
  ];
  rerender({id: 'run-1'});
  await act(async () => vi.advanceTimersByTimeAsync(600));
}

afterEach(() => vi.useRealTimers());

it('drops a previous run response after navigation', async () => {
  const {result, rerender} = await load();
  const olderRun = deferred<RunWithSummary>();
  vi.mocked(runsApi.getRun).mockReturnValueOnce(olderRun.promise);
  await emit(rerender, 'generate');

  stream.events = [];
  vi.mocked(runsApi.getRun).mockResolvedValue(run('run-2', 'completed'));
  rerender({id: 'run-2'});
  await act(async () => {});
  await act(async () => olderRun.resolve(run('run-1')));
  expect(result.current.run?.id).toBe('run-2');
});

it('coalesces mixed events into one selective refresh and cancels it on unmount', async () => {
  const {rerender, unmount} = await load();
  stream.events = [
    {seq: 1, type: 'generate', payload: {}},
    {seq: 2, type: 'reflection', payload: {}},
    {seq: 3, type: 'status', payload: {}},
  ];
  rerender({id: 'run-1'});
  await act(async () => vi.advanceTimersByTimeAsync(600));
  expect(runsApi.getRun).toHaveBeenCalledTimes(2);
  expect(runsApi.getHypotheses).toHaveBeenCalledTimes(2);
  expect(runsApi.getReviews).toHaveBeenCalledTimes(2);
  expect(runsApi.getEvidence).toHaveBeenCalledTimes(1);
  expect(runsApi.getReport).toHaveBeenCalledTimes(1);

  stream.events = [...stream.events, {seq: 4, type: 'ranking', payload: {}}];
  rerender({id: 'run-1'});
  unmount();
  await act(async () => vi.advanceTimersByTimeAsync(600));
  expect(runsApi.getRun).toHaveBeenCalledTimes(2);
});

it('refreshes the live counters on durable node task events', async () => {
  const {rerender, result} = await load();
  vi.mocked(runsApi.getRun).mockResolvedValue(
    makeRunWithSummary({
      id: 'run-1',
      status: 'running',
      summary: {...run().summary, hypotheses: 16, evidence: 229},
    }),
  );
  stream.events = [
    {seq: 1, type: 'scientific_task', payload: {task: 'ranking'}},
    {seq: 2, type: 'scientific_task', payload: {task: 'supervisor'}},
  ];
  rerender({id: 'run-1'});
  await act(async () => vi.advanceTimersByTimeAsync(600));
  expect(runsApi.getHypotheses).toHaveBeenCalledTimes(2);
  expect(runsApi.getMatches).toHaveBeenCalledTimes(2);
  expect(result.current.run?.summary.hypotheses).toBe(16);
  expect(result.current.run?.summary.evidence).toBe(229);
});

beforeEach(() => {
  vi.useFakeTimers();
  vi.resetAllMocks();
  stream.events = [];
  stream.terminal = false;
  vi.mocked(runsApi.getRun).mockResolvedValue(run());
  vi.mocked(runsApi.getHypotheses).mockResolvedValue([]);
  vi.mocked(runsApi.getEvidence).mockResolvedValue([]);
  vi.mocked(runsApi.getMatches).mockResolvedValue([]);
  vi.mocked(runsApi.getReviews).mockResolvedValue([]);
  vi.mocked(runsApi.getClaimEvidence).mockResolvedValue([]);
  vi.mocked(runsApi.getSafety).mockResolvedValue([]);
  vi.mocked(runsApi.getReport).mockResolvedValue(null);
});
