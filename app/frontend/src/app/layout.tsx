import {
  type ReactNode,
  useCallback,
  useEffect,
  useRef,
  useState,
  type Dispatch,
  type SetStateAction,
  type RefObject,
  lazy,
  Suspense,
} from 'react';
import {useLocation} from 'react-router-dom';
import type {RunStatus} from '@/shared/api/runs';
import {presenceProps, usePresence} from '@/shared/ui';
import {joinClasses} from '@/shared/ui/classes';
import {useRunHistoryContext} from '@/shared/hooks/history_context';
import type {SettingsSection} from '@/features/access/settings_sections';
import {NEW_CHAT_EVENT, HEADER_TITLE_EVENT} from '@/shared/lib/dom_events';
import {
  closeDrawerIfMobile,
  useBackgroundInert,
  useEscapeKey,
  useFocusTrap,
  useIsMobile,
  useRestoreFocusOnClose,
} from '@/shared/hooks/dom';
import {ShellHeader} from './layout_header';
import {NavRail, withExamples, type ChatRailData} from './layout_nav_rail';
import {
  sessionSwitchData,
  type SessionSwitchData,
} from '@/features/runs/session_switch';
import {useChatHistoryContext} from '@/shared/hooks/history_context';
import {routeIds} from '@/shared/lib/routes';
import {useLaunchStatus} from '@/shared/hooks/launch_status_context';
import {LaunchStatusBanner} from '@/shared/ui/launch_status_banner';

type LayoutChrome = ReturnType<typeof useLayoutChrome>;

const SettingsDialog = lazy(() =>
  import('@/features/access/settings_dialog').then(module => ({
    default: module.SettingsDialog,
  })),
);

const WORKSPACE_CLASSES =
  'ucs-workspace relative z-1 grid min-w-0 grid-rows-[auto_minmax(0,1fr)] overflow-clip rounded-l-workspace bg-cosci-bg ' +
  'phone:rounded-none';

const REPORT_WORKSPACE_CLASSES =
  WORKSPACE_CLASSES + ' min-h-[100vh] supports-[height:100dvh]:min-h-[100dvh]';

// dvh tracks the visible viewport on iOS Safari, where vh includes the
// collapsed-toolbar space; vh stays as the fallback.
const PAGE_CLASSES =
  'ucs-page h-[calc(100vh-4.5rem)] min-w-0 p-0 supports-[height:100dvh]:h-[calc(100dvh-4.5rem)]';

const DEFAULT_PAGE_CLASSES = PAGE_CLASSES + ' overflow-auto';

const HOME_PAGE_CLASSES =
  PAGE_CLASSES +
  ' ucs-page--home overflow-auto desktop:overflow-hidden ' +
  '[@media(max-width:1180px)]:overflow-x-hidden [@media(max-width:1180px)]:overflow-y-auto';

// Narrow report overflow must remain reachable horizontally; vertical scroll
// stays owned by the inner report pane.
const REPORT_PAGE_CLASSES =
  PAGE_CLASSES + ' min-h-0 overflow-x-auto overflow-y-hidden';

const SHELL_CLASSES =
  'ucs-app-shell grid min-h-[100vh] bg-cosci-rail supports-[height:100dvh]:min-h-[100dvh] ' +
  '[transition:grid-template-columns_var(--motion-duration-long)_var(--motion-ease-standard)] motion-reduce:[transition:none] ' +
  'phone:grid-cols-[minmax(0,1fr)]';

const SHELL_OPEN_GRID_CLASSES =
  'nav-open above-phone:grid-cols-[17.25rem_minmax(0,1fr)]';

// Keep the collapsed icon on the open rail's 24px gutter so toggling cannot
// shift it.
const SHELL_COLLAPSED_GRID_CLASSES =
  'nav-collapsed above-phone:grid-cols-[4.5rem_minmax(0,1fr)]';

function pageClassesFor(pathname: string, isRunRoute: boolean): string {
  if (isRunRoute) return REPORT_PAGE_CLASSES;
  return pathname === '/' ? HOME_PAGE_CLASSES : DEFAULT_PAGE_CLASSES;
}

function deriveRoutePresentation(pathname: string): {
  isRunRoute: boolean;
  activeRunId: string | undefined;
  activeChatId: string | undefined;
  titleContextKey: string;
  workspaceClasses: string;
  pageClasses: string;
} {
  const {runId: activeRunId, chatId: activeChatId} = routeIds(pathname);
  const isRunRoute = activeRunId !== undefined;
  // Tab changes keep RunDetail mounted and do not redispatch its title; clear
  // only when the title-owning context changes.
  const titleContextKey = isRunRoute ? `run:${activeRunId}` : pathname;
  const workspaceClasses = isRunRoute
    ? REPORT_WORKSPACE_CLASSES
    : WORKSPACE_CLASSES;

  return {
    isRunRoute,
    activeRunId,
    activeChatId,
    titleContextKey,
    workspaceClasses,
    pageClasses: pageClassesFor(pathname, isRunRoute),
  };
}

function shellClassFor(isRunRoute: boolean, navOpen: boolean): string {
  return joinClasses(
    SHELL_CLASSES,
    isRunRoute ? 'report-shell' : 'home-shell',
    navOpen ? SHELL_OPEN_GRID_CLASSES : SHELL_COLLAPSED_GRID_CLASSES,
  );
}

// Navigation belongs to real links; reset only the current tab and dismiss
// mobile overlays while preserving desktop rail preference.
function createStartNewChatHandler(
  setNavOpen: (open: boolean) => void,
): () => void {
  return () => {
    closeDrawerIfMobile(setNavOpen);
    window.dispatchEvent(new Event(NEW_CHAT_EVENT));
  };
}

function DrawerScrim({
  navOpen,
  onDismiss,
}: {
  navOpen: boolean;
  onDismiss: () => void;
}) {
  const {mounted, state} = usePresence(navOpen);
  if (!mounted) return null;
  return (
    <div
      {...presenceProps(state)}
      data-motion="long"
      className="ui-motion-fade fixed inset-0 z-drawer-scrim hidden bg-scrim phone:block"
      aria-hidden="true"
      onClick={onDismiss}
    />
  );
}

interface ShellOverlaysProps {
  chrome: LayoutChrome;
}

function ShellOverlays({chrome}: ShellOverlaysProps) {
  const {settingsSection, setSettingsSection} = chrome;
  const [requested, setRequested] = useState(Boolean(settingsSection));
  useEffect(() => {
    if (settingsSection) setRequested(true);
  }, [settingsSection]);
  // Keep the loaded dialog mounted so closing still completes its exit motion.
  if (!requested && !settingsSection) return null;
  return (
    <Suspense fallback={null}>
      <SettingsDialog
        section={settingsSection}
        onSectionChange={setSettingsSection}
        onClose={() => setSettingsSection(null)}
      />
    </Suspense>
  );
}

interface ShellNavProps {
  chrome: LayoutChrome;
  startNewChat: () => void;
  rail: ChatRailData;
}

// The phone drawer covers the page, so while it is open it behaves as a modal:
// focus moves in and stays, the page behind is inert, and closing returns focus.
function DrawerModality({
  containerRef,
}: {
  containerRef: RefObject<HTMLDivElement | null>;
}) {
  useRestoreFocusOnClose();
  useFocusTrap(containerRef);
  useBackgroundInert(containerRef, 'Navigation drawer');
  useEffect(() => {
    containerRef.current
      ?.querySelector<HTMLElement>('a[href], button:not([disabled])')
      ?.focus();
  }, [containerRef]);
  return null;
}

// `contents` keeps the rail and scrim as shell grid items while giving the
// modal one container, so the scrim stays clickable when the page is inert.
function ShellNav({chrome, startNewChat, rail}: ShellNavProps) {
  const drawerRef = useRef<HTMLDivElement>(null);
  const isMobile = useIsMobile();
  return (
    <div ref={drawerRef} className="contents">
      {isMobile && chrome.navOpen && (
        <DrawerModality containerRef={drawerRef} />
      )}
      <NavRail
        navOpen={chrome.navOpen}
        toggleNav={chrome.toggleNav}
        startNewChat={startNewChat}
        rail={rail}
        activePanel={chrome.activePanel}
        onTogglePanel={chrome.togglePanel}
        onOpenSettings={chrome.openSettings}
        settingsControlRef={chrome.settingsControlRef}
      />
      <DrawerScrim
        navOpen={chrome.navOpen}
        onDismiss={() => chrome.setNavOpen(false)}
      />
    </div>
  );
}

interface ShellWorkspaceProps {
  chrome: LayoutChrome;
  startNewChat: () => void;
  headerTitle: string;
  conversationHeading?: string;
  session: SessionSwitchData | null;
  runStatus: RunStatus | undefined;
  workspaceClasses: string;
  pageClasses: string;
  children: ReactNode;
}

function ShellWorkspace({
  chrome,
  startNewChat,
  headerTitle,
  conversationHeading,
  session,
  runStatus,
  workspaceClasses,
  pageClasses,
  children,
}: ShellWorkspaceProps) {
  const {status} = useLaunchStatus();
  const notice = Boolean(status?.message);
  return (
    <section
      className={workspaceClasses}
      style={notice ? {height: '100dvh'} : undefined}
    >
      <div role="banner" aria-label="Workspace header" className="min-w-0">
        <ShellHeader
          navOpen={chrome.navOpen}
          toggleNav={chrome.toggleNav}
          startNewChat={startNewChat}
          headerTitle={headerTitle}
          session={session}
          runStatus={runStatus}
          headerActionsRef={chrome.headerActionsRef}
        />
        <LaunchStatusBanner />
      </div>
      <main
        className={pageClasses}
        style={notice ? {height: 'auto', minHeight: 0} : undefined}
      >
        {conversationHeading && (
          <h1 className="sr-only">{conversationHeading}</h1>
        )}
        {children}
      </main>
    </section>
  );
}

function useLayoutState() {
  const location = useLocation();
  const {
    isRunRoute,
    activeRunId,
    activeChatId,
    titleContextKey,
    workspaceClasses,
    pageClasses,
  } = deriveRoutePresentation(location.pathname);
  const headerTitle = useHeaderTitle(titleContextKey);
  const {chats, showAllChats, toggleShowAllChats} = useChatHistory();
  const chrome = useLayoutChrome(location.pathname);
  const {history} = useRunHistoryContext();
  const session = sessionSwitchData(chats, activeChatId, activeRunId);
  // Shared history already polls active runs; avoid a second status request for
  // header controls.
  const runStatus = history.find(run => run.id === session?.runId)?.status;
  const shellClass = shellClassFor(isRunRoute, chrome.navOpen);
  const startNewChat = createStartNewChatHandler(chrome.setNavOpen);

  return {
    shellClass,
    chrome,
    startNewChat,
    rail: {
      chats: withExamples(chats, history),
      activeChatId,
      activeRunId,
      showAllChats,
      onToggleShowAllChats: toggleShowAllChats,
    },
    headerTitle,
    conversationHeading: activeChatId
      ? headerTitle || 'Research conversation'
      : undefined,
    session,
    runStatus,
    workspaceClasses,
    pageClasses,
  };
}

export function Layout({children}: {children: ReactNode}) {
  const state = useLayoutState();

  return (
    <div className={state.shellClass}>
      <ShellNav
        chrome={state.chrome}
        startNewChat={state.startNewChat}
        rail={state.rail}
      />
      <ShellWorkspace
        chrome={state.chrome}
        startNewChat={state.startNewChat}
        headerTitle={state.headerTitle}
        conversationHeading={state.conversationHeading}
        session={state.session}
        runStatus={state.runStatus}
        workspaceClasses={state.workspaceClasses}
        pageClasses={state.pageClasses}
      >
        {children}
      </ShellWorkspace>
      <ShellOverlays chrome={state.chrome} />
    </div>
  );
}

export type ShellPanel = 'settings';

// Chats enter history from their first turn, whether or not they become runs.
function useChatHistory() {
  const {chats} = useChatHistoryContext();
  const [showAllChats, setShowAllChats] = useState(false);

  return {
    chats,
    showAllChats,
    toggleShowAllChats: () => setShowAllChats(current => !current),
  };
}

function useHeaderTitle(contextKey: string): string {
  const [overrideTitle, setOverrideTitle] = useState('');

  useEffect(() => {
    setOverrideTitle('');
  }, [contextKey]);

  useEffect(() => {
    function onHeaderTitle(event: Event) {
      const custom = event as CustomEvent<string>;
      setOverrideTitle(custom.detail || '');
    }
    window.addEventListener(HEADER_TITLE_EVENT, onHeaderTitle);
    return () => {
      window.removeEventListener(HEADER_TITLE_EVENT, onHeaderTitle);
    };
  }, []);

  return overrideTitle;
}

// Navigating must dismiss overlays; desktop rail expansion remains a user
// preference.
function useDismissChromeOnNavigate(
  pathname: string,
  setActivePanel: (panel: ShellPanel | null) => void,
  setNavOpen: (open: boolean) => void,
) {
  useEffect(() => {
    setActivePanel(null);
    closeDrawerIfMobile(setNavOpen);
  }, [pathname]);
}

function useEscapeClosesDrawer(
  navOpen: boolean,
  setNavOpen: (open: boolean) => void,
) {
  const closeDrawer = useCallback(
    () => closeDrawerIfMobile(setNavOpen),
    [setNavOpen],
  );
  useEscapeKey(closeDrawer, navOpen);
}

function useChromeActions(
  setNavOpen: Dispatch<SetStateAction<boolean>>,
  setActivePanel: Dispatch<SetStateAction<ShellPanel | null>>,
  setSettingsSection: Dispatch<SetStateAction<SettingsSection | null>>,
) {
  function toggleNav() {
    setNavOpen(open => !open);
    setActivePanel(null);
  }

  function togglePanel(panel: ShellPanel) {
    setActivePanel(current => (current === panel ? null : panel));
  }

  function openSettings(section: SettingsSection) {
    setActivePanel(null);
    // Close the underlying mobile drawer so dismissing Settings cannot reveal a
    // stale overlay.
    closeDrawerIfMobile(setNavOpen);
    setSettingsSection(section);
  }

  return {toggleNav, togglePanel, openSettings};
}

function useChromeState() {
  const [navOpen, setNavOpen] = useState(false);
  const [activePanel, setActivePanel] = useState<ShellPanel | null>(null);
  const [settingsSection, setSettingsSection] =
    useState<SettingsSection | null>(null);
  const settingsControlRef = useRef<HTMLDivElement>(null);
  const headerActionsRef = useRef<HTMLDivElement>(null);

  return {
    navOpen,
    setNavOpen,
    activePanel,
    setActivePanel,
    settingsSection,
    setSettingsSection,
    settingsControlRef,
    headerActionsRef,
  };
}

function useLayoutChrome(pathname: string) {
  const state = useChromeState();
  const {navOpen, activePanel, setActivePanel, setNavOpen} = state;
  const {settingsControlRef, headerActionsRef} = state;

  const {toggleNav, togglePanel, openSettings} = useChromeActions(
    setNavOpen,
    setActivePanel,
    state.setSettingsSection,
  );

  useDismissChromeOnNavigate(pathname, setActivePanel, setNavOpen);
  useEscapeClosesDrawer(navOpen, setNavOpen);

  return {
    navOpen,
    setNavOpen,
    activePanel,
    settingsSection: state.settingsSection,
    setSettingsSection: state.setSettingsSection,
    settingsControlRef,
    headerActionsRef,
    toggleNav,
    togglePanel,
    openSettings,
  };
}
