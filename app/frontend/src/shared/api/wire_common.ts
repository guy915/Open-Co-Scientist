// Generated from co_scientist.api.contracts; edit the backend models.

export interface AttributeScale {
  '1'?: string;
  '3'?: string;
  '5'?: string;
}

export interface CategoricalAttribute {
  name: string;
  values: string[];
}

export type JsonPrimitive = string | number | boolean | null;

export type JsonValue =
  string | number | boolean | JsonValue[] | {[key: string]: JsonValue} | null;

export interface NamedCriterion {
  name: string;
  value: string;
}

export type RunAttribute = string | ScaledAttribute | CategoricalAttribute;

export interface RunConfig {
  initial_hypotheses_count?: number;
  max_iterations?: number;
  evolution_max_count?: number;
  tournament_pairs?: number;
  evidence_count?: number;
  enable_literature_review?: boolean;
  enable_web_search?: boolean;
  k_factor?: number;
  tier?: 'express' | 'standard' | 'extended' | 'ultra';
  focus?: 'prefer_evidence' | 'balance' | 'prefer_novelty' | 'breakthrough';
  setup?: RunSetupConfig;
  [key: string]: JsonValue | RunSetupConfig | undefined;
}

export type RunCriterion = string | NamedCriterion;

export type RunEventActivity =
  | 'planning'
  | 'literature_search'
  | 'drafting'
  | 'review'
  | 'tournament'
  | 'evolution'
  | 'deduplication'
  | 'safety'
  | 'synthesis'
  | 'other';

export type RunFocus =
  'prefer_evidence' | 'balance' | 'prefer_novelty' | 'breakthrough';

export type RunMode = 'standard' | 'express' | 'extended' | 'ultra';

export interface RunSetupConfig {
  goal: string;
  requirements: string[];
  attributes: (string | ScaledAttribute | CategoricalAttribute)[];
  criteria: (string | NamedCriterion)[];
  focus: 'prefer_evidence' | 'balance' | 'prefer_novelty' | 'breakthrough';
  tier: 'express' | 'standard' | 'extended' | 'ultra';
}

export type RunStatus =
  | 'draft'
  | 'queued'
  | 'running'
  | 'synthesizing'
  | 'completed'
  | 'cancelled'
  | 'failed'
  | 'blocked'
  | 'paused';

export type RunTier = 'express' | 'standard' | 'extended' | 'ultra';

export interface ScaledAttribute {
  name: string;
  scale: AttributeScale;
}
