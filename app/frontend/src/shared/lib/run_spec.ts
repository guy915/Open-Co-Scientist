import type {
  ChatSummary,
  Interview,
  Run,
  RunAttribute,
  RunCriterion,
  RunFocus,
  RunTier,
} from '@/shared/api/runs';
import {keyedProviders} from './client_id';

// The backend permits only express for keyless free runs.
const FREE_RUN_TIER: RunTier = 'express';

function defaultRunTier(): RunTier {
  return keyedProviders().length > 0 ? 'standard' : FREE_RUN_TIER;
}

export interface InferredRunSpec {
  interviewId?: string;
  goal: string;
  requirements: string[];
  attributes: string[];
  criteria: string[];
  focus: RunFocus;
  tier: RunTier;
  notifyOnCompletion?: boolean;
  completionEmail?: string;
}

export function interviewToRunSpec(interview: Interview): InferredRunSpec {
  return {
    interviewId: interview.id,
    goal: interview.fields.research_challenge,
    requirements: interview.fields.preferences,
    attributes: interview.fields.focus_area,
    criteria: [],
    focus: 'balance',
    tier: defaultRunTier(),
    notifyOnCompletion: false,
    completionEmail: '',
  };
}

export interface EditedSpecFields {
  goal: string;
  attributes: string[];
  requirements: string[];
}

export function buildInterviewFieldsPayload(
  values: EditedSpecFields,
): Omit<Interview['fields'], 'title' | 'lab_constraints'> {
  return {
    research_challenge: values.goal,
    focus_area: values.attributes,
    preferences: values.requirements,
  };
}

// Adopt server-normalized edits rather than assuming submitted list entries
// persisted unchanged.
export function applyEditedInterviewFields(
  interview: Interview,
): Pick<
  InferredRunSpec,
  'interviewId' | 'goal' | 'attributes' | 'requirements'
> {
  return {
    interviewId: interview.id,
    goal: interview.fields.research_challenge,
    attributes: interview.fields.focus_area,
    requirements: interview.fields.preferences,
  };
}

export interface RunTierOption {
  id: RunTier;
  label: string;
  description: string;
  disabled?: boolean;
  hint?: string;
}

export interface RunFocusOption {
  id: RunFocus;
  label: string;
  description: string;
}

export const TIER_OPTIONS: RunTierOption[] = [
  {
    id: 'express',
    label: 'Express',
    description:
      'Suitable for quick research questions and small-scale experiments.',
  },
  {
    id: 'standard',
    label: 'Standard',
    description:
      'Suitable for medium-sized research questions and experiments.',
  },
  {
    id: 'extended',
    label: 'Extended',
    description: 'Suitable for large-scale research questions and experiments.',
  },
  {
    id: 'ultra',
    label: 'Ultra',
    description:
      'Most compute-intensive, using the largest models for cutting-edge ' +
      'insights.',
  },
];

export function availableTierOptions(): RunTierOption[] {
  if (keyedProviders().length > 0) return TIER_OPTIONS;
  return TIER_OPTIONS.map(option =>
    option.id === FREE_RUN_TIER
      ? option
      : {
          ...option,
          disabled: true,
          hint: 'Requires your own API key (Settings > Model).',
        },
  );
}

export const FOCUS_OPTIONS: RunFocusOption[] = [
  {
    id: 'prefer_evidence',
    label: 'Prefer evidence',
    description:
      'Prioritizes well-established methods and data for high-confidence, ' +
      'incremental advances.',
  },
  {
    id: 'balance',
    label: 'Balance',
    description:
      'A mix of established techniques and novel approaches for a ' +
      'comprehensive strategy.',
  },
  {
    id: 'prefer_novelty',
    label: 'Prefer novelty',
    description:
      'Favors unconventional ideas and exploratory methods for creative, ' +
      'higher-risk solutions.',
  },
  {
    id: 'breakthrough',
    label: 'Breakthrough',
    description:
      'Focuses on high-risk, high-reward strategies with the potential for ' +
      'paradigm shifts.',
  },
];

export function runOptionLabel(
  options: readonly {id: string; label: string}[],
  value: string | undefined,
  fallback: string,
): string {
  return options.find(option => option.id === value)?.label ?? fallback;
}

function joinWithOr(values: string[]): string {
  if (values.length === 1) return values[0];
  if (values.length === 2) return `${values[0]} or ${values[1]}`;
  return `${values.slice(0, -1).join(', ')}, or ${values[values.length - 1]}`;
}

function scaledDisplayString(
  name: string,
  scale: {'1'?: string; '3'?: string; '5'?: string},
): string {
  const anchors = (['1', '3', '5'] as const)
    .filter(point => scale[point])
    .map(point => `${point}: ${scale[point]}`);
  return anchors.length ? `${name}: 1-5 scale (${anchors.join(', ')})` : name;
}

function categoricalDisplayString(name: string, values: string[]): string {
  const options = (values ?? []).filter(Boolean);
  return options.length ? `${name} (${joinWithOr(options)})` : name;
}

// Legacy attributes may be prose rather than structured records.
export function attributeDisplayString(item: RunAttribute): string {
  if (typeof item === 'string') return item;
  if ('scale' in item) return scaledDisplayString(item.name, item.scale);
  return categoricalDisplayString(item.name, item.values);
}

function criterionDisplayString(item: RunCriterion): string {
  if (typeof item === 'string') return item;
  return `${item.name}: ${item.value}`;
}

export interface PendingRunCreatePayload extends Record<string, unknown> {
  research_goal?: string;
  requirements?: string[];
  attributes?: string[];
  criteria?: string[];
  focus?: RunFocus;
  tier?: RunTier;
  notify_on_completion?: boolean;
  completion_email?: string;
}

export interface LinkedRunTarget {
  chatId: string;
  runId: string;
  chat: ChatSummary | undefined;
  interview: Interview | null;
}

function firstDefined<T>(fallback: T, ...values: (T | undefined)[]): T {
  return values.find(value => value !== undefined) ?? fallback;
}

function whenPresent<A, B>(
  source: A | null | undefined,
  get: (source: A) => B | undefined,
): B | undefined {
  if (source === undefined || source === null) return undefined;
  return get(source);
}

export function recoverySpecForRun(
  target: LinkedRunTarget,
  run: Run,
  payload: PendingRunCreatePayload | undefined,
): InferredRunSpec {
  const setup = run.config.setup;
  const interviewGoal = whenPresent(
    target.interview,
    interview => interview.fields.research_challenge,
  );
  const interviewPreferences = whenPresent(
    target.interview,
    interview => interview.fields.preferences,
  );
  const interviewAttributes = whenPresent(
    target.interview,
    interview => interview.fields.focus_area,
  );
  return {
    interviewId: firstDefined(
      target.chatId,
      whenPresent(target.interview, interview => interview.id),
    ),
    goal: firstDefined(
      run.research_goal,
      whenPresent(setup, value => value.goal),
      whenPresent(payload, value => value.research_goal),
      interviewGoal,
    ),
    requirements: firstDefined(
      [],
      whenPresent(setup, value => value.requirements),
      whenPresent(payload, value => value.requirements),
      interviewPreferences,
    ),
    attributes: firstDefined(
      [],
      whenPresent(setup, value => value.attributes.map(attributeDisplayString)),
      whenPresent(payload, value => value.attributes),
      interviewAttributes,
    ),
    criteria: firstDefined(
      [],
      whenPresent(setup, value => value.criteria.map(criterionDisplayString)),
      whenPresent(payload, value => value.criteria),
    ),
    focus: firstDefined(
      'balance',
      whenPresent(setup, value => value.focus),
      run.config.focus,
      whenPresent(payload, value => value.focus),
    ),
    tier: firstDefined(
      'standard',
      whenPresent(setup, value => value.tier),
      run.config.tier,
      whenPresent(payload, value => value.tier),
    ),
    notifyOnCompletion: firstDefined(
      false,
      whenPresent(payload, value => value.notify_on_completion),
    ),
    completionEmail: firstDefined(
      '',
      whenPresent(payload, value => value.completion_email),
    ),
  };
}
