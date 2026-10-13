import type {RefObject} from 'react';
import {Link} from 'react-router-dom';
import type {RunStatus} from '@/shared/api/runs';
import type {IconName} from '@/shared/ui/icon';
import {isModifiedClick} from '@/shared/lib/dom_events';
import {Chip, IconButton} from '@/shared/ui';
import {GoogleLabsIcon} from '@/shared/ui/layout_primitives';
import {TruncatedLabel} from '@/shared/ui/truncated_label';
import {CancelRunControl} from '@/features/runs/cancel_run';
import {SessionDiagnostics} from '@/features/diagnostics/diagnostics';
import {FeedbackControl} from '@/features/diagnostics/feedback_dialog';
import {
  SessionSwitch,
  type SessionSwitchData,
} from '@/features/runs/session_switch';
import {joinClasses, tooltipClassNames} from '@/shared/ui/classes';
import type {SystemStatus} from '@/shared/api/system';
import {useSystemStatus} from '@/shared/hooks/system_status_context';

const HEADER_CLASSES =
  'ucs-header-action-bar sticky top-0 z-header flex min-h-[4rem] items-center justify-between gap-4 [border-bottom:0] bg-cosci-bg px-7 ' +
  'phone:min-w-0 phone:gap-1.5 phone:px-2';

const PRODUCT_LOCKUP_CLASSES =
  'inline-flex cursor-pointer items-center gap-2 [border:0] bg-transparent p-0 font-gsans text-[1.25rem] font-medium tracking-[-0.6px] text-cosci-fg no-underline';

const HEADER_TITLE_CLASSES =
  'ucs-header-title absolute top-1/2 left-1/2 min-w-0 max-w-[min(52rem,44vw)] overflow-hidden text-center text-[1rem] font-medium text-cosci-fg [transform:translate(-50%,-50%)] phone:hidden';

// Keep the session switch on phones: it is the route back to the transcript
// after leaving a run.
const HEADER_ACTIONS_CLASSES =
  'ucs-header-actions absolute top-1/2 right-[1.35rem] flex min-w-max items-center gap-2 [transform:translateY(-50%)] ' +
  'phone:static phone:flex-none phone:[transform:none] phone:[&>:not(.ucs-session-switch):not(.ucs-feedback-control)]:hidden';

function HamburgerButton({
  navOpen,
  onClick,
}: {
  navOpen: boolean;
  onClick: () => void;
}) {
  return (
    // Phones only; the wrapper owns display so the button keeps its own.
    <span className="hidden flex-none phone:inline-flex">
      <IconButton
        icon="menu"
        size="md"
        label="Open navigation"
        tooltip={null}
        aria-expanded={navOpen}
        aria-controls="primary-navigation"
        onClick={onClick}
      />
    </span>
  );
}

// Only ordinary link navigation resets this tab’s chat; modified clicks must
// preserve it.
function ProductLockup({
  onNewChat,
  hasSession,
}: {
  onNewChat: () => void;
  hasSession: boolean;
}) {
  return (
    <Link
      to="/"
      state={{cosciAction: 'new-chat'}}
      className={tooltipClassNames({
        className: joinClasses(
          PRODUCT_LOCKUP_CLASSES,
          hasSession ? 'phone:mr-auto' : 'phone:mr-2',
        ),
        placement: 'right',
      })}
      aria-label="Go to Open Co-Scientist home"
      data-tooltip="Home"
      onClick={event => {
        if (!isModifiedClick(event)) onNewChat();
      }}
    >
      <GoogleLabsIcon
        aria-hidden="true"
        className="size-[1.32rem] flex-[0_0_1.32rem] text-cosci-accent phone:hidden"
      />
      <span>Open Co-Scientist</span>
    </Link>
  );
}

export function ShellHeader({
  navOpen,
  toggleNav,
  startNewChat,
  headerTitle,
  headerActionsRef,
  session,
  runStatus,
}: {
  navOpen: boolean;
  toggleNav: () => void;
  startNewChat: () => void;
  headerTitle: string;
  headerActionsRef: RefObject<HTMLDivElement | null>;
  session: SessionSwitchData | null;
  runStatus: RunStatus | undefined;
}) {
  return (
    <header className={HEADER_CLASSES}>
      <HamburgerButton navOpen={navOpen} onClick={toggleNav} />
      <ProductLockup onNewChat={startNewChat} hasSession={session !== null} />
      <div
        id="header-landing-tabs"
        className="pointer-events-none absolute inset-0 overflow-hidden"
      />
      {/* Session controls replace the title; the run/transcript already presents it. */}
      <div className={joinClasses(HEADER_TITLE_CLASSES, session && 'hidden')}>
        {headerTitle && (
          <TruncatedLabel
            className="block min-w-0 overflow-hidden whitespace-nowrap"
            text={headerTitle}
          />
        )}
      </div>
      {/* Navigation dismisses popovers itself, so the session switch may share this anchor. */}
      <div ref={headerActionsRef} className={HEADER_ACTIONS_CLASSES}>
        <CancelRunControl runId={session?.runId} status={runStatus} />
        <SessionSwitch session={session} />
        <SystemStatusIndicator />
        <FeedbackControl runId={session?.runId} />
        <SessionDiagnostics />
      </div>
    </header>
  );
}

interface SystemStatusChip {
  label: string;
  detail: string;
  icon: IconName;
  danger: boolean;
}

function buildSystemStatusChip(
  status: SystemStatus | null,
  unreachable: boolean,
): SystemStatusChip | null {
  if (unreachable) {
    return {
      label: 'API offline',
      detail: 'The backend API is not reachable.',
      icon: 'warning',
      danger: true,
    };
  }
  if (status?.llm_backend === 'offline') {
    return {
      label: 'Offline mode',
      detail:
        'Runs use the deterministic offline LLM backend — the full agent ' +
        'pipeline runs, but no live model calls are made. Worker model ' +
        `when enabled: ${status.model_name}.`,
      icon: 'computer',
      danger: false,
    };
  }
  return null;
}

export function SystemStatusIndicator() {
  const {status, unreachable} = useSystemStatus();
  const chip = buildSystemStatusChip(status, unreachable);
  if (!chip) return null;

  return (
    <Chip
      role="status"
      size="lg"
      tone={chip.danger ? 'danger' : 'accent'}
      icon={chip.icon}
      tooltip={chip.detail}
      tooltipPlacement="bottom"
      layoutClassName="ucs-system-status"
    >
      {/* Tablet headers also hold the session switch; the icon keeps the
          status in view and the tooltip names it. */}
      <span className="tablet:sr-only">{chip.label}</span>
    </Chip>
  );
}
