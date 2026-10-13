import {Card, ExternalLink, safeExternalHref} from '@/shared/ui';
import type {
  ClaimEvidenceRow,
  Hypothesis,
  MatchRow,
  Review,
} from '@/shared/api/runs';
import {Icon} from '@/shared/ui/icon';
import {smoothScrollToSection} from '@/shared/lib/smooth_scroll';
import {useMemo, type MouseEvent, type ReactNode} from 'react';
import {
  claimEvidenceSummary,
  debateDepthLabel,
  findHypothesisMatches,
  findHypothesisReview,
  findHypothesisReviews,
  hasReviewDetail,
  NO_REVIEW_CRITIQUES_TEXT,
  normalizeSpans,
  originLabel,
  parseDebateTranscript,
  parseReviewDetail,
  reviewCritiqueText,
  reviewerLabel,
  reviewSummaryText,
  tournamentSummaryText,
  type NormalizedSpan,
  type ReviewDetail,
} from './ideas_detail_data';

const IDEA_DETAIL_PANE_CLASSES =
  'idea-detail-pane grid min-h-0 min-w-0 flex-1 content-start gap-5 ' +
  'overflow-x-hidden overflow-y-auto border-r-0 bg-transparent px-7 ' +
  // Stack below the three-column fit threshold so detail can grow; the separate
  // phone breakpoint controls interaction and gutters.
  'pt-6 pb-14 max-[1023px]:flex-none ' +
  'max-[1023px]:overflow-y-visible phone:px-4';

const IDEA_DETAIL_EMPTY_CLASSES =
  `${IDEA_DETAIL_PANE_CLASSES} empty place-items-center text-center ` +
  'text-th-muted-fg';

interface HypothesisDetailProps {
  hypothesis: Hypothesis | null;
  reviews: Review[];
  matches: MatchRow[];
  claimEvidence?: ClaimEvidenceRow[];
}

// Keep hooks before the empty-state return; input memoization avoids rescanning
// sibling collections on unrelated SSE updates.
function useHypothesisRecords(
  hypothesis: Hypothesis | null,
  reviews: Review[],
  matches: MatchRow[],
  claimEvidence: ClaimEvidenceRow[],
) {
  const review = useMemo(
    () => (hypothesis ? findHypothesisReview(hypothesis, reviews) : undefined),
    [hypothesis, reviews],
  );
  const allReviews = useMemo(
    () => (hypothesis ? findHypothesisReviews(hypothesis, reviews) : []),
    [hypothesis, reviews],
  );
  const matchHistory = useMemo(
    () => (hypothesis ? findHypothesisMatches(hypothesis, matches) : []),
    [hypothesis, matches],
  );
  const claims = useMemo(
    () =>
      hypothesis
        ? claimEvidence.filter(c => c.hypothesis_id === hypothesis.id)
        : [],
    [hypothesis, claimEvidence],
  );
  return {review, allReviews, matchHistory, claims};
}

export function HypothesisDetail({
  hypothesis,
  reviews,
  matches,
  claimEvidence = [],
}: HypothesisDetailProps) {
  const {review, allReviews, matchHistory, claims} = useHypothesisRecords(
    hypothesis,
    reviews,
    matches,
    claimEvidence,
  );

  if (!hypothesis) {
    return (
      <section id={DETAIL_PANE_ID} className={IDEA_DETAIL_EMPTY_CLASSES}>
        <Icon name="format_list_numbered" />
        <p>Select a hypothesis to inspect the review and tournament details.</p>
      </section>
    );
  }

  return (
    <HypothesisDetailSections
      paneClasses={IDEA_DETAIL_PANE_CLASSES}
      hypothesis={hypothesis}
      review={review}
      allReviews={allReviews}
      matchHistory={matchHistory}
      claims={claims}
    />
  );
}

// Rail jumps target the stacked detail pane or desktop report scroller; the
// scroll utility stays independent of app classes.
const IDEA_SCROLL_PANE_SELECTOR = '.idea-detail-pane, .cosci-report-scroll';

// The pane and rail share section vocabulary so links cannot drift from
// headings.
const SECTIONS = {
  overview: 'Hypothesis overview',
  description: 'Description',
  provenance: 'Provenance & lineage',
  reviewSummary: 'Review summary',
  reviewCritiques: 'Review critiques',
  tournament: 'Tournament performance',
  matchSummary: 'Match summary',
} as const;

const RAIL_SECTIONS: readonly string[] = [
  SECTIONS.overview,
  SECTIONS.description,
  SECTIONS.provenance,
  SECTIONS.reviewSummary,
  SECTIONS.reviewCritiques,
  SECTIONS.tournament,
];

function sectionSlug(title: string): string {
  return title.toLowerCase().replaceAll(' ', '-');
}

function smoothSectionClick(
  event: MouseEvent<HTMLAnchorElement>,
  sectionId: string,
) {
  const didScroll = smoothScrollToSection(
    sectionId,
    16,
    IDEA_SCROLL_PANE_SELECTOR,
  );
  if (!didScroll) return;
  event.preventDefault();
}

export function SectionsRail() {
  return (
    <Card
      as="section"
      size="panel"
      layoutClassName="idea-sections-rail m-5 min-w-0 min-w-[12.5rem] self-start max-[1023px]:hidden"
      aria-label="Sections"
    >
      <span className="text-[0.9rem] tracking-[0.1px] text-cosci-idea-title-text">
        Sections
      </span>
      <nav aria-label="Hypothesis sections" className="mt-5 grid gap-6">
        {RAIL_SECTIONS.map(item => (
          <a
            key={item}
            href={`#${sectionSlug(item)}`}
            className="block whitespace-nowrap text-[0.85rem] leading-6 font-medium text-cosci-blue no-underline"
            onClick={event => smoothSectionClick(event, sectionSlug(item))}
          >
            {item} &gt;
          </a>
        ))}
      </nav>
    </Card>
  );
}

// Go/No-Go framing is display-only and complements the critique rather than
// replacing scientific quality assessment.
function VerdictLines({detail}: {detail: ReviewDetail}) {
  if (!detail.goNoGo && !detail.timeToVerdict) return null;
  return (
    <>
      {detail.goNoGo && (
        <p>
          <strong>Verdict: </strong>
          {detail.goNoGo}
        </p>
      )}
      {detail.timeToVerdict && (
        <p>
          <strong>Time to verdict: </strong>
          {detail.timeToVerdict}
        </p>
      )}
    </>
  );
}

// Retain simulation prose alongside structured failures: stripping repeated
// lines would couple rendering to labels and lose model/step/robustness context.
function SimulationFindings({detail}: {detail: ReviewDetail}) {
  if (!detail.failurePoints.length && !detail.decisiveStep) return null;
  return (
    <>
      {detail.failurePoints.length > 0 && (
        <ol className="list-decimal pl-5">
          {detail.failurePoints.map((point, i) => (
            <li key={i}>
              <strong>Failure point:</strong> {point}
            </li>
          ))}
        </ol>
      )}
      {detail.decisiveStep && (
        <p>
          <strong>Decisive step: </strong>
          {detail.decisiveStep}
        </p>
      )}
    </>
  );
}

function ReviewFindings({review}: {review: Review}) {
  const detail = parseReviewDetail(review);
  if (!hasReviewDetail(detail)) return null;
  return (
    <div className="mb-1">
      <VerdictLines detail={detail} />
      <SimulationFindings detail={detail} />
    </div>
  );
}

function ReviewCritiquesContent({reviews}: {reviews: Review[]}) {
  if (!reviews.length) {
    return <p>{NO_REVIEW_CRITIQUES_TEXT}</p>;
  }
  return (
    <>
      {reviews.map(item => (
        <div key={item.id} className="mb-3 last:mb-0">
          <h3>{reviewerLabel(item.reviewer_agent)}</h3>
          <ReviewFindings review={item} />
          <p>{reviewCritiqueText(item)}</p>
        </div>
      ))}
    </>
  );
}

export const DETAIL_PANE_ID = 'hypothesis-detail-pane';

interface DetailSectionsProps {
  paneClasses: string;
  hypothesis: Hypothesis;
  review: Review | undefined;
  allReviews: Review[];
  matchHistory: MatchRow[];
  claims: ClaimEvidenceRow[];
}

function HypothesisDetailSections({
  paneClasses,
  hypothesis,
  review,
  allReviews,
  matchHistory,
  claims,
}: DetailSectionsProps) {
  return (
    <section
      id={DETAIL_PANE_ID}
      className={paneClasses}
      aria-label="Hypothesis detail"
    >
      <DetailSection title={SECTIONS.overview}>
        {hypothesis.screened && (
          <details className="mb-3 rounded border border-th-border p-3 text-sm">
            <summary className="cursor-pointer font-medium focus-visible:outline-2 focus-visible:outline-th-ring">
              Screened, not deep-verified
            </summary>
            <p className="mt-2">
              This idea passed the safety screen and a screening review. It was
              not selected for finalist review or deep verification.
            </p>
          </details>
        )}
        <p>{hypothesis.statement}</p>
      </DetailSection>

      <DetailSection title={SECTIONS.description}>
        <HypothesisDescriptionContent hypothesis={hypothesis} />
      </DetailSection>

      <DetailSection title={SECTIONS.provenance}>
        <HypothesisProvenanceContent hypothesis={hypothesis} claims={claims} />
      </DetailSection>
      <DetailSection title={SECTIONS.reviewSummary}>
        <p>{reviewSummaryText(review)}</p>
      </DetailSection>
      <DetailSection title={SECTIONS.reviewCritiques}>
        <ReviewCritiquesContent reviews={allReviews} />
      </DetailSection>
      <DetailSection title={SECTIONS.tournament}>
        <p>{tournamentSummaryText(hypothesis)}</p>
      </DetailSection>
      <DetailSection title={SECTIONS.matchSummary}>
        <MatchSummaryContent
          hypothesisId={hypothesis.id}
          matches={matchHistory}
        />
      </DetailSection>
    </section>
  );
}

function HypothesisDescriptionContent({hypothesis}: {hypothesis: Hypothesis}) {
  return (
    <>
      <h3>{hypothesis.title}</h3>
      {hypothesis.mechanism && (
        <p>
          <strong>Proposed mechanism of action:</strong> {hypothesis.mechanism}
        </p>
      )}
      {hypothesis.expected_effect && (
        <p>
          <strong>Expected effect:</strong> {hypothesis.expected_effect}
        </p>
      )}
    </>
  );
}

// Unsupported claims remain visible with categorical/speculative roles; partial
// support is distinct from full entailment.
function claimVerdictText(claim: ClaimEvidenceRow): string {
  if (claim.label === 'partial') return 'partial support';
  if (claim.label === 'insufficient') {
    return claim.claim_role === 'speculative'
      ? 'Speculative — evidence insufficient'
      : 'Unsupported categorical claim';
  }
  return claim.label;
}

function ClaimVerdictLabel({claim}: {claim: ClaimEvidenceRow}) {
  return (
    <span className="capitalize font-medium">{claimVerdictText(claim)}</span>
  );
}

function EvidenceSpanList({spans}: {spans: NormalizedSpan[]}) {
  if (!spans.length) return null;
  return (
    <ul className="mt-1 flex flex-col gap-1">
      {spans.map((span, i) => (
        <li
          key={i}
          className="border-l-2 border-th-outline-variant pl-2 italic"
        >
          “{span.quote}”
          {safeExternalHref(span.url) && (
            <>
              {' '}
              <ExternalLink
                href={span.url}
                className="not-italic underline pointer-coarse:inline-block pointer-coarse:py-2"
              >
                open source
              </ExternalLink>
            </>
          )}
        </li>
      ))}
    </ul>
  );
}

function claimSpans(claim: ClaimEvidenceRow): NormalizedSpan[] {
  return claim.label === 'contradicts'
    ? normalizeSpans(claim.contradicting)
    : normalizeSpans(claim.supporting);
}

function ClaimEvidenceDetail({claims}: {claims: ClaimEvidenceRow[]}) {
  if (!claims.length) return null;
  return (
    <div className="mt-1 flex flex-col gap-2">
      {claims.map(claim => (
        <div key={claim.id} className="text-xs">
          <p>
            <ClaimVerdictLabel claim={claim} />: {claim.claim}
          </p>
          <EvidenceSpanList spans={claimSpans(claim)} />
        </div>
      ))}
    </div>
  );
}

function ProvenanceOriginLines({hypothesis}: {hypothesis: Hypothesis}) {
  return (
    <>
      <p>
        <strong>Origin:</strong> {originLabel(hypothesis.created_by_agent)}
        {hypothesis.author ? ` — ${hypothesis.author}` : ''}
      </p>
      <p>
        <strong>Generation:</strong>{' '}
        {hypothesis.generation === 0
          ? 'Generation 0 (initial hypothesis)'
          : `Generation ${hypothesis.generation}`}
        {hypothesis.parent_id && ' — evolved from an earlier hypothesis'}
      </p>
    </>
  );
}

function HypothesisProvenanceContent({
  hypothesis,
  claims,
}: {
  hypothesis: Hypothesis;
  claims: ClaimEvidenceRow[];
}) {
  const claimSummary = claimEvidenceSummary(claims);
  return (
    <>
      <ProvenanceOriginLines hypothesis={hypothesis} />
      {hypothesis.cluster_id && (
        <p>
          <strong>Proximity cluster:</strong> {hypothesis.cluster_id}
        </p>
      )}
      <p>
        <strong>Safety status:</strong>{' '}
        <span className="capitalize">
          {hypothesis.safety_status || 'not screened'}
        </span>
      </p>
      {claimSummary && (
        <p>
          <strong>Claim evidence:</strong> {claimSummary}
        </p>
      )}
      <ClaimEvidenceDetail claims={claims} />
    </>
  );
}

function MatchSummaryContent({
  hypothesisId,
  matches,
}: {
  hypothesisId: string;
  matches: MatchRow[];
}) {
  if (!matches.length) {
    return <p>No match rationale is available yet.</p>;
  }
  return (
    <>
      <h3>Match history</h3>
      <ol className="mt-2 flex flex-col gap-4">
        {matches.map(match => (
          <MatchHistoryItem
            key={match.id}
            hypothesisId={hypothesisId}
            match={match}
          />
        ))}
      </ol>
    </>
  );
}

function MatchHistoryItem({
  hypothesisId,
  match,
}: {
  hypothesisId: string;
  match: MatchRow;
}) {
  const transcript = parseDebateTranscript(match.debate_transcript);

  return (
    <li className="grid gap-1 border-l-2 border-th-outline-variant pl-3">
      <p>{matchResult(match, hypothesisId)}</p>
      <p>{matchIteration(match)}</p>
      <p>
        <strong>Elo change:</strong>{' '}
        {formatEloChange(matchEloChange(match, hypothesisId))}
      </p>
      <p>
        <strong>Debate depth:</strong> {debateDepthLabel(match.debate_turns)}
      </p>
      <p>{match.rationale || 'No match rationale was recorded.'}</p>
      {transcript && (
        <MatchTranscriptDisclosure
          transcript={transcript}
          selectedSide={selectedTranscriptSide(
            match,
            hypothesisId,
            transcript.verdict,
          )}
        />
      )}
    </li>
  );
}

function matchResult(match: MatchRow, hypothesisId: string): string {
  const won = match.winner_id === hypothesisId;
  return `${won ? 'Win' : 'Loss'} against ${won ? match.loser_id : match.winner_id}`;
}

function matchIteration(match: MatchRow): string {
  return `Iteration ${match.iteration}${match.tier ? ` · ${match.tier}` : ''}`;
}

function matchEloChange(match: MatchRow, hypothesisId: string): number {
  return match.winner_id === hypothesisId
    ? match.winner_elo_after - match.winner_elo_before
    : match.loser_elo_after - match.loser_elo_before;
}

function formatEloChange(change: number): string {
  return `${change > 0 ? '+' : ''}${change}`;
}

function selectedTranscriptSide(
  match: MatchRow,
  hypothesisId: string,
  verdict: '1' | '2',
): '1' | '2' {
  return (verdict === '1') === (match.winner_id === hypothesisId) ? '1' : '2';
}

function MatchTranscriptDisclosure({
  transcript,
  selectedSide,
}: {
  transcript: NonNullable<ReturnType<typeof parseDebateTranscript>>;
  selectedSide: '1' | '2';
}) {
  if (!transcript.turns.length) return null;
  return (
    <details>
      <summary>Debate transcript ({transcript.turns.length} turns)</summary>
      <ol className="mt-2 flex flex-col gap-2">
        {transcript.turns.map(turn => {
          const selectedWasPresentedFirst = turn.first === selectedSide;
          const selectedWasFavored = turn.favored === selectedSide;
          return (
            <li key={turn.turn}>
              <p>
                <strong>Turn {turn.turn}:</strong> selected hypothesis was
                presented as Hypothesis {selectedWasPresentedFirst ? '1' : '2'};
                this turn favored {selectedWasFavored ? 'it' : 'the opponent'}.
              </p>
              <p>{turn.text}</p>
            </li>
          );
        })}
      </ol>
    </details>
  );
}

function DetailSection({
  title,
  children,
}: {
  title: string;
  children: ReactNode;
}) {
  return (
    <section
      className="idea-detail-section grid gap-2 border-t-0 pt-0 [&_h2]:m-0 [&_h2]:mb-2 [&_h2]:font-gsans [&_h2]:text-[2rem] [&_h2]:leading-10 [&_h2]:font-normal phone:[&_h2]:text-[clamp(1.5rem,6.8vw,2rem)] phone:[&_h2]:leading-[1.2] [&_h2]:text-cosci-idea-title-text [&_h3]:m-0 [&_h3]:text-base [&_h3]:font-semibold [&_h3]:normal-case [&_h3]:text-cosci-idea-title-text [&_p]:m-0 [&_p]:[overflow-wrap:anywhere] [&_p]:text-base [&_p]:leading-6 [&_p]:text-cosci-idea-detail-text"
      id={sectionSlug(title)}
    >
      <h2>{title}</h2>
      <div>{children}</div>
    </section>
  );
}
