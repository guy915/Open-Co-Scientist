// Generated from co_scientist.api.contracts; edit the backend models.

export interface ClaimEvidenceRow {
  id: number;
  hypothesis_id: string;
  claim: string;
  label: string;
  claim_role?: string;
  supporting: SupportSpan[];
  contradicting: SupportSpan[];
  assessor: string;
  verification_method?: string;
}

export interface Evidence {
  id: string;
  title: string;
  source: string;
  url: string;
  authors: string[];
  year: number | null;
  abstract?: string;
  available: boolean;
  retracted: boolean;
}

export interface Hypothesis {
  id: string;
  run_id: string;
  parent_id: string | null;
  generation: number;
  category: string | null;
  title: string;
  statement: string;
  mechanism: string | null;
  expected_effect: string | null;
  experimental_context: string | null;
  created_by_agent: string;
  author?: string | null;
  created_at: number;
  elo_rating: number;
  win_count: number;
  loss_count: number;
  novelty_score: number | null;
  plausibility_score: number | null;
  testability_score: number | null;
  safety_status: string | null;
  status: string | null;
  cluster_id: string | null;
  unverified?: boolean;
  screened?: boolean;
  verification_verdict?: string | null;
}

export interface HypothesisSnapshot {
  title: string;
  statement: string;
}

export interface MatchRow {
  id: number;
  iteration: number;
  winner_id: string;
  loser_id: string;
  winner_elo_before: number;
  winner_elo_after: number;
  loser_elo_before: number;
  loser_elo_after: number;
  rationale: string;
  tier: string | null;
  debate_turns: number;
  debate_transcript?: string | null;
  created_at: number;
}

export interface ReferencedEvidence {
  id: string;
  title: string;
  source: string;
  url: string | null;
  doi?: string | null;
  pmid?: string | null;
  sha256?: string | null;
}

export interface Review {
  id: number;
  hypothesis_id: string;
  reviewer_agent: string;
  summary: string;
  critique: string;
  detail_json?: string | null;
  novelty: number | null;
  plausibility: number | null;
  testability: number | null;
  overall: number | null;
}

export interface SafetyDecision {
  id: number;
  stage: string;
  decision: 'allow' | 'redact' | 'hold' | 'block';
  reason: string;
  matches: string[];
  category?: string | null;
  policy_version?: string | null;
  risk_domains: string[];
  requires_review: boolean;
  assessor?: string | null;
  resolution?: 'approved' | 'rejected' | null;
  resolved_by?: string | null;
  resolved_at?: number | null;
}

export interface SupportSpan {
  evidence_id: string;
  quote: string;
  start?: number;
  end?: number;
  source?: string;
  source_title?: string;
  url: string;
}
