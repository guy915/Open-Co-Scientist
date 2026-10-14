import {
  memo,
  useCallback,
  useEffect,
  useLayoutEffect,
  useMemo,
  useState,
  Fragment,
  type RefObject,
  useRef,
} from 'react';
import {
  useLocation,
  useNavigate,
  useParams,
  type NavigateFunction,
} from 'react-router-dom';
import {Button, IconButton, Toast, ErrorNotice} from '@/shared/ui';
import {conciseTitle} from '@/shared/lib/text';
import {HEADER_TITLE_EVENT, NEW_CHAT_EVENT} from '@/shared/lib/dom_events';
import {useIsMobile} from '@/shared/hooks/dom';
import {useToast, type ToastState} from '@/shared/hooks/timers';
import {useRunHistoryContext} from '@/shared/hooks/history_context';
import {
  useChatSession,
  type SpecStage,
  type LinkedDraftRecovery,
} from './use_chat_session';
import {useChatRehydration} from './use_chat_rehydrate';
import {type ConnectorToggleProps, Composer} from './chat_composer';
import {HomeStage} from './chat_home_stage';
import type {StartedSession} from './chat_timeline_run_spec_card';
import {pendingQuestions} from './chat_questions';
import {QuestionChooser} from './chat_questions';
import {
  buildTimelineItems,
  type TimelineItem,
  DRAFT_SPEC_ITEM_ID,
} from './chat_workspace_timeline';
import {TIMELINE_ANCHOR_ATTRIBUTE} from './chat_timeline_bubble';
import {scrollBehavior} from '@/shared/lib/reduced_motion';
import {chatPath} from '@/shared/lib/routes';
import {DeferredHomeLanding} from './deferred_home_landing';

interface ChatWorkspaceLocationState {
  cosciAction?: 'new-chat' | 'focus-composer';
}

export function ChatWorkspace() {
  const navigate = useNavigate();
  const {id: chatId} = useParams<{id?: string}>();
  const [showAllRecents, setShowAllRecents] = useState(false);
  const [pubmedEnabled, setPubmedEnabled] = useState(true);
  const [webSearchEnabled, setWebSearchEnabled] = useState(true);
  const connectors: ConnectorToggleProps = {
    pubmedEnabled,
    onPubmedEnabledChange: setPubmedEnabled,
    webSearchEnabled,
    onWebSearchEnabledChange: setWebSearchEnabled,
  };
  const {toast, setToast} = useToast();
  const {history, reload: reloadHistory} = useRunHistoryContext();
  const isMobile = useIsMobile();
  const onChatStarted = useCallback(
    (id: string) => void navigate(chatPath(id), {replace: true}),
    [navigate],
  );
  const session = useChatSession({
    reloadHistory,
    focusComposer: focusComposerTextarea,
    onChatStarted,
    setToast,
    pubmedEnabled,
    webSearchEnabled,
  });
  const linkedDraftRecovery = useChatRehydration(session, chatId);
  const resetWorkspace = useCallback(() => {
    session.resetSession();
    setToast(null);
    void reloadHistory();
  }, [reloadHistory, session.resetSession, setToast]);
  useChatWorkspaceGlobalEvents({
    resetWorkspace,
    focusComposer: focusComposerTextarea,
  });
  useEffect(
    () => syncHeaderTitle(session.draft, session.startedSession),
    [session.draft, session.startedSession],
  );
  const {timelineItems, scrollRef, composerRef} = useConversationLayout(
    session,
    navigate,
    resetWorkspace,
    focusComposerTextarea,
    linkedDraftRecovery,
  );

  // A reopened chat shows nothing until its transcript lands, not the home
  // page underneath it.
  const awaitingTranscript =
    Boolean(chatId) &&
    !session.hasConversation &&
    linkedDraftRecovery.unavailableChatId !== chatId;

  return (
    <div className="reference-workspace grid h-full min-h-full grid-cols-[minmax(0,1fr)] gap-4 phone:flex phone:min-h-0 phone:flex-1 phone:flex-col">
      <div className="reference-workspace-main relative flex h-full min-h-0 min-w-0 flex-col phone:flex-1">
        {session.hasConversation || awaitingTranscript ? (
          <ConversationView
            scrollRef={scrollRef}
            timelineItems={timelineItems}
            composerRef={composerRef}
            session={session}
            connectors={connectors}
          />
        ) : (
          <>
            <HomeStage
              input={session.input}
              setInput={session.setInput}
              connectors={connectors}
              onSubmit={session.handleSubmit}
              runs={history}
              showAllRecents={showAllRecents}
              onToggleShowAll={() => setShowAllRecents(current => !current)}
            />
            {/* Phones get the composer only; the landing is a desktop surface. */}
            {!isMobile && <DeferredHomeLanding />}
          </>
        )}
        <ToastPortal toast={toast} />
      </div>
    </div>
  );
}

function focusComposerTextarea() {
  window.requestAnimationFrame(() => {
    const composer = document.querySelector<HTMLTextAreaElement>(
      '.reference-composer textarea',
    );
    composer?.focus();
  });
}

function syncHeaderTitle(
  draft: SpecStage | null,
  startedSession: StartedSession | null,
) {
  const title = draft
    ? conciseTitle(draft.spec.goal)
    : startedSession
      ? startedSession.title
      : '';
  window.dispatchEvent(new CustomEvent(HEADER_TITLE_EVENT, {detail: title}));
  return () => {
    window.dispatchEvent(new CustomEvent(HEADER_TITLE_EVENT, {detail: ''}));
  };
}

function ToastPortal({toast}: {toast: ToastState | null}) {
  return (
    <Toast>
      {toast && (
        <>
          <span>{toast.message}</span>
          {toast.action && (
            <Button variant="link" onClick={toast.action.onClick}>
              {toast.action.label}
            </Button>
          )}
        </>
      )}
    </Toast>
  );
}

// Clear handled navigation state so back/forward and rerenders cannot replay
// new-chat actions.
function useChatWorkspaceGlobalEvents({
  resetWorkspace,
  focusComposer,
}: {
  resetWorkspace: () => void;
  focusComposer: () => void;
}) {
  const location = useLocation();
  const navigate = useNavigate();

  useEffect(() => {
    window.addEventListener(NEW_CHAT_EVENT, resetWorkspace);
    return () => {
      window.removeEventListener(NEW_CHAT_EVENT, resetWorkspace);
    };
  }, [resetWorkspace]);

  useEffect(() => {
    const state = location.state as ChatWorkspaceLocationState | null;
    if (!state?.cosciAction) return;
    if (state.cosciAction === 'new-chat') resetWorkspace();
    if (state.cosciAction === 'focus-composer') focusComposer();
    void navigate(location.pathname, {replace: true, state: null});
  }, [
    focusComposer,
    location.pathname,
    location.state,
    navigate,
    resetWorkspace,
  ]);
}

const CHAT_COLUMN_CLASSES =
  'reference-chat-column ui-motion-enter-items mx-auto grid w-[min(100%,50.75rem)] gap-5';

function syncComposerHeight(
  composerRef: RefObject<HTMLDivElement | null>,
  scrollRef: RefObject<HTMLDivElement | null>,
  hasConversation: boolean,
  refs: TimelineScrollRefs,
) {
  const composer = composerRef.current;
  const scroller = scrollRef.current;
  if (!hasConversation || !composer || !scroller) return;
  let previousHeight: number | null = null;
  let frame = 0;
  const sync = () => {
    const height = composer.offsetHeight;
    const shouldFollow =
      (previousHeight === null || height !== previousHeight) &&
      isFollowingLayoutChange(refs, scroller);
    scroller.style.setProperty('--chat-composer-h', `${height}px`);
    if (shouldFollow) scrollToBottom(refs, scroller);
    previousHeight = height;
  };
  sync();
  const observer = new ResizeObserver(() => {
    cancelAnimationFrame(frame);
    frame = requestAnimationFrame(() => {
      frame = 0;
      sync();
    });
  });
  observer.observe(composer);
  return () => {
    observer.disconnect();
    cancelAnimationFrame(frame);
  };
}

function useConversationLayout(
  session: ReturnType<typeof useChatSession>,
  navigate: NavigateFunction,
  resetWorkspace: () => void,
  focusComposer: () => void,
  linkedDraftRecovery: LinkedDraftRecovery,
) {
  const composerRef = useRef<HTMLDivElement>(null);

  const {
    messages,
    handleEditMessage,
    handleCopyRequest,
    handleRetryMessage,
    draft,
    setDraft,
    isStarting,
    isAwaitingAgent,
    agentReasoning,
    agentDraft,
    handleCancelDraftSpec,
    handleRetryDraftSpec,
    handleStartRun,
    confirmed,
    stageDraftSpec,
    startedSession,
  } = session;
  const {canContinueLinkedDraft, spec, status, retryStatusLookup} =
    linkedDraftRecovery;
  // Keyed on everything but the composer text, so a keystroke does not rebuild
  // and re-render every bubble of a long transcript.
  const timelineItems = useMemo(
    () =>
      buildTimelineItems({
        messages,
        handleEditMessage,
        handleCopyRequest,
        handleRetryMessage,
        draft,
        setDraft,
        isStarting,
        isAwaitingAgent,
        agentReasoning,
        agentDraft,
        handleCancelDraftSpec,
        handleRetryDraftSpec,
        handleStartRun,
        confirmed,
        stageDraftSpec,
        startedSession,
        navigate,
        resetWorkspace,
        focusComposer,
        linkedDraftRecovery: {
          canContinueLinkedDraft,
          spec,
          status,
          retryStatusLookup,
        },
      }),
    [
      messages,
      handleEditMessage,
      handleCopyRequest,
      handleRetryMessage,
      draft,
      setDraft,
      isStarting,
      isAwaitingAgent,
      agentReasoning,
      agentDraft,
      handleCancelDraftSpec,
      handleRetryDraftSpec,
      handleStartRun,
      confirmed,
      stageDraftSpec,
      startedSession,
      navigate,
      resetWorkspace,
      focusComposer,
      canContinueLinkedDraft,
      spec,
      status,
      retryStatusLookup,
    ],
  );

  const scrollRef = useChatTimelineScroll(
    timelineItems,
    session.startedSession,
    session.isAwaitingAgent,
    session.interview?.id,
    session.error,
    composerRef,
    session.hasConversation,
  );

  return {timelineItems, scrollRef, composerRef};
}

interface ConversationViewProps {
  scrollRef: RefObject<HTMLDivElement | null>;
  timelineItems: TimelineItem[];
  composerRef: RefObject<HTMLDivElement | null>;
  session: Pick<
    ReturnType<typeof useChatSession>,
    | 'input'
    | 'setInput'
    | 'error'
    | 'isStarting'
    | 'isAwaitingAgent'
    | 'startedSession'
    | 'handleSubmit'
    | 'handleStop'
    | 'interview'
    | 'handleAnswerQuestions'
  >;
  connectors: ConnectorToggleProps;
}

function ConversationView(props: ConversationViewProps) {
  return (
    <>
      <MemoizedTimelineSection
        scrollRef={props.scrollRef}
        timelineItems={props.timelineItems}
        error={props.session.error}
      />
      <ComposerSection
        composerRef={props.composerRef}
        scrollRef={props.scrollRef}
        session={props.session}
        connectors={props.connectors}
      />
    </>
  );
}

// Symmetric scrollbar gutters align composer and timeline centers; the measured
// composer height keeps the final message reachable.
function TimelineSection({
  scrollRef,
  timelineItems,
  error,
}: {
  scrollRef: RefObject<HTMLDivElement | null>;
  timelineItems: TimelineItem[];
  error: string | null;
}) {
  return (
    <section
      ref={scrollRef}
      className="reference-chat-timeline flex-1 [scrollbar-gutter:stable_both-edges] [overflow-anchor:none] overflow-x-hidden overflow-y-auto px-4 pt-5 pb-[var(--chat-composer-h,12rem)]"
    >
      <div className={`${CHAT_COLUMN_CLASSES} content-end`}>
        {timelineItems.map(item => (
          <Fragment key={item.id}>{item.node}</Fragment>
        ))}
        {error && <ErrorNotice>{error}</ErrorNotice>}
      </div>
    </section>
  );
}

const MemoizedTimelineSection = memo(TimelineSection);

interface ComposerSectionProps {
  composerRef: RefObject<HTMLDivElement | null>;
  scrollRef: RefObject<HTMLDivElement | null>;
  session: Pick<
    ReturnType<typeof useChatSession>,
    | 'input'
    | 'setInput'
    | 'isStarting'
    | 'isAwaitingAgent'
    | 'startedSession'
    | 'handleSubmit'
    | 'handleStop'
    | 'interview'
    | 'handleAnswerQuestions'
  >;
  connectors: ConnectorToggleProps;
}

// Only the composer catches input; its transparent fade stays click-through
// while messages scroll beneath it. The fade owns spacing, so the composer's
// own top margin would introduce a gap.
function ComposerSection(props: ComposerSectionProps) {
  const {
    input,
    setInput,
    isStarting,
    isAwaitingAgent,
    handleSubmit,
    handleStop,
  } = props.session;
  return (
    <div
      ref={props.composerRef}
      className="pointer-events-none absolute inset-x-0 bottom-0 bg-[linear-gradient(to_top,var(--cosci-bg)_62%,transparent)] px-4 pt-11 pb-8 phone:pb-[max(0.75rem,env(safe-area-inset-bottom))] [&_.reference-composer]:mt-0 [&>*]:pointer-events-auto"
    >
      {/* The jump button hangs off the composer column, a spacing step above
          its top edge. */}
      <div className={`${CHAT_COLUMN_CLASSES} relative`}>
        <JumpToBottomButton scrollRef={props.scrollRef} />
        <Composer
          input={input}
          setInput={setInput}
          inConversation
          busy={isStarting}
          autoFocus
          connectors={props.connectors}
          onSubmit={handleSubmit}
          stoppable={isAwaitingAgent}
          onStop={handleStop}
          aboveInput={composerQuestions(props)}
        />
      </div>
    </div>
  );
}

function useScrolledAwayFromBottom(
  scrollRef: RefObject<HTMLDivElement | null>,
): boolean {
  const [away, setAway] = useState(false);
  useEffect(() => {
    const scroller = scrollRef.current;
    if (!scroller) return;
    const update = () => {
      const column = scroller.firstElementChild;
      const actions = column?.lastElementChild?.querySelector<HTMLElement>(
        '.reference-message-actions',
      );
      const composer = scroller.nextElementSibling?.querySelector<HTMLElement>(
        'form.reference-composer',
      );
      if (actions && composer) {
        const pastLatestRest =
          actions.getBoundingClientRect().top -
            composer.getBoundingClientRect().top -
            ACTION_ROW_REST_INSET_PX >
          32;
        setAway(pastLatestRest);
        return;
      }
      setAway(!isFollowingBottom(scroller));
    };
    update();
    scroller.addEventListener('scroll', update, {passive: true});
    return () => scroller.removeEventListener('scroll', update);
  }, [scrollRef]);
  return away;
}

function JumpToBottomButton({
  scrollRef,
}: {
  scrollRef: RefObject<HTMLDivElement | null>;
}) {
  const away = useScrolledAwayFromBottom(scrollRef);
  if (!away) return null;
  // The wrapper owns placement: tooltip anchors are position: relative, which
  // would turn the button itself back into a grid row above the composer.
  // Flex centring leaves `translate` free for the column's enter motion.
  return (
    <span className="pointer-events-none absolute inset-x-0 bottom-full mb-3 flex justify-center">
      <IconButton
        variant="elevated"
        size="md"
        icon="arrow_downward"
        label="Jump to latest message"
        layoutClassName="reference-jump-to-bottom pointer-events-auto"
        onClick={() => {
          const scroller = scrollRef.current;
          scroller?.scrollTo({
            top: scroller.scrollHeight,
            behavior: scrollBehavior(),
          });
        }}
      />
    </span>
  );
}

// Hide sent questions while their replacement arrives; key the chooser by turn
// to reset local selections.
function composerQuestions(props: ComposerSectionProps) {
  const pending = pendingQuestions(props.session.interview);
  if (!pending || props.session.isAwaitingAgent) return null;
  return (
    <QuestionChooser
      key={pending.turnId}
      questions={pending.questions}
      onAnswer={props.session.handleAnswerQuestions}
    />
  );
}

// Anchor new draft plans at their top; confirmed plans replace the same turn
// and must not jump again.
function timelineScrollTarget(
  timelineItems: TimelineItem[],
  startedSession: StartedSession | null,
): {signature: string; anchorMode: 'plan' | 'bottom'} {
  const signature = timelineItems
    .map(item => `${item.id}:${item.at}:${item.revision ?? ''}`)
    .join('|');
  const latestTimelineItemId =
    timelineItems.length > 0 ? timelineItems[timelineItems.length - 1].id : '';
  const anchorMode: 'plan' | 'bottom' =
    !startedSession && latestTimelineItemId === DRAFT_SPEC_ITEM_ID
      ? 'plan'
      : 'bottom';
  return {signature, anchorMode};
}

// Streaming must preserve a reader who deliberately scrolled up.
const FOLLOW_THRESHOLD_PX = 64;

interface TimelineScrollRefs {
  scroller: RefObject<HTMLDivElement | null>;
  previousSignature: RefObject<string>;
  needsInitialScroll: RefObject<boolean>;
  lastAppliedTop: RefObject<number>;
}

const NO_APPLIED_TOP = -1;
// The 8px entrance shift settles before the next render; leave a small buffer.
const ACTION_ROW_REST_INSET_PX = 12;

// Measure at scroll time: asynchronous scroll events can leave a stale bottom
// flag and undo a reader's gesture.
function isFollowingBottom(scroller: HTMLDivElement): boolean {
  const gap =
    scroller.scrollHeight - scroller.scrollTop - scroller.clientHeight;
  return gap <= FOLLOW_THRESHOLD_PX;
}

// Content growth is not a gesture; unchanged scrollTop since our own scroll
// keeps the timeline pinned.
function isPinnedToBottom(
  refs: TimelineScrollRefs,
  scroller: HTMLDivElement,
): boolean {
  if (scroller.scrollTop === refs.lastAppliedTop.current) return true;
  return isFollowingBottom(scroller);
}

function isFollowingLayoutChange(
  refs: TimelineScrollRefs,
  scroller: HTMLDivElement,
): boolean {
  const lastAppliedTop = refs.lastAppliedTop.current;
  if (lastAppliedTop < 0) return false;
  if (scroller.scrollTop === lastAppliedTop) return true;
  const maxScrollTop = scroller.scrollHeight - scroller.clientHeight;
  return (
    lastAppliedTop > maxScrollTop &&
    Math.abs(scroller.scrollTop - maxScrollTop) <= 1
  );
}

// Read scrollTop back because the browser clamps the assigned value.
function scrollToBottom(
  refs: TimelineScrollRefs,
  scroller: HTMLDivElement,
): void {
  const column = scroller.firstElementChild as HTMLElement | null;
  const latestItem = column?.lastElementChild;
  const actions = latestItem?.querySelector<HTMLElement>(
    '.reference-message-actions',
  );
  const composer = scroller.nextElementSibling?.querySelector<HTMLElement>(
    'form.reference-composer',
  );
  if (actions && composer && column) {
    // Short chats can reach scrollTop 0 before their actions clear the composer;
    // align the grid tracks to measured content space when no scroll is needed.
    const actionRect = actions.getBoundingClientRect();
    const composerRect = composer.getBoundingClientRect();
    const scrollerRect = scroller.getBoundingClientRect();
    const topPadding = Number.parseFloat(getComputedStyle(scroller).paddingTop);
    const restingHeight = Math.max(
      0,
      composerRect.top +
        ACTION_ROW_REST_INSET_PX +
        actionRect.height -
        scrollerRect.top -
        topPadding,
    );
    const minHeight = `${Math.ceil(restingHeight)}px`;
    if (column.style.minHeight !== minHeight) {
      column.style.minHeight = minHeight;
    }
    const actionTop = actions.getBoundingClientRect().top;
    const composerTop = composer.getBoundingClientRect().top;
    scroller.scrollTop = Math.max(
      0,
      scroller.scrollTop + actionTop - composerTop - ACTION_ROW_REST_INSET_PX,
    );
  } else {
    if (column) column.style.minHeight = '';
    scroller.scrollTop = scroller.scrollHeight;
  }
  refs.lastAppliedTop.current = scroller.scrollTop;
}

const ANCHOR_TOP_INSET_PX = 20;

// Anchor the arriving plan's own top, never the conversation's zero; fall back
// to bottom when its element is absent.
function scrollItemToTop(
  refs: TimelineScrollRefs,
  scroller: HTMLDivElement,
  itemId: string,
): void {
  const anchor = scroller.querySelector<HTMLElement>(
    `[${TIMELINE_ANCHOR_ATTRIBUTE}="${itemId}"]`,
  );
  if (!anchor) {
    scrollToBottom(refs, scroller);
    return;
  }
  const offset =
    anchor.getBoundingClientRect().top - scroller.getBoundingClientRect().top;
  scroller.scrollTop += offset - ANCHOR_TOP_INSET_PX;
  // Only bottom scrolling establishes a pin; recording a top anchor would jump
  // the reader to the plan's Start button.
  refs.lastAppliedTop.current = NO_APPLIED_TOP;
}

// Initial nonempty landing wins over plan anchors and follow guards; reopened
// chats have no reader position to preserve yet.
function shouldForceInitialBottom(
  refs: TimelineScrollRefs,
  timelineSignature: string,
): boolean {
  if (timelineSignature === '' || !refs.needsInitialScroll.current) {
    return false;
  }
  refs.needsInitialScroll.current = false;
  return true;
}

function shouldSkipFollow(
  refs: TimelineScrollRefs,
  forceInitialBottom: boolean,
  timelineAnchorMode: 'plan' | 'bottom',
  scroller: HTMLDivElement,
): boolean {
  return (
    !forceInitialBottom &&
    timelineAnchorMode === 'bottom' &&
    !isPinnedToBottom(refs, scroller)
  );
}

function applyTimelineScroll(
  refs: TimelineScrollRefs,
  scroller: HTMLDivElement,
  forceInitialBottom: boolean,
  timelineAnchorMode: 'plan' | 'bottom',
) {
  if (!forceInitialBottom && timelineAnchorMode === 'plan') {
    scrollItemToTop(refs, scroller, DRAFT_SPEC_ITEM_ID);
    return;
  }
  scrollToBottom(refs, scroller);
}

// Bottom landing runs before paint, or a reopened chat flashes its top first;
// plan anchors defer until layout settles so their rectangles are current.
function syncTimelineScroll(
  refs: TimelineScrollRefs,
  timelineSignature: string,
  timelineAnchorMode: 'plan' | 'bottom',
) {
  const scroller = refs.scroller.current;
  if (!scroller || refs.previousSignature.current === timelineSignature) {
    return;
  }
  refs.previousSignature.current = timelineSignature;
  const forceInitialBottom = shouldForceInitialBottom(refs, timelineSignature);
  if (
    shouldSkipFollow(refs, forceInitialBottom, timelineAnchorMode, scroller)
  ) {
    return;
  }
  const apply = () =>
    applyTimelineScroll(refs, scroller, forceInitialBottom, timelineAnchorMode);
  if (forceInitialBottom || timelineAnchorMode === 'bottom') {
    apply();
    return;
  }
  const timeout = window.setTimeout(apply, 0);
  return () => window.clearTimeout(timeout);
}

// Lazy Markdown and late attachments grow rows without a signature change;
// keep a pinned reader at the bottom through that growth.
function followContentGrowth(refs: TimelineScrollRefs) {
  const scroller = refs.scroller.current;
  const column = scroller?.firstElementChild;
  if (!scroller || !column || typeof ResizeObserver === 'undefined') return;
  let frame = 0;
  const follow = (layoutResized = false) => {
    const stillFollowing = () =>
      layoutResized
        ? isFollowingLayoutChange(refs, scroller)
        : scroller.scrollTop === refs.lastAppliedTop.current;
    if (stillFollowing()) {
      cancelAnimationFrame(frame);
      frame = requestAnimationFrame(() => {
        frame = 0;
        if (stillFollowing()) scrollToBottom(refs, scroller);
      });
    }
  };
  const resizeObserver = new ResizeObserver(() => follow(true));
  const mutationObserver = new MutationObserver(() => follow());
  const detachFromAutoFollow = () => {
    refs.lastAppliedTop.current = NO_APPLIED_TOP;
    cancelAnimationFrame(frame);
    frame = 0;
  };
  const handleWheel = (event: WheelEvent) => {
    if (event.deltaY !== 0) detachFromAutoFollow();
  };
  const handleKeyScroll = (event: KeyboardEvent) => {
    const target = event.target;
    const isEditable =
      target instanceof HTMLElement &&
      (target.isContentEditable ||
        target.closest('input, textarea, select, [contenteditable="true"]'));
    if (
      !isEditable &&
      [
        'ArrowDown',
        'ArrowUp',
        'End',
        'Home',
        'PageDown',
        'PageUp',
        ' ',
      ].includes(event.key)
    ) {
      detachFromAutoFollow();
    }
  };
  const handleScrollbarPointerDown = (event: PointerEvent) => {
    if (event.target === scroller) detachFromAutoFollow();
  };
  resizeObserver.observe(column);
  resizeObserver.observe(scroller);
  mutationObserver.observe(column, {childList: true, subtree: true});
  scroller.addEventListener('wheel', handleWheel, {passive: true});
  scroller.addEventListener('touchmove', detachFromAutoFollow, {passive: true});
  scroller.addEventListener('pointerdown', handleScrollbarPointerDown, {
    passive: true,
  });
  scroller.addEventListener('keydown', handleKeyScroll, {passive: true});
  return () => {
    resizeObserver.disconnect();
    mutationObserver.disconnect();
    scroller.removeEventListener('wheel', handleWheel);
    scroller.removeEventListener('touchmove', detachFromAutoFollow);
    scroller.removeEventListener('pointerdown', handleScrollbarPointerDown);
    scroller.removeEventListener('keydown', handleKeyScroll);
    cancelAnimationFrame(frame);
  };
}

// Conversation switches do not remount the workspace or pass through empty;
// rearm initial scrolling by identity.
function armInitialScroll(refs: TimelineScrollRefs) {
  refs.needsInitialScroll.current = true;
}

// Sending is an explicit act that reattaches the timeline so the reply cannot
// arrive off screen.
function syncSentTurnScroll(
  refs: TimelineScrollRefs,
  isAwaitingAgent: boolean,
) {
  const scroller = refs.scroller.current;
  if (!isAwaitingAgent || !scroller) return;
  const timeout = window.setTimeout(() => scrollToBottom(refs, scroller), 0);
  return () => window.clearTimeout(timeout);
}

function syncStartedSessionScroll(
  refs: TimelineScrollRefs,
  startedSession: StartedSession | null,
) {
  const scroller = refs.scroller.current;
  if (!startedSession || !scroller) {
    return;
  }
  const timeout = window.setTimeout(() => scrollToBottom(refs, scroller), 0);
  return () => window.clearTimeout(timeout);
}

function syncErrorScroll(refs: TimelineScrollRefs, error: string | null) {
  const scroller = refs.scroller.current;
  if (!error || !scroller) return;
  const timeout = window.setTimeout(() => scrollToBottom(refs, scroller), 0);
  return () => window.clearTimeout(timeout);
}

export function useChatTimelineScroll(
  timelineItems: TimelineItem[],
  startedSession: StartedSession | null,
  isAwaitingAgent = false,
  conversationId?: string,
  error: string | null = null,
  composerRef?: RefObject<HTMLDivElement | null>,
  hasConversation = false,
) {
  const scrollRef = useRef<HTMLDivElement>(null);
  const previousTimelineSignature = useRef('');
  const needsInitialScroll = useRef(true);
  const lastAppliedTop = useRef(NO_APPLIED_TOP);
  const refs: TimelineScrollRefs = {
    scroller: scrollRef,
    previousSignature: previousTimelineSignature,
    needsInitialScroll,
    lastAppliedTop,
  };

  const {signature: timelineSignature, anchorMode: timelineAnchorMode} =
    timelineScrollTarget(timelineItems, startedSession);

  // Rearm before the signature effect when navigation and loaded content land
  // in the same commit.
  useLayoutEffect(() => armInitialScroll(refs), [conversationId]);

  useLayoutEffect(
    () => syncTimelineScroll(refs, timelineSignature, timelineAnchorMode),
    [timelineAnchorMode, timelineSignature],
  );

  // The scroller mounts with the first conversation content.
  const hasContent = timelineItems.length > 0;
  useEffect(() => followContentGrowth(refs), [conversationId, hasContent]);

  // The overlaid composer needs matching timeline bottom padding as it grows.
  useEffect(() => {
    if (!composerRef) return;
    return syncComposerHeight(composerRef, scrollRef, hasConversation, refs);
  }, [composerRef, conversationId, hasConversation]);

  useEffect(() => syncSentTurnScroll(refs, isAwaitingAgent), [isAwaitingAgent]);

  useEffect(() => syncErrorScroll(refs, error), [error]);

  // Key this forced scroll by session id; announcement fragments must not
  // repeatedly override a reader's position.
  useEffect(
    () => syncStartedSessionScroll(refs, startedSession),
    [startedSession?.id],
  );

  return scrollRef;
}
