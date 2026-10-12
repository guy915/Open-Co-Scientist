// Generated from co_scientist.api.contracts; edit the backend models.
import type {ClaimEvidenceRow} from './wire_science';

export interface AgentInsights {
  key_findings?: string[];
  uncertainties?: string[];
  contradictions?: string[];
  recommended_directions?: RecommendedDirection[];
  next_experiments?: string[];
}

export interface IdeaBucketEntry {
  id: string;
  title: string;
  reason: string;
}

export interface IdeaBuckets {
  high_potential: IdeaBucketEntry[];
  non_viable: IdeaBucketEntry[];
}

export interface KnowledgeBaseTopic {
  id: string;
  title: string;
  summary: string;
  detail: string;
  uncertainty?: string;
  reference_ids: string[];
}

export interface LeaderboardEntry {
  id: string;
  title: string;
  elo: number;
}

export interface OverviewSummary {
  summary?: string;
  research_directions?: ResearchDirection[];
}

export interface RecommendedDirection {
  focus_area: string;
  recommendation: string;
  justification: string;
}

export interface Report {
  id: string;
  run_id: string;
  payload: ReportPayload;
  markdown_path: string;
  markdown_text?: string;
  created_at: number;
}

export interface ReportPayload {
  research_goal?: string;
  run_mode?: 'standard' | 'express' | 'extended' | 'ultra';
  provider?: string;
  hypothesis_count?: number;
  idea_count?: number;
  verified_count?: number;
  evidence_count?: number;
  match_count?: number;
  citation_summary?: Record<string, number>;
  leaderboard?: LeaderboardEntry[];
  meta_review?: Record<string, unknown>;
  research_overview?: ResearchOverview;
  knowledge_base?: KnowledgeBaseTopic[];
  agent_insights?: AgentInsights;
  idea_buckets?: IdeaBuckets;
  claim_evidence?: ClaimEvidenceRow[];
  execution_time?: number;
  degraded_sections?: string[];
  retrieval_degradation?: RetrievalDegradation | null;
}

export interface ResearchContact {
  candidate_id: string;
  name: string;
  expertise: string;
  justification: string;
  source_id: string;
  source_title: string;
  source_url: string;
  source: string;
  research_direction?: string;
}

export interface ResearchDirection {
  title: string;
  importance: string;
  suggested_experiments: string[];
  recent_findings?: string;
  sub_topics?: ResearchSubTopic[];
}

export interface ResearchOverview {
  overview?: OverviewSummary;
  nih_specific_aims?: SpecificAims;
  research_contacts?: ResearchContact[];
}

export interface ResearchSubTopic {
  title?: string;
  why?: string;
  what?: string;
  example_idea?: string;
  specific_questions?: string[];
}

export interface RetrievalDegradation {
  reason: string;
  lost: string[];
  floor: string;
}

export interface SpecificAim {
  overarching_goal?: string;
  hypothesis?: string;
  reasoning?: string;
  aim?: string;
  rationale?: string;
  approach?: string;
}

export interface SpecificAims {
  disease_description?: string;
  unmet_need?: string;
  proposed_solution?: string;
  aims?: SpecificAim[];
  pilot_evaluation?: string;
  introduction?: string;
  impact?: string;
}
