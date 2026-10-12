import {describe, expect, it} from 'vitest';
import type {Run} from '@/shared/api/runs';
import {makeRun} from '@/shared/testing/fixtures';
import {homeRunStepIndex, RunStepFlow} from './chat_home_stage';
import {render, screen} from '@testing-library/react';

describe('home recents data', () => {
  const ENGINE_TASK_PHASES: [string, number | null][] = [
    ['engine.bootstrap', 1],
    ['engine.node.supervisor', 1],
    ['engine.node.orchestrator', null],
    ['engine.node.literature_review', 2],
    ['engine.node.generate', 2],
    ['engine.fanout.generation.strategy', 2],
    ['engine.fanout.generation.aggregate', 2],
    ['engine.node.reflection', 3],
    ['engine.node.comprehensive_reflection', 3],
    ['engine.node.review', 3],
    ['engine.node.deep_verification', 3],
    ['engine.node.safety_screen', 3],
    ['engine.node.proximity', 4],
    ['engine.fanout.reflection.item', 3],
    ['engine.fanout.reflection.aggregate', 3],
    ['engine.fanout.review.item', 3],
    ['engine.fanout.review.aggregate', 3],
    ['engine.fanout.verification.item', 3],
    ['engine.fanout.verification.aggregate', 3],
    ['engine.node.ranking', 4],
    ['engine.ranking.match', 4],
    ['engine.ranking.finalize', 4],
    ['engine.node.evolve', 4],
    ['engine.node.meta_review', 4],
    ['engine.node.research_overview', 4],
    ['engine.finalize', 4],
  ];

  function activeTask(active_task: string | null): Partial<Run> {
    return {
      status: 'running',
      execution_progress: {
        determinate: false,
        completed_tasks: 1,
        total_tasks: 4,
        fraction: null,
        active_task,
        queued_tasks: 2,
      },
    };
  }

  it('derives the phase from the engine provider’s active durable task', () => {
    for (const [task, phase] of ENGINE_TASK_PHASES) {
      expect(homeRunStepIndex(makeRun(activeTask(task))), task).toBe(phase);
    }
  });
});

describe('home recents run steps', () => {
  function runOn(active_task: string): Run {
    return makeRun({
      status: 'running',
      latest_stage: null,
      execution_progress: {
        determinate: false,
        completed_tasks: 1,
        total_tasks: 2,
        fraction: null,
        active_task,
        queued_tasks: 1,
      },
    });
  }

  function shownPhases(): (string | null | undefined)[] {
    return [...document.querySelectorAll('.reference-run-step-icon')].map(
      icon =>
        icon.parentElement?.querySelector('.reference-run-step-label')
          ?.textContent,
    );
  }

  it('reveals only the phases the run has actually reached', () => {
    render(<RunStepFlow run={runOn('engine.node.supervisor')} />);
    expect(shownPhases()).toEqual(['Exploring focus areas']);
    expect(screen.getByText('In Progress')).toBeInTheDocument();
  });
});
