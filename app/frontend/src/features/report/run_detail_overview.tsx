import {Card, ExternalLink} from '@/shared/ui';
import type {
  AgentInsights,
  Hypothesis,
  MatchRow,
  RecommendedDirection,
  Report,
  ReportPayload,
  ResearchOverview,
  RunWithSummary,
} from '@/shared/api/runs';
import {isRecord, readableText, readableTextList} from '@/shared/lib/text';
import {useMemo} from 'react';
import {
  REPORT_H3_CLASSES,
  REPORT_H4_CLASSES,
  REPORT_LIST_CLASSES,
  REPORT_SECTION_CLASSES,
  ReportDocument,
} from './run_detail_shell';
import {formatDurationPhrase} from '@/shared/lib/time';

// Keep an empty leaderboard's identity stable so it cannot invalidate
// downstream memos on every render.
const NO_LEADERBOARD: NonNullable<ReportPayload['leaderboard']> = [];

function overviewReportStats(
  report: Report | null,
  hypotheses: Hypothesis[],
  matches: MatchRow[],
): {
  overview: ResearchOverview | undefined;
  leaderboard: NonNullable<ReportPayload['leaderboard']>;
  ideaCount: number;
  matchCount: number;
} {
  if (!report) {
    return {
      overview: undefined,
      leaderboard: NO_LEADERBOARD,
      ideaCount: hypotheses.length,
      matchCount: matches.length,
    };
  }
  const {payload} = report;
  return {
    overview: payload.research_overview,
    leaderboard: payload.leaderboard ?? NO_LEADERBOARD,
    // Explored counts cover the whole pool, not released ideas.
    ideaCount: payload.idea_count ?? 0,
    matchCount: payload.match_count ?? 0,
  };
}

function winningIdeasItems(
  leaderboard: NonNullable<ReportPayload['leaderboard']>,
  hypotheses: Hypothesis[],
): {id: string; title: string; elo: number}[] {
  if (leaderboard.length) {
    return leaderboard
      .slice(0, 5)
      .map(item => ({id: item.id, title: item.title, elo: item.elo}));
  }
  return hypotheses
    .slice(0, 5)
    .map(h => ({id: h.id, title: h.title, elo: h.elo_rating}));
}

function useResearchOverviewDerived({
  run,
  leaderboard,
  hypotheses,
  ideaCount,
  matchCount,
}: {
  run: RunWithSummary | null;
  leaderboard: NonNullable<ReportPayload['leaderboard']>;
  hypotheses: Hypothesis[];
  ideaCount: number;
  matchCount: number;
}) {
  const leadStat = useMemo(
    () =>
      researchOverviewLeadStat({
        run,
        leaderboard,
        hypotheses,
        ideaCount,
        matchCount,
      }),
    [run, leaderboard, hypotheses, ideaCount, matchCount],
  );

  const winningIdeas = useMemo(
    () => winningIdeasItems(leaderboard, hypotheses),
    [leaderboard, hypotheses],
  );

  return {leadStat, winningIdeas};
}

export function ResearchOverviewView({
  run,
  report,
  hypotheses,
  matches,
}: {
  run: RunWithSummary | null;
  report: Report | null;
  hypotheses: Hypothesis[];
  matches: MatchRow[];
}) {
  const {overview, leaderboard, ideaCount, matchCount} = overviewReportStats(
    report,
    hypotheses,
    matches,
  );
  const {leadStat, winningIdeas} = useResearchOverviewDerived({
    run,
    leaderboard,
    hypotheses,
    ideaCount,
    matchCount,
  });

  return (
    <>
      <ReportDocument title="Summary">
        {leadStat ? (
          <p className="cosci-overview-lead-stat mt-1 mb-4 text-cosci-fg">
            {leadStat}
          </p>
        ) : null}
        <RetrievalDegradationNotice report={report} />
        <SummaryStats payload={report?.payload} />
        <AgentInsightsSection
          insights={report?.payload.agent_insights}
          degraded={sectionDegraded(report, META_REVIEW_SCHEMA)}
        />
        <OverviewSummary
          overview={overview}
          degraded={sectionDegraded(report, RESEARCH_OVERVIEW_SCHEMA)}
        />
        <ResearchDirectionsSection overview={overview} />
        <SpecificAimsSection overview={overview} />
        <ResearchContactsSection overview={overview} />
        <WinningIdeasSection items={winningIdeas} />
        <TournamentSummarySection matches={matches} />
      </ReportDocument>
    </>
  );
}

function bucketCount(
  payload: ReportPayload,
  key: 'high_potential' | 'non_viable',
): number {
  return payload.idea_buckets?.[key].length ?? 0;
}

// Verified counts and Unverified badges share the server support derivation,
// not High Potential counts.
function reportStats(payload: ReportPayload): (readonly [string, number])[] {
  return [
    ['High Potential', bucketCount(payload, 'high_potential')],
    ['Non-Viable', bucketCount(payload, 'non_viable')],
    ['Verified ideas', payload.verified_count ?? 0],
    ['Sources Analyzed', payload.evidence_count ?? 0],
  ] as const;
}

function SummaryStats({payload}: {payload: ReportPayload | undefined}) {
  if (!payload) return null;
  const stats = reportStats(payload);
  return (
    <dl className="grid grid-cols-4 gap-3 max-[900px]:grid-cols-2">
      {stats.map(([label, value]) => (
        <Card key={label} size="tile">
          <dt className="text-sm text-cosci-muted">{label}</dt>
          <dd className="mt-1 text-2xl font-medium">{value}</dd>
        </Card>
      ))}
    </dl>
  );
}

// Contacts are restricted to authors of analyzed sources.
function ResearchContactsSection({
  overview,
}: {
  overview: ResearchOverview | undefined;
}) {
  const contacts = overview?.research_contacts;
  if (!contacts?.length) return null;
  return (
    <section className={REPORT_SECTION_CLASSES}>
      <h3 className={REPORT_H3_CLASSES}>Research contacts</h3>
      <p>
        Relevant authors identified from the literature analyzed in this run.
      </p>
      {contacts.map(contact => {
        const direction = readableText(contact.research_direction);
        return (
          <div key={contact.candidate_id}>
            <h4 className={REPORT_H4_CLASSES}>{readableText(contact.name)}</h4>
            {direction ? (
              <p>
                <strong>Research direction: </strong>
                {direction}
              </p>
            ) : null}
            <p>{readableText(contact.expertise)}</p>
            {/* R14-16: Justification: is the one label consistent across
                all 14 published research contacts that carry it. */}
            <p>
              <strong>Justification: </strong>
              {readableText(contact.justification)}
            </p>
            {/* R14-16: the second, evidence-citing field's published label
                varies freely; "Supporting article" is this schema's own
                fixed name for it -- always exactly one grounded paper
                (source_title/source_url), never free citation prose. */}
            <ExternalLink
              className="text-th-primary underline"
              href={contact.source_url}
              fallback={
                <p>Supporting article: {readableText(contact.source_title)}</p>
              }
            >
              Supporting article: {readableText(contact.source_title)}
            </ExternalLink>
          </div>
        );
      })}
    </section>
  );
}

function overviewSummaryText(overview: ResearchOverview | undefined): string {
  return readableText(overview?.overview?.summary);
}

// Degraded blank summaries are generation failures, not promises of synthesis
// still in progress.
function OverviewSummary({
  overview,
  degraded = false,
}: {
  overview: ResearchOverview | undefined;
  degraded?: boolean;
}) {
  const summary = overviewSummaryText(overview);
  if (summary) return <p>{summary}</p>;
  if (degraded) return <DegradedSectionNotice />;
  return (
    <p>
      The research overview appears after Co-Scientist finishes the final
      synthesis step.
    </p>
  );
}

function WinningIdeasSection({
  items,
}: {
  items: {id: string; title: string; elo: number}[];
}) {
  if (!items.length) return null;
  return (
    <section className={REPORT_SECTION_CLASSES}>
      <h3 className={REPORT_H3_CLASSES}>Winning ideas</h3>
      <ol className={REPORT_LIST_CLASSES}>
        {items.map(item => (
          <li className="my-2.5 grid gap-0.5" key={item.id}>
            <strong>{item.title}</strong>
            <span className="text-[0.88rem] text-cosci-muted">
              Elo rating: {item.elo}
            </span>
          </li>
        ))}
      </ol>
    </section>
  );
}

function TournamentSummarySection({matches}: {matches: MatchRow[]}) {
  return (
    <section className={REPORT_SECTION_CLASSES}>
      <h3 className={REPORT_H3_CLASSES}>Tournament summary</h3>
      <p>
        {matches.length
          ? `${matches.length} tournament matches have been recorded ` +
            'for this run.'
          : 'Tournament matches appear here once ranking begins.'}
      </p>
    </section>
  );
}

function pluralPhrase(count: number, singular: string, plural: string): string {
  return count === 1 ? singular : plural;
}

function researchOverviewLeadStat({
  run,
  leaderboard,
  hypotheses,
  ideaCount,
  matchCount,
}: {
  run: RunWithSummary | null;
  leaderboard: {elo: number}[];
  hypotheses: Hypothesis[];
  ideaCount: number;
  matchCount: number;
}): string {
  if (!ideaCount) return '';

  const duration = runDurationPhrase(run);
  const highestElo = Math.max(
    0,
    ...leaderboard.map(item => item.elo),
    ...hypotheses.map(hypothesis => hypothesis.elo_rating),
  );

  const clauses = [
    duration ? ` over ${duration}` : '',
    highestElo > 0
      ? ` with the highest Elo rating of ${highestElo} points`
      : '',
    matchCount > 0
      ? ` and a total of ${matchCount} ` +
        `${pluralPhrase(matchCount, 'match was', 'matches were')} played`
      : '',
  ];
  const ideaLabel = pluralPhrase(ideaCount, 'idea was', 'ideas were');
  const sentence =
    `A total of ${ideaCount} ${ideaLabel} explored` +
    clauses.filter(Boolean).join('');
  return `${sentence}.`;
}

function runTimestampPair(
  run: RunWithSummary | null,
): {completedAt: number; createdAt: number} | null {
  if (!run?.completed_at || !run.created_at) return null;
  return {completedAt: run.completed_at, createdAt: run.created_at};
}

function runDurationPhrase(run: RunWithSummary | null): string {
  const pair = runTimestampPair(run);
  if (!pair) return '';
  const seconds = pair.completedAt - pair.createdAt;
  if (!Number.isFinite(seconds) || seconds <= 0) return '';
  return formatDurationPhrase(seconds);
}

function SpecificAimsSection({
  overview,
}: {
  overview: ResearchOverview | undefined;
}) {
  const specificAims = overview?.nih_specific_aims;
  if (!specificAims?.aims?.length) return null;
  return (
    <section className={REPORT_SECTION_CLASSES}>
      <h3 className={REPORT_H3_CLASSES}>Specific aims</h3>
      {AIMS_PREAMBLE_FIELDS.map(([key, heading]) => (
        <LabeledBlock
          key={key}
          heading={heading}
          text={readableText(specificAims[key])}
        />
      ))}
      {specificAims.aims.map((aim, index) => (
        <SpecificAim key={index} aim={aim} number={index + 1} />
      ))}
      {AIMS_CLOSING_FIELDS.map(([key, heading]) => (
        <LabeledBlock
          key={key}
          heading={heading}
          text={readableText(specificAims[key])}
        />
      ))}
    </section>
  );
}

const AIMS_PREAMBLE_FIELDS = [
  ['disease_description', 'Disease description'],
  ['unmet_need', 'Unmet need'],
  ['proposed_solution', 'Proposed solution'],
] as const;
const AIMS_CLOSING_FIELDS = [['pilot_evaluation', 'Pilot evaluation']] as const;
// Keep goals labeled under numbered aims, in the same field order as markdown.
const AIM_BODY_FIELDS = [
  ['overarching_goal', 'Overarching goal'],
  ['hypothesis', 'Hypothesis'],
  ['reasoning', 'Reasoning'],
] as const;

function LabeledBlock({heading, text}: {heading: string; text: string}) {
  if (!text) return null;
  if (!heading) return <p>{text}</p>;
  return (
    <div>
      <h4 className={REPORT_H4_CLASSES}>{heading}</h4>
      <p>{text}</p>
    </div>
  );
}

function SpecificAim({aim, number}: {aim: unknown; number: number}) {
  const record = isRecord(aim) ? aim : {};
  return (
    <div>
      <h4 className={REPORT_H4_CLASSES}>Specific Aims {number}</h4>
      {AIM_BODY_FIELDS.map(([key, label]) => {
        const text = readableText(record[key]);
        if (!text) return null;
        return (
          <p key={key}>
            <strong>{label}: </strong>
            {text}
          </p>
        );
      })}
    </div>
  );
}

interface SubTopicEntry {
  title: string;
  why: string;
  what: string;
  exampleIdea: string;
  questions: string[];
}

interface DirectionEntry {
  title: string;
  importance: string;
  recentFindings: string;
  experiments: string[];
  subTopics: SubTopicEntry[];
}

function toSubTopicEntry(raw: unknown): SubTopicEntry {
  const record = isRecord(raw) ? raw : {};
  return {
    title: readableText(record.title),
    why: readableText(record.why),
    what: readableText(record.what),
    exampleIdea: readableText(record.example_idea),
    questions: readableTextList(record.specific_questions),
  };
}

function subTopicEntries(raw: unknown): SubTopicEntry[] {
  const list = Array.isArray(raw) ? raw : [];
  return list
    .map(toSubTopicEntry)
    .filter(
      e => e.title || e.why || e.what || e.exampleIdea || e.questions.length,
    );
}

function toDirectionEntry(raw: unknown): DirectionEntry {
  const record = isRecord(raw) ? raw : {};
  return {
    title: readableText(record.title),
    importance: readableText(record.importance),
    recentFindings: readableText(record.recent_findings),
    experiments: readableTextList(record.suggested_experiments),
    subTopics: subTopicEntries(record.sub_topics),
  };
}

function directionEntries(
  overview: ResearchOverview | undefined,
): DirectionEntry[] {
  const raw = overview?.overview?.research_directions as unknown;
  const list = Array.isArray(raw) ? raw : [];
  return list
    .map(toDirectionEntry)
    .filter(
      e =>
        e.title ||
        e.importance ||
        e.recentFindings ||
        e.experiments.length ||
        e.subTopics.length,
    );
}

// Preview only multiple named directions; a single title duplicates rather
// than orients.
function DirectionsPreview({directions}: {directions: DirectionEntry[]}) {
  const titles = directions.map(d => d.title).filter(Boolean);
  if (titles.length < 2) return null;
  return (
    <>
      <p>We will be focusing on these research directions:</p>
      <ul className={REPORT_LIST_CLASSES}>
        {titles.map((title, i) => (
          <li key={`${title}-${i}`}>{title}</li>
        ))}
      </ul>
    </>
  );
}

const SUB_TOPIC_BODY_FIELDS = [
  ['why', 'Why'],
  ['what', 'What'],
  ['exampleIdea', 'Example idea'],
] as const;

function SubTopicLine({label, text}: {label: string; text: string}) {
  if (!text) return null;
  return (
    <p>
      <strong>{label}: </strong>
      {text}
    </p>
  );
}

function SubTopicItem({subTopic}: {subTopic: SubTopicEntry}) {
  return (
    <div>
      {subTopic.title ? (
        <p>
          <strong>{subTopic.title}</strong>
        </p>
      ) : null}
      {SUB_TOPIC_BODY_FIELDS.map(([key, label]) => (
        <SubTopicLine key={key} label={label} text={subTopic[key]} />
      ))}
      {subTopic.questions.length ? (
        <ul className={REPORT_LIST_CLASSES}>
          {subTopic.questions.map((question, i) => (
            <li key={`${question}-${i}`}>{question}</li>
          ))}
        </ul>
      ) : null}
    </div>
  );
}

function ResearchDirectionsSection({
  overview,
}: {
  overview: ResearchOverview | undefined;
}) {
  const directions = directionEntries(overview);
  if (!directions.length) return null;
  return (
    <section className={REPORT_SECTION_CLASSES}>
      <h3 className={REPORT_H3_CLASSES}>Research directions</h3>
      <DirectionsPreview directions={directions} />
      {directions.map((direction, index) => (
        <div key={direction.title || index}>
          <h4 className={REPORT_H4_CLASSES}>{direction.title}</h4>
          {direction.importance ? <p>{direction.importance}</p> : null}
          {direction.recentFindings ? (
            <p>
              <strong>Recent findings: </strong>
              {direction.recentFindings}
            </p>
          ) : null}
          {direction.experiments.length ? (
            <ul className={REPORT_LIST_CLASSES}>
              {direction.experiments.map((experiment, i) => (
                <li key={`${experiment}-${i}`}>{experiment}</li>
              ))}
            </ul>
          ) : null}
          {direction.subTopics.map((subTopic, i) => (
            <SubTopicItem key={subTopic.title || i} subTopic={subTopic} />
          ))}
        </div>
      ))}
    </section>
  );
}

const DEGRADED_SECTION_NOTICE_TEXT =
  'This section could not be generated after repeated attempts.';

const META_REVIEW_SCHEMA = 'meta_review';
const RESEARCH_OVERVIEW_SCHEMA = 'research_overview';

function DegradedSectionNotice() {
  return (
    <p className="text-[0.86rem] italic text-cosci-muted">
      {DEGRADED_SECTION_NOTICE_TEXT}
    </p>
  );
}

// Omit unknown capability identifiers rather than expose unexplained
// internals; the degradation notice remains true.
const LOST_CAPABILITY_LABELS: Record<string, string> = {
  literature_review: 'the literature review',
  observation_review: 'observation reviews',
  deep_research: 'follow-up research',
  review_evidence: 'evidence for the deep reviews',
  verification_probes: 'verification probes',
  evolution_grounding: 'grounding for evolved ideas',
};

const FLOOR_LABELS: Record<string, string> = {
  none: 'Nothing else was available to search.',
  run_attachments:
    'Only the documents attached to this run were available to search.',
};

// A run can complete without reachable literature; disclose that its ordinary-
// looking report is ungrounded.
function RetrievalDegradationNotice({report}: {report: Report | null}) {
  const degradation = report?.payload.retrieval_degradation;
  if (!degradation) return null;
  return (
    <p className="text-[0.86rem] italic text-cosci-muted">
      {retrievalDegradationText(degradation)}
    </p>
  );
}

function retrievalDegradationText(degradation: {
  lost: string[];
  floor: string;
}): string {
  const lost = (degradation.lost ?? [])
    .map(name => LOST_CAPABILITY_LABELS[name])
    .filter(label => !!label);
  const without = lost.length
    ? `, so it ran without ${joinReadable(lost)}`
    : '';
  const floor = FLOOR_LABELS[degradation.floor] ?? '';
  return `No literature source was reachable during this run${without}. ${floor}`.trim();
}

function joinReadable(items: string[]): string {
  if (items.length < 2) return items[0] ?? '';
  return `${items.slice(0, -1).join(', ')} and ${items[items.length - 1]}`;
}

function sectionDegraded(report: Report | null, schema: string): boolean {
  return (report?.payload.degraded_sections ?? []).includes(schema);
}

const EMPTY_INSIGHTS: Required<AgentInsights> = {
  key_findings: [],
  uncertainties: [],
  contradictions: [],
  recommended_directions: [],
  next_experiments: [],
};

function InsightList({title, values}: {title: string; values: string[]}) {
  const items = (values ?? [])
    .map(value => readableText(value).trim())
    .filter(value => value.length > 0);
  if (!items.length) return null;
  return (
    <div>
      <h4 className={REPORT_H4_CLASSES}>{title}</h4>
      <ul className={REPORT_LIST_CLASSES}>
        {items.map(item => (
          <li key={item}>{item}</li>
        ))}
      </ul>
    </div>
  );
}

function toRecommendation(raw: RecommendedDirection) {
  return {
    focusArea: readableText(raw.focus_area).trim(),
    recommendation: readableText(raw.recommendation).trim(),
    justification: readableText(raw.justification).trim(),
  };
}

function RecommendedDirections({
  directions,
}: {
  directions: RecommendedDirection[];
}) {
  const entries = (directions ?? [])
    .map(toRecommendation)
    .filter(entry => entry.recommendation || entry.focusArea);
  if (!entries.length) return null;
  return (
    <div>
      <h4 className={REPORT_H4_CLASSES}>Recommended directions</h4>
      {entries.map(entry => (
        <div key={entry.focusArea + entry.recommendation} className="mt-3">
          {entry.focusArea ? (
            <p className="font-medium">{entry.focusArea}</p>
          ) : null}
          <p>{entry.recommendation}</p>
          {entry.justification ? (
            <p className="text-cosci-muted">{entry.justification}</p>
          ) : null}
        </div>
      ))}
    </div>
  );
}

// Expose degraded synthesis rather than treat silence as success; findings
// must not leak private reasoning traces.
function AgentInsightsSection({
  insights,
  degraded = false,
}: {
  insights: AgentInsights | undefined;
  degraded?: boolean;
}) {
  if (!insights && !degraded) return null;
  return (
    <AgentInsightsBody
      content={completeInsights(insights ?? EMPTY_INSIGHTS)}
      degraded={degraded}
    />
  );
}

function completeInsights(content: AgentInsights): Required<AgentInsights> {
  return {...EMPTY_INSIGHTS, ...content};
}

function AgentInsightsBody({
  content,
  degraded,
}: {
  content: Required<AgentInsights>;
  degraded: boolean;
}) {
  return (
    <section className={REPORT_SECTION_CLASSES}>
      <h3 className={REPORT_H3_CLASSES}>Agent Insights</h3>
      {degraded ? <DegradedSectionNotice /> : null}
      <InsightList title="Key findings" values={content.key_findings} />
      <InsightList title="Uncertainties" values={content.uncertainties} />
      <InsightList title="Contradictions" values={content.contradictions} />
      <RecommendedDirections directions={content.recommended_directions} />
      <InsightList title="Next experiments" values={content.next_experiments} />
    </section>
  );
}
