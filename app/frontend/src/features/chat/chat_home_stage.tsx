import {type FormEvent, useState, Fragment, useEffect} from 'react';
import {type Run, isActiveStatus, isCompletedStatus} from '@/shared/api/runs';
import {joinClasses} from '@/shared/ui/classes';
import {useIsMobile} from '@/shared/hooks/dom';
import {Composer, type ConnectorToggleProps} from './chat_composer';
import {GoogleLabsIcon} from '@/shared/ui/layout_primitives';
import {Icon, type IconName} from '@/shared/ui/icon';
import {smoothScrollToSection} from '@/shared/lib/smooth_scroll';
import {Button, CardButton, Chip} from '@/shared/ui';
import {TruncatedLabel} from '@/shared/ui/truncated_label';
import {Link} from 'react-router-dom';
import {firstSentenceClause, capitalizeTerm} from '@/shared/lib/text';
import {useNowTick} from '@/shared/hooks/timers';
import {resultsPath} from '@/shared/hooks/session_side';
import {examplePath} from '@/shared/lib/routes';
import {displayTitle} from '@/shared/lib/titles';
import {formatDate, formatDurationPhrase} from '@/shared/lib/time';

export interface HomeStageProps {
  input: string;
  setInput: (value: string) => void;
  connectors: ConnectorToggleProps;
  onSubmit: (e: FormEvent<HTMLFormElement>, files: File[]) => void;
  runs: Run[];
  showAllRecents: boolean;
  onToggleShowAll: () => void;
}

// Phone and tablet bounds are exact media queries: Tailwind's max-* variants
// exclude their bound. The landing sheet owns the stage height when mounted.
const HOME_STAGE_CLASSES =
  'reference-home-stage grid h-full min-h-0 grid-cols-[minmax(40rem,50.75rem)_minmax(17rem,21rem)] justify-center gap-[clamp(2.5rem,7vw,7.5rem)] overflow-hidden px-6 pt-5 pb-[clamp(1.6rem,4vh,2.6rem)] ' +
  'tablet:h-auto tablet:min-h-full tablet:grid-cols-[minmax(0,1fr)] tablet:justify-items-center tablet:gap-[clamp(1.5rem,3.5vw,2.5rem)] tablet:overflow-visible tablet:px-[clamp(1.5rem,4vw,2.25rem)] tablet:py-0 ' +
  'desktop:[--home-recents-width:clamp(19rem,18vw,20.5rem)] desktop:[align-items:start] desktop:grid-cols-[minmax(0,1fr)_minmax(19rem,var(--home-recents-width))] desktop:justify-stretch desktop:gap-[clamp(2.25rem,3.1vw,3.35rem)] desktop:py-0 desktop:pr-[clamp(0.7rem,1.2vw,1.3rem)] desktop:pl-[clamp(2rem,3vw,4rem)] ' +
  'phone:flex phone:flex-1 phone:flex-col phone:gap-0 phone:px-[clamp(0.9rem,4vw,1.25rem)] phone:pt-3 phone:pb-[clamp(0.8rem,2.2vh,1.15rem)]';

// Only spacer tracks shrink as the composer grows upward; children take explicit
// rows so auto-flow cannot place the greeting into spacer tracks.
const HOME_MAIN_CLASSES =
  'reference-home-main relative z-0 grid [align-content:start] pt-[clamp(2.4rem,7vh,3.5rem)] ui-motion-enter ' +
  'tablet:h-[calc(100vh-4.5rem)] tablet:supports-[height:100dvh]:h-[calc(100dvh-4.5rem)] tablet:w-[min(100%,43rem)] tablet:grid-rows-[minmax(0,clamp(2.4rem,7vh,3.5rem))_auto_auto_minmax(0,1fr)_auto_minmax(auto,max-content)_max-content] tablet:pt-0 tablet:pb-[clamp(1rem,2.5vh,1.75rem)] ' +
  'desktop:h-full desktop:max-h-[calc(100vh-4.5rem)] desktop:supports-[height:100dvh]:max-h-[calc(100dvh-4.5rem)] desktop:max-w-[clamp(46rem,55vw,53.5rem)] desktop:min-w-0 desktop:justify-self-center desktop:grid-rows-[minmax(0,clamp(4.85rem,8.8vh,6.4rem))_auto_minmax(0,clamp(3.85rem,7.6vh,5.8rem))_auto_minmax(0,1fr)_auto_minmax(auto,max-content)_max-content] desktop:pt-0 desktop:pb-[clamp(1.35rem,3.2vh,2.25rem)] ' +
  'phone:flex phone:min-h-0 phone:w-full phone:flex-1 phone:flex-col phone:pt-2';

const HOME_TITLE_CLASSES =
  'm-[0_auto_0.5rem] w-[min(100%,34.375rem)] font-gsans text-[clamp(2.25rem,3.4vw,2.8125rem)] leading-[1.156] font-normal text-pretty text-cosci-fg text-center ' +
  'above-phone:row-2 desktop:w-[min(100%,43.5rem)] desktop:text-[clamp(2.35rem,3vw,2.8125rem)] ' +
  '[@media(min-width:1181px)_and_(max-height:760px)]:text-[clamp(2.1rem,2.8vw,2.65rem)] ' +
  'phone:m-0 phone:w-full phone:text-[clamp(1.85rem,7.6vw,2.35rem)] phone:leading-[1.16]';

const HOME_LOGO_CLASSES =
  'm-[auto_auto_1rem] block h-auto w-[2.5rem] self-center text-cosci-accent [&_path]:fill-current [&_path]:stroke-current';

const STEP_TIMELINE_CLASSES =
  "relative m-[clamp(3.7rem,8.5vh,5.2rem)_0_0] grid list-none grid-cols-3 gap-16 p-0 before:absolute before:top-4 before:right-4 before:left-4 before:h-px before:bg-cosci-step-line before:content-[''] " +
  'above-phone:row-3 desktop:row-4 desktop:mt-0 desktop:gap-x-0 ' +
  'tablet:mt-[clamp(2.75rem,6vh,4rem)] tablet:gap-0';

const STEP_ITEM_CLASSES =
  'relative z-1 grid gap-4 desktop:w-[min(100%,15.8rem)] tablet:w-[min(100%,14rem)] tablet:grid-cols-[1fr] tablet:gap-3.5';

const STEP_NUMBER_CLASSES =
  'grid size-[1.875rem] place-items-center rounded-full bg-cosci-accent text-[1rem] font-normal text-cosci-accent-fg [transition:background-color_0.3s_ease-in-out]';

const STEP_BODY_CLASSES =
  'mt-1 max-w-[16rem] text-[0.875rem] leading-[1.43] text-cosci-fg [@media(min-width:1181px)_and_(max-height:760px)]:text-[0.9rem] [@media(min-width:1181px)_and_(max-height:760px)]:leading-[1.28]';

const SUGGESTION_ROW_CLASSES =
  'mt-[clamp(3.2rem,7vh,4.35rem)] grid grid-cols-3 gap-3.5 above-phone:row-5 above-phone:mt-0 desktop:row-6 desktop:[align-self:end] phone:mt-auto phone:grid-cols-[minmax(0,1fr)] phone:gap-0.5';

const SUGGESTION_BUTTON_LAYOUT_CLASSES =
  'flex h-[4.4rem] max-h-[4.4rem] min-h-[4.4rem] w-full items-center overflow-hidden p-3 leading-[1.35] ' +
  '[@media(min-width:1181px)_and_(max-height:760px)]:max-h-[3.9rem] [@media(min-width:1181px)_and_(max-height:760px)]:min-h-[3.9rem] ' +
  'phone:h-auto phone:max-h-none phone:min-h-0 phone:gap-3.5 phone:px-3.5 phone:py-2.5';

// TruncatedLabel clips only at whole words; CSS text-overflow would cut
// letters.
const SUGGESTION_TEXT_CLASSES =
  'line-clamp-2 leading-[1.35] phone:block phone:min-w-0 phone:flex-auto phone:leading-[1.4] phone:whitespace-nowrap';

// The preview shows the full teaser: line clamps would cut letters, while
// viewport bounds permit whole-word wrapping.
const SUGGESTION_PREVIEW_CLASSES =
  'pointer-events-none absolute bottom-[calc(100%+0.5rem)] z-5 m-0 block w-max max-w-[min(46rem,calc(100vw-7rem))] text-[0.84rem] leading-[1.25rem] font-normal text-pretty text-cosci-shell-icon transition-[opacity,visibility] duration-short ease-standard phone:hidden';

// Anchor edge previews inward so they cannot overflow the viewport.
const SUGGESTION_PREVIEW_POSITION_CLASSES = [
  'left-0 text-left',
  'left-1/2 text-center',
  'right-0 text-right',
] as const;

function HomeScrollHint() {
  const onClick = () => {
    // Scroll this pane; scrollIntoView also moves clipped shell ancestors.
    smoothScrollToSection('landing', 0, '.ucs-page--home');
  };
  return (
    <Button
      variant="outlined"
      size="sm"
      trailingIcon="expand_more"
      layoutClassName="reference-home-scroll-hint"
      onClick={onClick}
    >
      Scroll to see how Open Co-Scientist works
    </Button>
  );
}

export function HomeStage(props: HomeStageProps) {
  const [hoveredSuggestion, setHoveredSuggestion] = useState<string | null>(
    null,
  );
  const isMobile = useIsMobile();

  return (
    <section className={HOME_STAGE_CLASSES}>
      <div className={HOME_MAIN_CLASSES}>
        <HomeGreeting isMobile={isMobile} />
        <HomeSuggestionRow
          isMobile={isMobile}
          suggestions={SUGGESTIONS}
          hoveredSuggestion={hoveredSuggestion}
          onPreview={setHoveredSuggestion}
          onSelect={prompt => {
            props.setInput(prompt);
            setHoveredSuggestion(null);
          }}
        />
        <Composer
          input={props.input}
          setInput={props.setInput}
          busy={false}
          large
          connectors={props.connectors}
          onSubmit={props.onSubmit}
        />
        {!isMobile && <HomeScrollHint />}
      </div>
      {!isMobile && (
        <HomeRecentsPanel
          runs={props.runs}
          showAll={props.showAllRecents}
          onToggleShowAll={props.onToggleShowAll}
        />
      )}
    </section>
  );
}

function HomeGreeting({isMobile}: {isMobile: boolean}) {
  return (
    <>
      {isMobile && (
        <GoogleLabsIcon aria-hidden="true" className={HOME_LOGO_CLASSES} />
      )}
      <h1 className={HOME_TITLE_CLASSES}>
        What breakthrough should we make today?
      </h1>
      {!isMobile && (
        <ol className={STEP_TIMELINE_CLASSES}>
          {SESSION_STEPS.map((step, index) => (
            <li
              key={step.n}
              className={joinClasses(
                STEP_ITEM_CLASSES,
                index === 1 && 'above-phone:justify-self-center',
                index === 2 && 'above-phone:[justify-self:end]',
              )}
            >
              <span className={STEP_NUMBER_CLASSES}>{step.n}</span>
              <div>
                <h2 className="m-0 text-[0.875rem] font-normal text-cosci-shell-icon">
                  {step.title}
                </h2>
                <p className={STEP_BODY_CLASSES}>{step.body}</p>
              </div>
            </li>
          ))}
        </ol>
      )}
    </>
  );
}

function HomeSuggestionRow({
  isMobile,
  suggestions,
  hoveredSuggestion,
  onPreview,
  onSelect,
}: {
  isMobile: boolean;
  suggestions: readonly Suggestion[];
  hoveredSuggestion: string | null;
  onPreview: (text: string | null) => void;
  onSelect: (text: string) => void;
}) {
  return (
    <div className={SUGGESTION_ROW_CLASSES}>
      {suggestions.map((suggestion, index) => (
        <SuggestionCard
          key={suggestion.preview}
          suggestion={suggestion}
          index={index}
          isMobile={isMobile}
          isPreviewed={hoveredSuggestion === suggestion.preview}
          onPreview={onPreview}
          onSelect={onSelect}
        />
      ))}
    </div>
  );
}

function SuggestionCard({
  suggestion,
  index,
  isMobile,
  isPreviewed,
  onPreview,
  onSelect,
}: {
  suggestion: Suggestion;
  index: number;
  isMobile: boolean;
  isPreviewed: boolean;
  onPreview: (preview: string | null) => void;
  onSelect: (prompt: string) => void;
}) {
  return (
    <div className="relative min-w-0 cursor-pointer">
      <SuggestionPreviewBubble
        text={suggestion.preview}
        isPreviewed={isPreviewed}
        position={Math.min(index, 2)}
      />
      <SuggestionTriggerButton
        suggestion={suggestion}
        isMobile={isMobile}
        isPreviewed={isPreviewed}
        onPreview={onPreview}
        onSelect={onSelect}
      />
    </div>
  );
}

function SuggestionPreviewBubble({
  text,
  isPreviewed,
  position,
}: {
  text: string;
  isPreviewed: boolean;
  position: number;
}) {
  const centered = position === 1;
  return (
    <p
      className={joinClasses(
        SUGGESTION_PREVIEW_CLASSES,
        SUGGESTION_PREVIEW_POSITION_CLASSES[position],
        isPreviewed ? 'visible opacity-100' : 'invisible opacity-0',
        isPreviewed
          ? centered
            ? '[transform:translateX(-50%)_translateY(0)]'
            : '[transform:translateY(0)]'
          : centered
            ? '[transform:translateX(-50%)_translateY(0.2rem)]'
            : '[transform:translateY(0.2rem)]',
      )}
      aria-hidden={!isPreviewed}
    >
      {text}
    </p>
  );
}

interface SuggestionTriggerButtonProps {
  suggestion: Suggestion;
  isMobile: boolean;
  isPreviewed: boolean;
  onPreview: (preview: string | null) => void;
  onSelect: (prompt: string) => void;
}

function SuggestionTriggerButton(props: SuggestionTriggerButtonProps) {
  const {suggestion, isMobile, isPreviewed, onPreview, onSelect} = props;
  return (
    <CardButton
      highlighted={isPreviewed}
      layoutClassName={SUGGESTION_BUTTON_LAYOUT_CLASSES}
      onPointerEnter={() => onPreview(suggestion.preview)}
      onPointerLeave={() => onPreview(null)}
      onFocus={() => onPreview(suggestion.preview)}
      onBlur={() => onPreview(null)}
      onClick={() => onSelect(suggestion.prompt)}
    >
      {/* Leading glyph is part of the phone list layout only; desktop cards
          are text-only. */}
      {isMobile && (
        <Icon
          className="size-[1.4rem] flex-none text-[1.4rem] text-cosci-shell-icon"
          name={suggestion.icon}
        />
      )}
      <TruncatedLabel
        className={SUGGESTION_TEXT_CLASSES}
        text={suggestion.preview}
        lines={isMobile ? 1 : 2}
      />
    </CardButton>
  );
}

// Suggestion teasers are not research goals; send the full prompt with output
// and scientific constraints.
export const SUGGESTIONS: readonly {
  preview: string;
  prompt: string;
  icon: IconName;
}[] = [
  {
    preview: 'Repurpose an approved drug to slow glioblastoma progression.',
    prompt:
      'Repurposing an approved drug for glioblastoma.\n\n' +
      'Suggest an existing, approved drug that could be repurposed to slow ' +
      'the progression of glioblastoma, and provide experimentally ' +
      'testable concentrations for an in-vitro proliferation assay in ' +
      'patient-derived glioblastoma cells. The drug should cross the ' +
      'blood-brain barrier and inhibit tumor cell growth.\n\n' +
      'The drug should have minimal cytotoxicity in healthy neural and glial ' +
      'cells, and should not have been experimentally tested for ' +
      'glioblastoma before.\n\n' +
      'Reason about the drug’s molecular mechanism, impacted pathways, and ' +
      'effect on tumor-cell proliferation. Describe the mechanism of ' +
      'action in detail, and reason about safety and toxicity, stating ' +
      'where no safety data exists.',
    icon: 'search',
  },
  {
    preview:
      'Find a mechanistic link between gut-microbiome metabolites and ' +
      'Parkinson’s.',
    prompt:
      'A novel hypothesis for gut-microbiome metabolites in Parkinson’s ' +
      'disease progression.\n\n' +
      'Develop a novel, mechanistic hypothesis explaining how specific ' +
      'gut-microbiome-derived metabolites influence the progression of ' +
      'Parkinson’s disease, focusing on the gut–brain axis and α-synuclein ' +
      'aggregation. Explain the mechanism of action in detail, from the ' +
      'metabolite to the molecular pathway to the neuronal phenotype.\n\n' +
      'Prioritize hypotheses that are novel — not already established in the ' +
      'literature — and consistent with known human and model-organism ' +
      'evidence.\n\n' +
      'Include a feasible experiment to test the hypothesis, specifying the ' +
      'model system (for example gnotobiotic mice or enteric neuron ' +
      'cultures) and the measurable readout.',
    icon: 'lightbulb',
  },
  {
    preview:
      'Resensitize multidrug-resistant Gram-negative bacteria to an ' +
      'antibiotic.',
    prompt:
      'A strategy to resensitize multidrug-resistant Gram-negative ' +
      'bacteria.\n\n' +
      'Propose a novel strategy to resensitize multidrug-resistant ' +
      'Gram-negative bacteria (for example carbapenem-resistant Klebsiella ' +
      'pneumoniae) to an existing antibiotic. The strategy should target a ' +
      'specific resistance mechanism — such as an efflux pump, β-lactamase ' +
      'activity, or outer-membrane permeability — and restore the potency ' +
      'of a clinically used drug.\n\n' +
      'The approach should be feasible with current laboratory techniques ' +
      'and should not depend on developing an entirely new class of ' +
      'antibiotic.\n\n' +
      'Explain the molecular mechanism in detail, reason about how readily ' +
      'resistance could emerge against the strategy itself, and outline an ' +
      'experimentally testable assay (for example a checkerboard MIC ' +
      'assay) to validate the resensitization.',
    icon: 'stars',
  },
];

type Suggestion = (typeof SUGGESTIONS)[number];

const SESSION_STEPS: readonly {
  n: number;
  title: string;
  body: string;
}[] = [
  {
    n: 1,
    title: 'Frame the research goal',
    body:
      'Describe the question, add useful context, and define what a ' +
      'strong hypothesis should satisfy.',
  },
  {
    n: 2,
    title: 'Generate hypotheses',
    body:
      'Co-Scientist explores mechanisms, evidence, and candidate ' +
      'explanations for the topic.',
  },
  {
    n: 3,
    title: 'Pressure-test the best ideas',
    body:
      'Hypotheses are compared against the criteria so the strongest ' +
      'directions rise to the top.',
  },
];

// 701-1180px keeps recents at natural height below the first screen rather
// than a desktop-style scroller; 1181px+ fills the stage column. Mobile never
// renders the panel.
const RECENTS_PANEL_CLASSES =
  'reference-recents-panel grid min-h-0 [align-content:start] gap-5 overflow-hidden pt-1 ui-motion-enter ' +
  'above-phone:mt-4 above-phone:min-h-auto above-phone:w-[min(100%,43rem)] above-phone:grid-rows-[auto_auto] above-phone:overflow-visible ' +
  'desktop:mt-0 desktop:h-full desktop:min-h-0 desktop:w-full desktop:max-h-[calc(100vh-4.5rem)] desktop:supports-[height:100dvh]:max-h-[calc(100dvh-4.5rem)] desktop:grid-rows-[auto_minmax(0,1fr)] desktop:[justify-self:end] desktop:gap-6 desktop:pt-[clamp(0.35rem,1vh,0.75rem)] desktop:pb-[clamp(1.35rem,3.2vh,2.25rem)]';

const RECENTS_PANEL_EMPTY_CLASSES = 'grid-rows-[auto_1fr] self-stretch pb-8';

const RECENTS_LIST_CLASSES =
  'ui-motion-enter-items m-0 grid min-h-0 list-none gap-7 desktop:scroll-p-[0.55rem_0.55rem_2.15rem]';

// Background-independent masks soften the scroll edge in both themes without
// matching surface colors; symmetric 1181px+ insets leave scrollbar slack so
// narrowed cards cannot overflow horizontally.
const RECENTS_LIST_SCROLL_CLASSES =
  'overflow-y-auto p-0 pr-2.5 [mask-image:linear-gradient(to_bottom,#000_calc(100%-2.25rem),transparent)] [-webkit-mask-image:linear-gradient(to_bottom,#000_calc(100%-2.25rem),transparent)] ' +
  'max-h-[calc(100vh-12rem)] supports-[height:100dvh]:max-h-[calc(100dvh-12rem)] above-phone:max-h-none above-phone:overflow-visible desktop:overflow-y-auto desktop:p-[0.55rem_0.55rem_2.15rem]';

// The empty state is not a scroller; a mask would dim the card itself.
const RECENTS_LIST_EMPTY_CLASSES = 'h-full overflow-hidden p-0';

// Its transition stays in home_surface.css: the global unlayered `a` rule
// would beat a utility.
const RECENT_CARD_CLASSES =
  'reference-recent-card grid min-h-[15.75rem] w-full cursor-pointer [align-content:start] gap-3 rounded-xl border border-cosci-suggestion-bg bg-cosci-composer-bg p-4 text-left text-cosci-fg no-underline shadow-card hover:bg-cosci-recent-card-hover-bg focus-visible:bg-cosci-recent-card-hover-bg desktop:min-h-0 desktop:border-transparent';

interface HomeRecentsPanelProps {
  runs: Run[];
  showAll: boolean;
  onToggleShowAll: () => void;
}

export function HomeRecentsPanel({
  runs,
  showAll,
  onToggleShowAll,
}: HomeRecentsPanelProps) {
  const visibleRuns = showAll ? runs : runs.slice(0, 4);
  const empty = visibleRuns.length === 0;
  return (
    <aside
      className={joinClasses(
        RECENTS_PANEL_CLASSES,
        empty && RECENTS_PANEL_EMPTY_CLASSES,
      )}
      aria-label="Recent runs"
    >
      <div className="flex items-center gap-2 text-th-muted-fg">
        <Icon className="size-5 desktop:size-[1.375rem]" name="history" />
        <h2 className="m-0 text-[1.15rem] font-semibold text-cosci-fg desktop:text-[1rem] desktop:leading-[1.5] desktop:font-medium">
          Recents
        </h2>
      </div>
      <ol
        className={joinClasses(
          RECENTS_LIST_CLASSES,
          empty ? RECENTS_LIST_EMPTY_CLASSES : RECENTS_LIST_SCROLL_CLASSES,
        )}
      >
        {empty ? (
          <li className="h-full min-h-0">
            <div className="box-border grid h-full min-h-[25rem] w-full place-items-center content-center gap-4 rounded-3xl border-[1.5px] border-dashed border-cosci-border p-6 text-center text-cosci-muted">
              <GoogleLabsIcon
                aria-hidden="true"
                className="block h-[1.95rem] w-[2.1rem] text-cosci-accent"
              />
              <strong className="max-w-[17rem] text-[1rem] leading-[1.35] font-[650] text-inherit">
                You have not started any sessions yet.
              </strong>
            </div>
          </li>
        ) : (
          visibleRuns.map(run => <RecentRunCard key={run.id} run={run} />)
        )}
        {runs.length > 4 && (
          <li className="flex justify-center pt-1 pb-2">
            <Button
              variant="disclosure"
              size="sm"
              trailingIcon={showAll ? 'expand_less' : 'expand_more'}
              onClick={onToggleShowAll}
            >
              {showAll ? 'Show less' : 'Show more'}
            </Button>
          </li>
        )}
      </ol>
    </aside>
  );
}

function RecentCardMeta({run}: {run: Run}) {
  const nowSeconds = useNowTick(1000, isActiveStatus(run.status));
  return (
    <span className="flex flex-wrap gap-1.5">
      <Chip size="xs" shape="tag">
        {formatDate(run.updated_at)}
      </Chip>
      <Chip size="xs" shape="tag">
        {formatHomeRunTimeChip(run, nowSeconds)}
      </Chip>
    </span>
  );
}

function RecentRunCard({run}: {run: Run}) {
  const active = isActiveStatus(run.status);
  return (
    <li>
      <Link
        to={run.is_demo ? examplePath(run.id) : resultsPath(run.id)}
        className={joinClasses(RECENT_CARD_CLASSES, active && 'is-active-run')}
        // The labels below are cut to fit; assistive tech still gets the goal.
        aria-description={run.research_goal}
      >
        <RecentCardMeta run={run} />
        <TruncatedLabel
          className="line-clamp-2 text-[1rem] leading-[1.5] font-medium"
          text={displayTitle(run.title, run.research_goal, firstSentenceClause)}
          lines={2}
        />
        <TruncatedLabel
          className="reference-recent-description line-clamp-3 text-[0.9rem] leading-[1.35] text-cosci-shell-icon desktop:text-[0.94rem] desktop:leading-[1.34]"
          text={run.research_goal}
          lines={3}
        />
        {active ? (
          <RunStepFlow run={run} />
        ) : (
          isCompletedStatus(run.status) && <RecentRunResults run={run} />
        )}
      </Link>
    </li>
  );
}

function RecentRunResults({run}: {run: Run}) {
  const topIdeas = run.top_hypotheses ?? [];
  const topScore = run.top_elo ?? null;
  return (
    <>
      <span className="flex flex-nowrap items-center gap-1.5">
        <Chip size="xs" shape="tag" tone="success" icon="emoji_events">
          Winning ideas
        </Chip>
        {topScore !== null && (
          <Chip size="xs" shape="tag" tone="success" icon="stars">
            Top score: {topScore}
          </Chip>
        )}
      </span>
      {topIdeas.length > 0 && (
        <ol className="m-0 mt-0.5 grid list-none gap-2.5 p-0 text-[0.75rem] leading-[1.33] tracking-[0.1px] text-cosci-fg desktop:gap-2">
          {topIdeas.map((idea, index) => (
            <li
              key={idea}
              className="grid grid-cols-[1.4rem_minmax(0,1fr)] gap-1"
            >
              <span>{index + 1}.</span>
              <TruncatedLabel
                className="line-clamp-2 min-w-0"
                text={idea}
                lines={2}
              />
            </li>
          ))}
        </ol>
      )}
    </>
  );
}

function formatHomeRunTimeChip(run: Run, nowSeconds: number): string {
  if (isCompletedStatus(run.status)) {
    const endTime = run.completed_at ?? run.updated_at;
    const duration = endTime ? endTime - run.created_at : -1;
    return `Total time: ${duration < 0 ? capitalizeTerm(run.status) : formatDurationPhrase(duration, {subMinute: true})}`;
  }
  if (isActiveStatus(run.status)) {
    return `Time elapsed: ${formatDurationPhrase(Math.max(0, nowSeconds - run.created_at), {subMinute: true})}`;
  }
  return `Status: ${capitalizeTerm(run.status)}`;
}

// Routing has no phase; leased tasks and stage events describe the same work.
const TASK_PHASE: Record<string, number | null> = {
  bootstrap: 1,
  supervisor: 1,
  orchestrator: null,
  generate: 2,
  generation: 2,
  literature_review: 2,
  reflection: 3,
  comprehensive_reflection: 3,
  review: 3,
  verification: 3,
  deep_verification: 3,
  safety_screen: 3,
  ranking: 4,
  proximity: 4,
  evolve: 4,
  meta_review: 4,
  research_overview: 4,
  finalize: 4,
};

const STAGE_TYPES = new Set([
  'supervisor.plan',
  'literature_review',
  'generate',
  'reflection',
  'proximity',
  'ranking',
  'evolve',
  'meta_review',
  'deep_verification',
  'research_overview',
]);

export function homeRunStepIndex(run: Run): number | null {
  if (run.status === 'queued') return 1;
  if (run.status === 'synthesizing') return 4;
  const task = run.execution_progress?.active_task;
  if (task) {
    const [, first = '', second = ''] = task.split('.');
    const phase =
      TASK_PHASE[first === 'node' || first === 'fanout' ? second : first] ??
      null;
    if (phase !== null) return phase;
  }
  const stage = run.latest_stage;
  if (!stage || !STAGE_TYPES.has(stage)) return null;
  return TASK_PHASE[stage === 'supervisor.plan' ? 'supervisor' : stage] ?? null;
}

const RUN_STEPS: {icon: IconName; label: string}[] = [
  {icon: 'summarize', label: 'Exploring focus areas'},
  {icon: 'rate_review', label: 'Generating hypotheses'},
  {icon: 'reviews', label: 'Reviewing hypotheses'},
  {icon: 'chess', label: 'Playing tournament'},
];

const RUN_STEP_CLASSES =
  'flex items-center gap-2.5 text-[0.85rem] leading-[1.2] font-medium text-cosci-fg';

const RUN_STEP_DELIMITER_CLASSES =
  'm-[0.2rem_0_0.2rem_0.625rem] h-3 border-s border-s-cosci-border';

// Show the latest phase, not the furthest: later cycles legitimately return to
// generation and review.
export function RunStepFlow({run}: {run: Run}) {
  const phase = homeRunStepIndex(run);
  // Hold the last observed phase through routing gaps; a reported phase
  // replaces it even when it moves backward.
  const [currentPhase, setCurrentPhase] = useState(phase ?? 1);
  useEffect(() => {
    if (phase === null) return;
    setCurrentPhase(phase);
  }, [phase]);
  const revealed = RUN_STEPS.slice(0, currentPhase);

  return (
    <div className="mt-0.5 grid gap-3">
      <div className="ui-motion-enter-items grid">
        <div className={RUN_STEP_CLASSES}>
          <span
            aria-hidden="true"
            className="size-5 shrink-0 animate-[reference-run-step-spin_0.8s_linear_infinite] rounded-full border-2 border-[color:color-mix(in_srgb,var(--color-th-primary)_28%,transparent)] border-t-th-primary motion-reduce:animate-none"
          />
          <span className="reference-run-step-label min-w-0">In Progress</span>
        </div>
        <div aria-hidden="true" className={RUN_STEP_DELIMITER_CLASSES} />
        {revealed.map((step, index) => (
          <Fragment key={step.label}>
            <RunStepItem icon={step.icon} label={step.label} />
            {index < revealed.length - 1 && (
              <div aria-hidden="true" className={RUN_STEP_DELIMITER_CLASSES} />
            )}
          </Fragment>
        ))}
      </div>
    </div>
  );
}

function RunStepItem({icon, label}: {icon: IconName; label: string}) {
  return (
    <div className={RUN_STEP_CLASSES}>
      <Icon
        className="reference-run-step-icon size-5 shrink-0 text-cosci-shell-icon"
        name={icon}
      />
      <span className="reference-run-step-label min-w-0">{label}</span>
    </div>
  );
}
