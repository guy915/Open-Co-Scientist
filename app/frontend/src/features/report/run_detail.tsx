import {
  isCompletedStatus,
  isTerminalNonCompletedStatus,
  runActivity,
  runGoal,
  type RunActivity,
  type RunWithSummary,
  type TerminalNonCompletedStatus,
} from '@/shared/api/runs';
import {useRunHistoryContext} from '@/shared/hooks/history_context';
import {useNavigate, useParams} from 'react-router-dom';
import {RunRecovery} from './run_recovery';
import {IdeasTab} from './ideas_tab';
import {normalizeTab, TABS, type TabName} from '@/shared/lib/run_tabs';
import {ActiveRunView} from './run_detail_active';
import {useRunDetailData} from './run_detail_data';
import {LearningView} from './run_detail_learning';
import {ResearchOverviewView} from './run_detail_overview';
import {
  AwaitingDecisionNotice,
  ReportErrorAlert,
  reportSectionLabel,
  ReportTabNav,
  ReportTitlebar,
  ReportUngroundedNotice,
  RunEndState,
  RunToast,
  useTabNavigation,
} from './run_detail_shell';
import {RunSpecificationsView} from './run_detail_specifications';
import {joinClasses} from '@/shared/ui/classes';
import {DocumentSkeleton} from '@/shared/ui';

const REPORT_SCROLL_CLASSES =
  'cosci-report-scroll min-h-0 overflow-auto phone:overflow-x-hidden';

// A failed refresh's alert sits above the body in its own row instead of taking
// the body's row; alone, the body spans both.
const REPORT_BODY_CLASSES =
  'grid min-h-0 min-w-0 grid-rows-[auto_minmax(0,1fr)] [&>:only-child]:row-span-2';

type RunDetailData = ReturnType<typeof useRunDetailData>;

// Use history status before fetch completion to avoid flashing finished-report
// chrome for a running run.
function useRunActivity(
  id: string | undefined,
  data: RunDetailData,
): RunActivity {
  const {history} = useRunHistoryContext();
  if (data.loaded) return data.run ? runActivity(data.run.status) : 'inactive';
  return runActivity(history.find(run => run.id === id)?.status);
}

const REPORT_ROWS_WITH_TABS =
  'grid-rows-[3.75rem_5rem_minmax(0,1fr)] ' +
  'phone:grid-rows-[3.25rem_4.25rem_minmax(0,1fr)] ' +
  '[@media(max-height:500px)]:grid-rows-[3rem_3rem_minmax(0,1fr)]';

const REPORT_ROWS_WITHOUT_TABS =
  'grid-rows-[3.75rem_minmax(0,1fr)] ' +
  'phone:grid-rows-[3.25rem_minmax(0,1fr)] ' +
  '[@media(max-height:500px)]:grid-rows-[3rem_minmax(0,1fr)]';

function reportPageClasses(showTabs: boolean): string {
  // Keep horizontal overflow available to the mobile shell fallback; inner
  // report content owns vertical scrolling.
  return `cosci-report-page grid h-full min-h-0 bg-cosci-bg text-cosci-fg phone:min-w-0 phone:overflow-x-auto phone:overflow-y-hidden ${
    showTabs ? REPORT_ROWS_WITH_TABS : REPORT_ROWS_WITHOUT_TABS
  }`;
}

type RunWithBackend = RunWithSummary & {
  llm_backend?: 'offline' | 'real' | null;
};

// The persisted backend is authoritative, not the current process mode.
function runUsedOffline(run: RunWithSummary): boolean {
  return (run as RunWithBackend).llm_backend === 'offline';
}

// Flag completed runs without retrieved literature as ungrounded, except
// illustrative offline runs.
function reportIsUngrounded(data: RunDetailData): boolean {
  const run = data.run;
  if (!data.loaded || !run || !isCompletedStatus(run.status)) return false;
  if (data.evidence.length > 0) return false;
  return !runUsedOffline(run);
}

function terminalEndStateOf(run: RunWithSummary | null): {
  status: TerminalNonCompletedStatus;
  error: string | null;
  failureKind: string | null | undefined;
} | null {
  if (run && isTerminalNonCompletedStatus(run.status)) {
    return {
      status: run.status,
      error: run.error,
      failureKind: run.failure_kind,
    };
  }
  return null;
}

function isInitialLoading(data: RunDetailData): boolean {
  return !data.loaded && !data.error;
}

// A run that never loaded (unknown id, failed first fetch) has no report to
// show; the alert is the whole page.
function failedWithoutRun(data: RunDetailData): boolean {
  return data.loaded && !data.run;
}

function awaitingDecisionCount(data: RunDetailData): number {
  return data.run?.awaiting_decision_count ?? 0;
}

export function RunDetail() {
  const {id, tab} = useParams<{id: string; tab?: string}>();
  const activeTab = normalizeTab(tab);
  const {ideasViewKey, onTabChange} = useTabNavigation(id, activeTab);
  const data = useRunDetailData(id);
  const activity = useRunActivity(id, data);

  if (!id) return null;

  // Show terminal failure instead of tabs that would present partial output as
  // a finished report.
  const showEndState =
    terminalEndStateOf(data.run) !== null || data.run?.can_resume === true;
  const showTabs =
    activity === 'inactive' && !showEndState && !failedWithoutRun(data);
  return (
    <div className={reportPageClasses(showTabs)}>
      <ReportTitlebar title={data.title} activeTab={activeTab} />

      {showTabs && (
        <ReportTabNav
          activeTab={activeTab}
          onTabChange={onTabChange}
          tabs={TABS}
        />
      )}

      <div className={REPORT_BODY_CLASSES}>
        <ReportErrorAlert message={data.error} />
        <RunDetailBody
          active={activity === 'active'}
          activeTab={activeTab}
          ideasViewKey={ideasViewKey}
          data={data}
        />
      </div>

      <RunToast message={data.toast} />
    </div>
  );
}

interface RunDetailBodyProps {
  active: boolean;
  activeTab: TabName;
  ideasViewKey: number;
  data: RunDetailData;
}

function RunDetailBody({
  active,
  activeTab,
  ideasViewKey,
  data,
}: RunDetailBodyProps) {
  const navigate = useNavigate();
  if (isInitialLoading(data))
    return <DocumentSkeleton label="Loading report…" />;
  if (failedWithoutRun(data)) return null;
  if (data.run?.can_resume) {
    return (
      <div className={REPORT_SCROLL_CLASSES}>
        <RunRecovery run={data.run} onResumed={() => navigate(0)} />
      </div>
    );
  }
  const endState = terminalEndStateOf(data.run);
  if (endState) {
    return (
      <div className={REPORT_SCROLL_CLASSES}>
        <RunEndState
          status={endState.status}
          error={endState.error}
          failureKind={endState.failureKind}
        />
      </div>
    );
  }
  if (active && data.run) return <LiveRunSection data={data} />;
  return (
    <RunDetailTabContent
      activeTab={activeTab}
      ideasViewKey={ideasViewKey}
      data={data}
    />
  );
}

function LiveRunSection({data}: {data: RunDetailData}) {
  if (!data.run) return null;
  return (
    <ActiveRunView
      run={data.run}
      events={data.events}
      evidenceCount={Math.max(data.evidence.length, data.run.summary.evidence)}
      ideaCount={Math.max(data.hypotheses.length, data.run.summary.hypotheses)}
    />
  );
}

function OverviewSection({data}: {data: RunDetailData}) {
  return (
    <ResearchOverviewView
      run={data.run}
      report={data.report}
      hypotheses={data.hypotheses}
      matches={data.matches}
    />
  );
}

function IdeasSection({
  ideasViewKey,
  data,
}: {
  ideasViewKey: number;
  data: RunDetailData;
}) {
  return (
    <section className="cosci-all-ideas h-full p-0 phone:h-auto">
      <IdeasTab
        key={ideasViewKey}
        hypotheses={data.hypotheses}
        reviews={data.reviews}
        matches={data.matches}
        claimEvidence={data.claimEvidence}
      />
    </section>
  );
}

const TAB_SECTIONS: Record<
  TabName,
  (ideasViewKey: number, data: RunDetailData) => React.ReactNode
> = {
  details: (_key, data) => (
    <RunSpecificationsView
      run={data.run}
      safety={data.safety}
      onSafetyChanged={data.refreshNow}
    />
  ),
  learning: (_key, data) => (
    <LearningView
      goal={runGoal(data.run)}
      evidence={data.evidence}
      report={data.report}
    />
  ),
  overview: (_key, data) => <OverviewSection data={data} />,
  ideas: (ideasViewKey, data) => (
    <IdeasSection ideasViewKey={ideasViewKey} data={data} />
  ),
};

function tabSection(
  activeTab: TabName,
  ideasViewKey: number,
  data: RunDetailData,
) {
  return TAB_SECTIONS[activeTab](ideasViewKey, data);
}

// Remount on tab changes to reset local state; keep report-wide notices inside
// the fixed scrolling grid track.
function RunDetailTabContent({
  activeTab,
  ideasViewKey,
  data,
}: Omit<RunDetailBodyProps, 'active'>) {
  return (
    <div
      className={joinClasses(REPORT_SCROLL_CLASSES, 'ui-motion-enter')}
      key={activeTab}
      aria-label={reportSectionLabel(activeTab)}
      role="region"
      tabIndex={0}
    >
      <AwaitingDecisionNotice count={awaitingDecisionCount(data)} />
      {reportIsUngrounded(data) && <ReportUngroundedNotice />}
      {tabSection(activeTab, ideasViewKey, data)}
    </div>
  );
}
