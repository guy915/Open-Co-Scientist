import type {
  ClaimEvidenceRow,
  Hypothesis,
  MatchRow,
  Review,
} from '@/shared/api/runs';
import {Chip} from '@/shared/ui';
import {
  UNDERMINED_VERDICT,
  presentedHypotheses,
  ratingLabel,
} from '@/shared/lib/hypotheses';
import {useMemo} from 'react';
import {Link, useSearchParams} from 'react-router-dom';
import {useIsMobile} from '@/shared/hooks/dom';
import {TruncatedLabel} from '@/shared/ui/truncated_label';
import {
  DETAIL_PANE_ID,
  HypothesisDetail,
  SectionsRail,
} from './ideas_detail_pane';

const IDEA_RANK_ROW_CLASSES =
  'idea-rank-row grid min-h-[8.9rem] w-full min-w-0 ' +
  'grid-cols-[minmax(0,1fr)] cursor-pointer content-start ' +
  'gap-2 rounded-xl no-underline ' +
  'border border-cosci-idea-row-border bg-cosci-idea-row-bg ' +
  'p-4 text-left text-cosci-idea-row-text transition-colors duration-150 ' +
  'hover:border-cosci-idea-row-hover-border ' +
  'hover:bg-cosci-idea-row-hover-bg ' +
  'motion-reduce:transition-none';

function resolveSelectedHypothesis(
  sorted: Hypothesis[],
  selectedId: string | null,
  isMobile: boolean,
): Hypothesis | null {
  if (!sorted.length) return null;
  if (selectedId) return sorted.find(h => h.id === selectedId) ?? sorted[0];
  return isMobile ? null : sorted[0];
}

function ideaSearch(id: string): {search: string} {
  return {search: `?idea=${encodeURIComponent(id)}`};
}

function useIdeaSelection(hypotheses: Hypothesis[], isMobile: boolean) {
  // Tab links omit selection search, so returning to the mobile ideas tab
  // reopens the list.
  const [params] = useSearchParams();
  const selectedId = params.get('idea');
  // Exclude withdrawn ideas before selecting defaults so hidden rows cannot
  // remain the selected detail.
  const sorted = useMemo(() => presentedHypotheses(hypotheses), [hypotheses]);
  const selected = useMemo(
    () => resolveSelectedHypothesis(sorted, selectedId, isMobile),
    [sorted, selectedId, isMobile],
  );

  return {sorted, selected};
}

// not one empty result.
function emptyIdeasNote(exploredCount: number): string {
  if (!exploredCount) {
    return 'Hypotheses appear here once the generation node runs.';
  }
  return (
    'Every idea this run explored was ruled out or folded into another. ' +
    'Nothing was put forward.'
  );
}

export function IdeasTab({
  hypotheses,
  reviews,
  matches = [],
  claimEvidence = [],
}: {
  hypotheses: Hypothesis[];
  reviews: Review[];
  matches?: MatchRow[];
  claimEvidence?: ClaimEvidenceRow[];
}) {
  const isMobile = useIsMobile();
  const {sorted, selected} = useIdeaSelection(hypotheses, isMobile);

  if (!sorted.length) {
    return (
      <div className="rounded border border-th-border p-6 text-sm text-center text-th-muted-fg">
        {emptyIdeasNote(hypotheses.length)}
      </div>
    );
  }

  const IdeaView = isMobile ? MobileIdeaView : DesktopIdeaSplit;
  return (
    // Three-column fit and phone interaction need separate breakpoints; Tailwind
    // requires literal responsive classes rather than dynamically assembled strings.
    <div className="flex h-full min-h-0 flex-col overflow-hidden bg-cosci-bg max-[1023px]:h-auto max-[1023px]:overflow-visible">
      <IdeaView
        sorted={sorted}
        selected={selected}
        reviews={reviews}
        matches={matches}
        claimEvidence={claimEvidence}
      />
    </div>
  );
}

interface IdeaViewProps {
  sorted: Hypothesis[];
  selected: Hypothesis | null;
  reviews: Review[];
  matches: MatchRow[];
  claimEvidence: ClaimEvidenceRow[];
}

// The titlebar supplies the visible mobile Back control; this local screen-
// reader hint makes the escape discoverable inside the view.
function MobileIdeaView({
  sorted,
  selected,
  reviews,
  matches,
  claimEvidence,
}: IdeaViewProps) {
  return (
    <div className="idea-mobile-view flex h-auto min-h-0 flex-none flex-col overflow-visible bg-cosci-bg">
      {selected ? (
        <>
          <p className="sr-only">
            Use the Back button in the title bar to return to the ranked ideas
            list.
          </p>
          <HypothesisDetail
            hypothesis={selected}
            reviews={reviews}
            matches={matches}
            claimEvidence={claimEvidence}
          />
        </>
      ) : (
        <ol
          className="idea-mobile-list ui-motion-enter-items m-0 grid min-h-0 content-start gap-3 overflow-visible bg-transparent p-4 list-none"
          aria-label="Ranked hypothesis list"
        >
          {sorted.map((h, index) => (
            <IdeaListItem
              key={h.id}
              rank={index + 1}
              hypothesis={h}
              selected={false}
            />
          ))}
        </ol>
      )}
    </div>
  );
}

function DesktopIdeaSplit({
  sorted,
  selected,
  reviews,
  matches,
  claimEvidence,
}: IdeaViewProps) {
  return (
    <div className="idea-split-shell flex h-full min-h-0 flex-col overflow-hidden rounded-none border-0 bg-cosci-bg">
      <div className="idea-split-grid reference grid min-h-0 min-w-0 flex-1 grid-cols-[minmax(24rem,0.66fr)_minmax(0,1.25fr)_17rem] max-[1023px]:grid-cols-1">
        <ol
          // An auto grid track follows nowrap content width; minmax(0,1fr) prevents
          // clipped cards and sideways scrolling.
          className="idea-rank-list ui-motion-enter-items m-0 grid min-w-0 grid-cols-[minmax(0,1fr)] content-start gap-3 overflow-x-hidden overflow-y-auto border-r border-cosci-idea-list-border bg-transparent py-5 pr-6 pl-5 list-none"
          aria-label="Ranked hypothesis list"
        >
          {sorted.map((h, index) => (
            <IdeaListItem
              key={h.id}
              rank={index + 1}
              hypothesis={h}
              selected={h.id === selected?.id}
            />
          ))}
        </ol>
        <HypothesisDetail
          hypothesis={selected}
          reviews={reviews}
          matches={matches}
          claimEvidence={claimEvidence}
        />
        <SectionsRail />
      </div>
    </div>
  );
}

const SCREENED_TOOLTIP =
  'Screened, not deep-verified: passed the safety screen and a screening ' +
  'review but was not a finalist';

function ideaRowClassName(selected: boolean): string {
  return selected
    ? `${IDEA_RANK_ROW_CLASSES} selected !border-cosci-idea-row-selected-border !bg-cosci-idea-row-selected-bg hover:!border-cosci-idea-row-selected-border hover:!bg-cosci-idea-row-selected-hover-bg`
    : IDEA_RANK_ROW_CLASSES;
}

function IdeaRankHead({
  rank,
  hypothesis,
}: {
  rank: number;
  hypothesis: Hypothesis;
}) {
  return (
    <span className="idea-rank-head flex min-w-0 flex-wrap items-center gap-2.5">
      <Chip
        tone="info"
        layoutClassName="idea-rank-number min-w-7 justify-center"
      >
        {rank}
      </Chip>
      <Chip
        tone="info"
        layoutClassName="idea-elo-chip min-w-[6.35rem] justify-center"
      >
        {ratingLabel(hypothesis)}
      </Chip>
      {hypothesis.verification_verdict === UNDERMINED_VERDICT ? (
        // Undermined means evidence against an assumption; unverified means no verdict.
        // Distinct caution tones must not imply equal scientific standing.
        <Chip
          tone="danger"
          icon="warning"
          layoutClassName="idea-undermined-chip"
        >
          Undermined
        </Chip>
      ) : null}
      {hypothesis.unverified &&
      hypothesis.verification_verdict !== UNDERMINED_VERDICT ? (
        <Chip tone="info" icon="warning" layoutClassName="idea-unverified-chip">
          Unverified
        </Chip>
      ) : null}
      {hypothesis.screened ? (
        // Depth goes to finalists; a screened idea was never examined, so it
        // is neither verified nor unverified.
        <Chip
          tone="neutral"
          layoutClassName="idea-screened-chip max-w-full !h-auto !whitespace-normal py-1"
          tooltip={SCREENED_TOOLTIP}
          aria-label={SCREENED_TOOLTIP}
        >
          Screened, not deep-verified
        </Chip>
      ) : null}
    </span>
  );
}

function IdeaListItem({
  rank,
  hypothesis,
  selected,
}: {
  rank: number;
  hypothesis: Hypothesis;
  selected: boolean;
}) {
  return (
    <li>
      {/* Keep push navigation: mobile Back must return to the ranked list. */}
      <Link
        to={ideaSearch(hypothesis.id)}
        className={ideaRowClassName(selected)}
        aria-current={selected ? 'page' : undefined}
        aria-controls={selected ? DETAIL_PANE_ID : undefined}
      >
        <IdeaRankHead rank={rank} hypothesis={hypothesis} />
        <TruncatedLabel
          className="idea-rank-title mt-1.5 block min-w-0 overflow-hidden whitespace-nowrap text-base leading-6 font-medium text-cosci-idea-title-text"
          text={hypothesis.title}
        />
        <TruncatedLabel
          className="idea-rank-preview line-clamp-2 overflow-hidden text-[0.75rem] leading-4 tracking-[0.1px] text-cosci-idea-preview-text"
          text={hypothesis.statement}
          lines={2}
        />
      </Link>
    </li>
  );
}
