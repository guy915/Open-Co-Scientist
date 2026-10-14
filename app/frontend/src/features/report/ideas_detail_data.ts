import type {
  ClaimEvidenceRow,
  Hypothesis,
  MatchRow,
  Review,
  SupportSpan,
} from '@/shared/api/runs';
import {isRecord, readableText, readableTextList} from '@/shared/lib/text';

export function findHypothesisReview(
  hypothesis: Hypothesis,
  reviews: Review[],
): Review | undefined {
  return reviews.find(r => r.hypothesis_id === hypothesis.id);
}

export function findHypothesisReviews(
  hypothesis: Hypothesis,
  reviews: Review[],
): Review[] {
  return reviews.filter(r => r.hypothesis_id === hypothesis.id);
}

const REVIEWER_LABELS: Readonly<Record<string, string>> = {
  review: 'Initial peer review',
  reflection: 'Reflection review',
  full_review: 'Full review',
  simulation_review: 'Simulation review',
  recurrent_review: 'Recurrent review',
  deep_verification: 'Deep verification',
  scientist: 'Scientist review',
};

export function reviewerLabel(reviewerAgent: string): string {
  return REVIEWER_LABELS[reviewerAgent] || reviewerAgent || 'Unknown reviewer';
}

export function findHypothesisMatches(
  hypothesis: Hypothesis,
  matches: MatchRow[],
): MatchRow[] {
  return matches
    .filter(m => m.winner_id === hypothesis.id || m.loser_id === hypothesis.id)
    .sort((a, b) => b.created_at - a.created_at || b.id - a.id);
}

interface DebateTranscriptTurn {
  turn: number;
  favored: '1' | '2';
  text: string;
  first: '1' | '2';
}

export interface DebateTranscript {
  verdict: '1' | '2';
  turns: DebateTranscriptTurn[];
}

function isTranscriptSide(value: unknown): value is '1' | '2' {
  return value === '1' || value === '2';
}

function isDebateTranscriptTurn(value: unknown): value is DebateTranscriptTurn {
  if (!isRecord(value)) return false;
  return (
    typeof value.turn === 'number' &&
    isTranscriptSide(value.favored) &&
    typeof value.text === 'string' &&
    isTranscriptSide(value.first)
  );
}

// Legacy rows may lack transcripts; malformed stored JSON degrades to absence
// rather than breaking report rendering.
export function parseDebateTranscript(
  raw: string | null | undefined,
): DebateTranscript | null {
  if (!raw) return null;
  return asDebateTranscript(parseTranscriptJson(raw));
}

function parseTranscriptJson(raw: string): unknown {
  try {
    return JSON.parse(raw);
  } catch {
    return null;
  }
}

function asDebateTranscript(value: unknown): DebateTranscript | null {
  if (!isRecord(value)) return null;
  if (!isTranscriptSide(value.verdict)) return null;
  if (!Array.isArray(value.turns)) return null;
  return {
    verdict: value.verdict,
    turns: value.turns.filter(isDebateTranscriptTurn),
  };
}

export function originLabel(createdByAgent: string): string {
  switch (createdByAgent) {
    case 'evolution':
      return 'Evolution agent (refined from a parent)';
    case 'generation':
      return 'Generation agent';
    case 'scientist_manual':
      return 'Scientist (human-authored)';
    default:
      return createdByAgent || 'Unknown';
  }
}

interface ClaimCounts {
  supports: number;
  partial: number;
  contradicts: number;
  categoricalUnsupported: number;
  speculative: number;
}

// Partial is a separate support tier: consistent, relevant evidence falls short
// of full entailment.
function claimCountKey(c: ClaimEvidenceRow): keyof ClaimCounts {
  if (c.label === 'supports') return 'supports';
  if (c.label === 'partial') return 'partial';
  if (c.label === 'contradicts') return 'contradicts';
  if (c.claim_role === 'speculative') return 'speculative';
  return 'categoricalUnsupported';
}

function tallyClaimCounts(claims: ClaimEvidenceRow[]): ClaimCounts {
  const counts: ClaimCounts = {
    supports: 0,
    partial: 0,
    contradicts: 0,
    categoricalUnsupported: 0,
    speculative: 0,
  };
  for (const c of claims) counts[claimCountKey(c)]++;
  return counts;
}

const CLAIM_COUNT_LABELS: readonly {
  key: keyof ClaimCounts;
  suffix: string;
}[] = [
  {key: 'supports', suffix: 'supported'},
  {key: 'partial', suffix: 'partially supported'},
  {key: 'contradicts', suffix: 'contradicted'},
  {key: 'categoricalUnsupported', suffix: 'unsupported categorical'},
  {key: 'speculative', suffix: 'speculative'},
];

function describeClaimCounts(total: number, counts: ClaimCounts): string {
  const parts = [`${total} claim(s) assessed`];
  for (const {key, suffix} of CLAIM_COUNT_LABELS) {
    if (counts[key]) parts.push(`${counts[key]} ${suffix}`);
  }
  return parts.join(', ');
}

export function claimEvidenceSummary(
  claims: ClaimEvidenceRow[],
): string | null {
  if (!claims.length) return null;
  return describeClaimCounts(claims.length, tallyClaimCounts(claims));
}

export interface NormalizedSpan {
  quote: string;
  url?: string;
}

export function normalizeSpans(
  items: SupportSpan[] | undefined,
): NormalizedSpan[] {
  if (!items) return [];
  return items.map(item => ({quote: item.quote, url: item.url || undefined}));
}

export function reviewSummaryText(review: Review | undefined): string {
  return (
    review?.summary ||
    'Reviewer notes will appear after the review node completes.'
  );
}

export const NO_REVIEW_CRITIQUES_TEXT =
  'No review critiques have been recorded yet.';

// An empty critique still represents a recorded review, not a review that never
// ran.
export function reviewCritiqueText(review: Review): string {
  return review.critique || 'No critique text was recorded.';
}

// json_object responses do not enforce field schemas; coerce review findings
// through readable-text helpers rather than trusting declared types.
export interface ReviewDetail {
  failurePoints: string[];
  decisiveStep: string;
  goNoGo: string;
  timeToVerdict: string;
}

const EMPTY_REVIEW_DETAIL: ReviewDetail = {
  failurePoints: [],
  decisiveStep: '',
  goNoGo: '',
  timeToVerdict: '',
};

function parseDetailObject(raw: string): Record<string, unknown> | null {
  try {
    const parsed: unknown = JSON.parse(raw);
    return isRecord(parsed) ? parsed : null;
  } catch {
    return null;
  }
}

export function parseReviewDetail(review: Review): ReviewDetail {
  const detail = review.detail_json && parseDetailObject(review.detail_json);
  if (!detail) return EMPTY_REVIEW_DETAIL;
  return {
    failurePoints: readableTextList(detail.failure_points)
      .map(p => p.trim())
      .filter(Boolean),
    decisiveStep: readableText(detail.decisive_step).trim(),
    goNoGo: readableText(detail.go_no_go).trim(),
    timeToVerdict: readableText(detail.time_to_verdict).trim(),
  };
}

export function hasReviewDetail(detail: ReviewDetail): boolean {
  return Boolean(
    detail.failurePoints.length ||
    detail.decisiveStep ||
    detail.goNoGo ||
    detail.timeToVerdict,
  );
}

export function tournamentSummaryText(hypothesis: Hypothesis): string {
  const totalMatches = hypothesis.win_count + hypothesis.loss_count;
  if (!totalMatches) return 'No tournament matches have been recorded yet.';
  const winRate = Math.round((hypothesis.win_count / totalMatches) * 100);
  return (
    `${hypothesis.win_count} wins and ${hypothesis.loss_count} losses ` +
    `across ${totalMatches} pairwise matches (${winRate}% win rate).`
  );
}

// Google Co-Scientist allocates deeper debate to top-ranked matchups relative to
// median Elo (SSR sections 4 and 12).
export function debateDepthLabel(turns: number | undefined): string {
  if (turns && turns > 1) {
    return `Multi-turn scientific debate (${turns} turns)`;
  }
  return 'Single-turn comparison';
}
