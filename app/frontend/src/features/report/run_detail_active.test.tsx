import type {
  StreamConnectionState,
  StreamEvent,
} from '@/shared/hooks/use_run_stream';
import {render, screen} from '@testing-library/react';
import {describe, expect, it} from 'vitest';
import {ActiveRunView} from './run_detail_active';
import {ActivityLog, windowedActivityGroups} from './run_detail_activity_log';
import type {RunWithStreamState} from './run_detail_data';
import {makeRunWithSetup} from '@/shared/testing/fixtures';

describe('run detail active', () => {
  function activeRun(connection?: StreamConnectionState): RunWithStreamState {
    return {
      ...makeRunWithSetup('Study pathway X'),
      status: 'running',
      created_at: Date.now() / 1000 - 5,
      stream_connection: connection,
    };
  }

  function renderView(connection?: StreamConnectionState) {
    return render(
      <ActiveRunView
        run={activeRun(connection)}
        events={[]}
        evidenceCount={0}
        ideaCount={0}
      />,
    );
  }

  function hasLivePulse(): boolean {
    return (
      document.querySelector('[class~="motion-safe:animate-ping"]') !== null
    );
  }

  it('shows the live pulse and no status note on an open stream', () => {
    renderView('open');
    expect(hasLivePulse()).toBe(true);
    expect(screen.queryByRole('status', {name: ''})).toBeNull();
  });

  it('explains capacity refusal while keeping the live pulse off', () => {
    renderView('capacity');
    expect(hasLivePulse()).toBe(false);
    expect(screen.getByRole('status', {name: ''})).toHaveTextContent(
      'Live updates are busy. Close extra run tabs; we will retry shortly.',
    );
  });

  it('surfaces a reconnecting stream instead of passing as healthy', () => {
    renderView('reconnecting');
    expect(hasLivePulse()).toBe(false);
    expect(screen.getByRole('status', {name: ''})).toHaveTextContent(
      'Reconnecting...',
    );
  });
});

describe('run detail activity log', () => {
  const NOW = 1_000_000;

  function event(
    seq: number,
    payload: Record<string, unknown>,
    type = 'scientific_task',
  ): StreamEvent {
    return {seq, type, payload, created_at: NOW};
  }

  function renderLog(events: StreamEvent[]) {
    return render(
      <ActivityLog
        groups={windowedActivityGroups(events, 10)}
        connection="open"
        nowSeconds={NOW}
      />,
    );
  }

  it('falls back to the legacy node-keyed table when `activity` is absent', () => {
    renderLog([event(1, {task: 'ranking'})]);
    expect(screen.getByText('Ranking tournament')).toBeInTheDocument();
    expect(screen.queryByText('Comparing ideas')).toBeNull();
  });

  it('collapses a group to its newest step without a toggle', () => {
    renderLog([
      event(1, {task: 'ranking', activity: 'tournament', message: 'Match 1'}),
      event(2, {task: 'ranking', activity: 'tournament', message: 'Match 2'}),
    ]);
    expect(screen.getByText('Comparing ideas')).toBeInTheDocument();
    expect(screen.getByText('Match 2')).toBeInTheDocument();
    expect(screen.queryByText('Match 1')).toBeNull();
    expect(screen.queryByRole('button')).toBeNull();
  });
});
