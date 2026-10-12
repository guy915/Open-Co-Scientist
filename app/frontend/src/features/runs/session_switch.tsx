import type {ChatSummary} from '@/shared/api/runs';
import {Icon, type IconName} from '@/shared/ui/icon';
import {joinClasses, tooltipClassNames} from '@/shared/ui/classes';
import {TabNav, TabNavLink} from '@/shared/ui';
import {HEADER_CONTROL_ICON_CLASSES} from '@/shared/ui/layout_primitives';
import {resultsPath, useRecordRunTab} from '@/shared/hooks/session_side';
import {chatPath} from '@/shared/lib/routes';

export interface SessionSwitchData {
  chatId: string;
  runId: string;
  active: 'chat' | 'results';
}

function sessionChat(
  chats: readonly ChatSummary[],
  activeChatId: string | undefined,
  activeRunId: string | undefined,
): ChatSummary | undefined {
  if (activeChatId) return chats.find(entry => entry.id === activeChatId);
  if (activeRunId) return chats.find(entry => entry.run_id === activeRunId);
  return undefined;
}

export function sessionSwitchData(
  chats: readonly ChatSummary[],
  activeChatId: string | undefined,
  activeRunId: string | undefined,
): SessionSwitchData | null {
  const chat = sessionChat(chats, activeChatId, activeRunId);
  if (!chat?.run_id) return null;
  return {
    chatId: chat.id,
    runId: chat.run_id,
    active: activeChatId ? 'chat' : 'results',
  };
}

const SWITCH_SIDES: {
  side: 'chat' | 'results';
  icon: IconName;
  label: string;
  tooltip: string;
}[] = [
  {
    side: 'chat',
    icon: 'forum',
    label: 'Chat',
    tooltip: 'Ask questions about this session',
  },
  {
    side: 'results',
    icon: 'lab_profile',
    label: 'Results',
    tooltip: 'View research progress and results',
  },
];

function sessionSideHref(session: SessionSwitchData, side: string): string {
  return side === 'chat'
    ? chatPath(session.chatId)
    : resultsPath(session.runId);
}

// Real links preserve deep/new-tab navigation; explicit names survive hidden
// phone labels, and aria-current marks navigation rather than a pressed button.
export function SessionSwitch({session}: {session: SessionSwitchData | null}) {
  useRecordRunTab();
  if (!session) return null;
  return (
    // One highlight travels between the sides instead of each side's fill
    // vanishing and reappearing.
    <TabNav
      label="Session view"
      variant="pill"
      current={session.active}
      layoutClassName="ucs-session-switch h-[2.35rem] min-w-max text-[0.88rem] font-semibold"
    >
      {SWITCH_SIDES.map(({side, icon, label, tooltip}) => (
        <TabNavLink
          key={side}
          variant="pill"
          to={sessionSideHref(session, side)}
          current={side === session.active}
          className={tooltipClassNames({placement: 'bottom'})}
          data-tooltip={tooltip}
          aria-label={label}
        >
          <Icon
            className={joinClasses(HEADER_CONTROL_ICON_CLASSES, 'flex-none')}
            name={icon}
          />
          <span className="phone:hidden">{label}</span>
        </TabNavLink>
      ))}
    </TabNav>
  );
}
