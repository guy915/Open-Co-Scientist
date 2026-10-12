// Generated from co_scientist.api.contracts; edit the backend models.
import type {RunConfig, RunStatus} from './wire_common';

export interface MessageMetadata {
  sources?: QaSource[];
  reasoning?: string;
  fallback?: boolean;
}

export interface QaSource {
  n: number;
  evidence_id: string;
  title: string;
  url?: string | null;
  source?: string | null;
  year?: number | null;
  state: string;
  passage?: string | null;
}

export interface Run {
  id: string;
  research_goal: string;
  title?: string | null;
  run_mode?: 'standard' | 'express' | 'extended' | 'ultra';
  profile: 'standard' | 'express' | 'extended' | 'ultra';
  status: RunStatus;
  provider: 'mock' | 'engine';
  config: RunConfig;
  is_demo?: boolean;
  llm_backend?: string | null;
  created_at: number;
  updated_at: number;
  completed_at: number | null;
  error: string | null;
  top_elo?: number | null;
  top_hypotheses?: string[] | null;
  latest_stage?: string | null;
  awaiting_decision_count?: number;
  execution_progress?: RunExecutionProgress;
}

export interface RunExecutionProgress {
  determinate: boolean;
  completed_tasks: number;
  total_tasks: number;
  fraction: number | null;
  active_task: string | null;
  queued_tasks: number;
}

export interface RunMessage {
  id: number;
  run_id: string;
  sender: string;
  content: string;
  kind: string;
  created_at: number;
  applied: boolean;
  meta: MessageMetadata | null;
  applied_at?: number | null;
  applied_decision?: string | null;
}

export interface RunSummary {
  events: number;
  hypotheses: number;
  evidence: number;
  matches: number;
  reviews: number;
}

export interface RunWithSummary {
  id: string;
  research_goal: string;
  title?: string | null;
  run_mode?: 'standard' | 'express' | 'extended' | 'ultra';
  profile: 'standard' | 'express' | 'extended' | 'ultra';
  status: RunStatus;
  provider: 'mock' | 'engine';
  config: RunConfig;
  is_demo?: boolean;
  llm_backend?: string | null;
  created_at: number;
  updated_at: number;
  completed_at: number | null;
  error: string | null;
  top_elo?: number | null;
  top_hypotheses?: string[] | null;
  latest_stage?: string | null;
  awaiting_decision_count?: number;
  execution_progress?: RunExecutionProgress;
  summary: RunSummary;
  failure_kind?: string | null;
  can_resume?: boolean;
  resume_requires_ack?: boolean;
}
