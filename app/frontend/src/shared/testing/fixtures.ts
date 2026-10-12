import type {
  ChatSummary,
  Hypothesis,
  InterviewQuestion,
  MatchRow,
  Run,
  RunMessage,
  RunWithSummary,
} from '@/shared/api/runs';
import type {InferredRunSpec} from '@/shared/lib/run_spec';

export function makeHypothesis(over: Partial<Hypothesis> = {}): Hypothesis {
  return {
    id: 'h1',
    run_id: 'r1',
    parent_id: null,
    generation: 0,
    category: null,
    title: 'Untitled hypothesis',
    statement: 'A statement.',
    mechanism: null,
    expected_effect: null,
    experimental_context: null,
    created_by_agent: 'generate',
    created_at: 0,
    elo_rating: 1200,
    win_count: 0,
    loss_count: 0,
    novelty_score: null,
    plausibility_score: null,
    testability_score: null,
    safety_status: null,
    status: null,
    cluster_id: null,
    ...over,
  };
}

export function makeRun(over: Partial<Run> = {}): Run {
  return {
    id: 'r1',
    research_goal: 'goal',
    profile: 'standard',
    status: 'completed',
    provider: 'engine',
    config: {},
    created_at: 0,
    updated_at: 0,
    completed_at: null,
    error: null,
    ...over,
  };
}

const RUN_PRESETS = {
  blank: {},
  overview: {
    id: 'run-1',
    research_goal: 'Study pathway X',
    created_at: 1_700_000_000,
    completed_at: 1_700_000_000 + 3 * 3600,
  },
  listed: {
    provider: 'mock',
    created_at: 1,
    updated_at: 2,
    completed_at: 3,
    summary: {events: 1, hypotheses: 1, evidence: 1, matches: 1, reviews: 1},
  },
  chat: {
    id: 'run-1',
    research_goal: 'Investigate glucose homeostasis.',
    provider: 'mock',
    created_at: 1,
    updated_at: 2,
    completed_at: 3,
    top_elo: 1200,
    summary: {events: 4, hypotheses: 1, evidence: 1, matches: 1, reviews: 1},
  },
} satisfies Record<string, Partial<RunWithSummary>>;

export function makeRunWithSummary(
  over: Partial<RunWithSummary> = {},
  preset: keyof typeof RUN_PRESETS = 'blank',
): RunWithSummary {
  return {
    ...makeRun(),
    summary: {events: 0, hypotheses: 0, evidence: 0, matches: 0, reviews: 0},
    ...RUN_PRESETS[preset],
    ...over,
  };
}

export function makeRunWithSetup(
  goal: string,
  over: Partial<RunWithSummary> = {},
): RunWithSummary {
  return makeRunWithSummary({
    id: 'run-1',
    research_goal: goal,
    config: {
      setup: makeSpec({
        goal,
        requirements: ['Testable'],
        attributes: ['Novel'],
        criteria: ['Feasible'],
      }),
    },
    ...over,
  });
}

export function makeSpec(over: Partial<InferredRunSpec> = {}): InferredRunSpec {
  return {
    goal: 'Study liver fibrosis',
    requirements: ['Req A'],
    attributes: ['Attr A'],
    criteria: ['Crit A'],
    focus: 'balance',
    tier: 'standard',
    ...over,
  };
}

export function makeMatch(id: number, over: Partial<MatchRow> = {}): MatchRow {
  return {id, ...over} as unknown as MatchRow;
}

const CHAT_PRESETS = {
  completed: {
    id: 'interview-1',
    title: 'Cold-stress glucose homeostasis',
    challenge: 'Investigate glucose homeostasis.',
    status: 'completed',
    run_id: 'run-1',
    created_at: 1,
    updated_at: 3,
  },
  active: {
    id: 'interview-1',
    title: null,
    challenge: 'Investigate glucose homeostasis.',
    status: 'active',
    run_id: null,
    created_at: 1,
    updated_at: 2,
  },
} satisfies Record<string, ChatSummary>;

export function makeChat(
  over: Partial<ChatSummary> = {},
  preset: keyof typeof CHAT_PRESETS = 'completed',
): ChatSummary {
  return {...CHAT_PRESETS[preset], ...over};
}

export function makeRunMessage(over: Partial<RunMessage> = {}): RunMessage {
  return {
    id: 1,
    run_id: 'run-1',
    sender: 'user',
    content: 'Hello',
    kind: 'qa',
    created_at: 1,
    applied: false,
    meta: null,
    ...over,
  };
}

export function makeQuestion(
  over: Partial<InterviewQuestion> = {},
): InterviewQuestion {
  return {
    header: 'Model system',
    question: 'Which model system should the ideas be built around?',
    multi_select: false,
    options: [
      {label: 'Primary human cells', description: 'Closest to patient biology'},
      {label: 'iPSC-derived line', description: 'Renewable and editable'},
    ],
    ...over,
  };
}

// The export fences its JSON in Markdown; strip the fence before parsing.
export function exportedRecords<T = Record<string, unknown>>(
  text: string,
): T[] {
  const body = text.slice(text.indexOf('```json') + '```json'.length);
  return JSON.parse(body.slice(0, body.lastIndexOf('```'))) as T[];
}
