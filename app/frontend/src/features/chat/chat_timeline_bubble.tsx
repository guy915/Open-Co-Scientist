import type {QaSource} from '@/shared/api/runs';
import {MarkdownMessage} from '@/shared/ui/markdown_message';
import {Button, IconButton, TextArea} from '@/shared/ui';
import {
  useEffect,
  useLayoutEffect,
  useRef,
  useState,
  type CSSProperties,
  type Dispatch,
  type KeyboardEvent,
  type ReactNode,
  type RefObject,
  type SetStateAction,
  type TransitionEvent,
} from 'react';
import {
  MessageActionRow,
  requestActions,
  responseActions,
  type MessageAction,
} from './chat_timeline_message_actions';
import {ThoughtsDisclosure} from './chat_timeline_thoughts';
import {prefersReducedMotion} from '@/shared/lib/reduced_motion';

const CHAT_BUBBLE_USER_ROW_CLASSES =
  'reference-bubble-row user group/user relative flex flex-col items-end justify-end gap-1.5';

export interface ChatEntry {
  id: string;
  role: 'user' | 'assistant';
  content: string;
  // Retain reasoning with the durable reply so it remains readable after
  // streaming.
  reasoning?: string;
  // Optimistic and start-message bubbles lack server turns and cannot be
  // edited or retried.
  turnId?: number;
  messageId?: number;
  // Label deterministic fallback questions so scripted text cannot pass as
  // model output.
  fallback?: boolean;
  created_at: number;
  sources?: QaSource[];
  // The started-session card anchors here rather than on its own clock.
  startRequest?: boolean;
}

export const FALLBACK_NOTICE_TEXT = 'Guided questions (no model available)';

function FallbackTurnNotice() {
  return (
    <p className="mb-1 text-xs font-medium text-cosci-muted">
      {FALLBACK_NOTICE_TEXT}
    </p>
  );
}

export function MessageAttachment({children}: {children: ReactNode}) {
  return (
    <div className="reference-message-attachment mt-3 grid gap-5">
      {children}
    </div>
  );
}

export interface AssistantMessageProps {
  content: string;
  fallback?: boolean;
  reasoning?: string;
  live?: boolean;
  // Hide partial streaming prose from accessibility; announce the settled
  // reply once, not once per token.
  streaming?: boolean;
  ariaLabel?: string;
  attachment?: ReactNode;
  actions?: MessageAction[];
  anchorId?: string;
}

// Anchor the actual turn top rather than guessing scroll offsets.
export const TIMELINE_ANCHOR_ATTRIBUTE = 'data-timeline-anchor';

function TurnActions({actions}: {actions?: MessageAction[]}) {
  if (!actions?.length) return null;
  return <MessageActionRow actions={actions} />;
}

// Keep reasoning, reply, attachment and actions in one row so timeline
// resorting cannot reshuffle them.
export function AssistantMessage({
  content,
  fallback,
  reasoning,
  live,
  streaming,
  ariaLabel,
  attachment,
  actions,
  anchorId,
}: AssistantMessageProps) {
  const Row: 'section' | 'div' = ariaLabel ? 'section' : 'div';
  return (
    <Row
      className="reference-bubble-row relative flex flex-col items-start justify-start gap-3"
      aria-label={ariaLabel}
      {...{[TIMELINE_ANCHOR_ATTRIBUTE]: anchorId}}
    >
      <div className="min-w-0">
        {fallback && <FallbackTurnNotice />}
        <ThoughtsDisclosure
          reasoning={reasoning}
          live={live}
          answering={Boolean(live && content)}
        />
        <div
          className="reference-model-bubble max-w-[50.75rem] text-base leading-[1.45] text-cosci-fg"
          aria-hidden={streaming}
        >
          <MarkdownMessage
            content={content}
            className="reference-model-bubble-text min-w-0 break-words"
          />
          {attachment}
        </div>
      </div>
      <TurnActions actions={actions} />
    </Row>
  );
}

export interface ChatBubbleProps {
  message: ChatEntry;
  onSubmitEdit: (content: string) => void;
  onCopyRequest: () => void;
  onRetry: () => void;
  revisable: boolean;
}

// Shrink the action anchor to the bubble width, or short prompts strand
// controls across the full column.
function UserBubble({
  message,
  bubbleText,
  onEdit,
  onCopyRequest,
}: {
  message: ChatEntry;
  bubbleText: ReturnType<typeof useCollapsibleBubbleText>;
  onEdit: (() => void) | null;
  onCopyRequest: () => void;
}) {
  return (
    <div className={CHAT_BUBBLE_USER_ROW_CLASSES}>
      <div className="relative w-fit">
        <div className="min-w-0">
          <ThoughtsDisclosure reasoning={message.reasoning} />
          <BubbleText
            bubbleClassName="reference-user-bubble flex max-w-[31rem] items-start gap-4 rounded-bubble rounded-tr-sm bg-cosci-user-bubble-bg py-3 pr-3.5 pl-4 text-base leading-[1.45] text-cosci-fg"
            content={message.content}
            {...bubbleText}
          />
        </div>
        <MessageActionRow
          align="end"
          actions={requestActions(onEdit, onCopyRequest)}
        />
      </div>
    </div>
  );
}

export function ChatBubble(props: ChatBubbleProps) {
  const {message, onSubmitEdit, onCopyRequest, onRetry, revisable} = props;
  const isUser = message.role === 'user';
  const [editing, setEditing] = useState(false);
  const bubbleText = useCollapsibleBubbleText(isUser, message.content);

  if (editing) {
    return (
      <div className={CHAT_BUBBLE_USER_ROW_CLASSES}>
        <BubbleEditor
          initial={message.content}
          onCancel={() => setEditing(false)}
          onSubmit={content => {
            setEditing(false);
            onSubmitEdit(content);
          }}
        />
      </div>
    );
  }

  if (!isUser) {
    return (
      <AssistantMessage
        content={message.content}
        fallback={message.fallback}
        reasoning={message.reasoning}
        actions={responseActions(
          revisable ? onRetry : null,
          message.content,
          'co-scientist-response.md',
        )}
      />
    );
  }

  return (
    <UserBubble
      message={message}
      bubbleText={bubbleText}
      onEdit={revisable ? () => setEditing(true) : null}
      onCopyRequest={onCopyRequest}
    />
  );
}

function fitToContent(node: HTMLTextAreaElement | null): void {
  if (!node) return;
  node.style.height = 'auto';
  node.style.height = `${node.scrollHeight}px`;
}

// Edit the durable turn in place; reloading it into the next-message composer
// would append a duplicate.
function BubbleEditor({
  initial,
  onCancel,
  onSubmit,
}: {
  initial: string;
  onCancel: () => void;
  onSubmit: (content: string) => void;
}) {
  const ref = useRef<HTMLTextAreaElement>(null);
  const [value, setValue] = useState(initial);
  const changed = value.trim() !== '' && value.trim() !== initial.trim();

  useEffect(() => {
    const node = ref.current;
    if (!node) return;
    fitToContent(node);
    node.focus();
    node.setSelectionRange(node.value.length, node.value.length);
  }, []);

  function handleKeyDown(event: KeyboardEvent<HTMLTextAreaElement>) {
    if (event.key === 'Escape') {
      event.preventDefault();
      onCancel();
      return;
    }
    if (event.key === 'Enter' && !event.shiftKey) {
      event.preventDefault();
      if (changed) onSubmit(value);
    }
  }

  return (
    // Do not compose flex and block classes: Tailwind output order, not class
    // string order, decides which wins.
    <div className="reference-user-bubble-editor block w-[36rem] max-w-full rounded-bubble rounded-tr-sm bg-cosci-user-bubble-bg px-4 py-3 text-base leading-[1.45] text-cosci-fg">
      <TextArea
        ref={ref}
        rows={1}
        aria-label="Edit prompt"
        variant="bare"
        layoutClassName="block resize-none text-base leading-[1.45]"
        value={value}
        onChange={event => {
          setValue(event.currentTarget.value);
          fitToContent(event.currentTarget);
        }}
        onKeyDown={handleKeyDown}
      />
      <div className="mt-3 flex justify-end gap-2">
        {/* Labelled past their visible text because a plan card's Cancel and
            the composer's Send sit on the same page. */}
        <Button
          variant="outlined"
          size="sm"
          aria-label="Cancel edit"
          onClick={onCancel}
        >
          Cancel
        </Button>
        <Button
          size="sm"
          aria-label="Send edited prompt"
          disabled={!changed}
          onClick={() => onSubmit(value)}
        >
          Send
        </Button>
      </div>
    </div>
  );
}

const COLLAPSED_LINE_COUNT = 4;

// Remove clamp and max-height while measuring to obtain the true expanded
// height.
function measureBubbleHeights(element: HTMLSpanElement) {
  const styles = window.getComputedStyle(element);
  const fontSize = Number.parseFloat(styles.fontSize) || 16;
  const lineHeight =
    Number.parseFloat(styles.lineHeight) || Math.round(fontSize * 1.45);
  const collapsed = Math.round(lineHeight * COLLAPSED_LINE_COUNT);
  const previousMaxHeight = element.style.maxHeight;
  const previousClamp = element.style.getPropertyValue('-webkit-line-clamp');
  const previousDisplay = element.style.display;
  const previousWhiteSpace = element.style.whiteSpace;
  element.style.maxHeight = 'none';
  element.style.setProperty('-webkit-line-clamp', 'unset');
  element.style.display = 'block';
  element.style.whiteSpace = 'pre-wrap';
  const full = element.scrollHeight;
  element.style.maxHeight = previousMaxHeight;
  element.style.setProperty('-webkit-line-clamp', previousClamp);
  element.style.display = previousDisplay;
  element.style.whiteSpace = previousWhiteSpace;
  return {collapsed, full};
}

// Allow two pixels for subpixel rounding when deciding whether a bubble
// overflows.
function exceedsCollapsedHeight(heights: {
  collapsed: number;
  full: number;
}): boolean {
  return heights.full > heights.collapsed + 2;
}

function collapsibleTextClassName(
  collapsible: boolean,
  clamped: boolean,
  expanded: boolean,
): string {
  if (!collapsible)
    return 'reference-user-bubble-text min-w-0 break-words whitespace-pre-wrap';
  const stateClasses =
    clamped && !expanded
      ? 'block whitespace-normal'
      : 'block whitespace-pre-wrap';
  return `${'reference-user-bubble-text min-w-0 break-words overflow-hidden transition-[max-height] duration-long ease-standard motion-reduce:transition-none'} ${stateClasses}`;
}

// Drop the height cap after opening so later resizing cannot clip the bubble.
function collapsibleTextStyle(
  collapsible: boolean,
  expanded: boolean,
  settled: boolean,
  heights: {collapsed: number; full: number},
): CSSProperties | undefined {
  if (!collapsible) return undefined;
  return {
    maxHeight: expanded
      ? settled
        ? undefined
        : `${heights.full}px`
      : `${heights.collapsed}px`,
  };
}

interface BubbleHeights {
  collapsed: number;
  full: number;
}

interface CollapsibleBubbleTextState {
  textRef: RefObject<HTMLSpanElement | null>;
  isUser: boolean;
  canCollapse: boolean;
  expanded: boolean;
  clamped: boolean;
  settled: boolean;
  heights: BubbleHeights;
  setCanCollapse: Dispatch<SetStateAction<boolean>>;
  setExpanded: Dispatch<SetStateAction<boolean>>;
  setClamped: Dispatch<SetStateAction<boolean>>;
  setSettled: Dispatch<SetStateAction<boolean>>;
  setHeights: Dispatch<SetStateAction<BubbleHeights>>;
}

// Reset collapse state when content or role changes so reused nodes cannot
// inherit stale expansion.
function remeasureBubbleOnChange(state: CollapsibleBubbleTextState): void {
  const {isUser, textRef, setCanCollapse, setHeights, setExpanded} = state;
  const {setSettled, setClamped} = state;
  if (!isUser || !textRef.current) {
    setCanCollapse(false);
    return;
  }
  const measured = measureBubbleHeights(textRef.current);
  const exceeds = exceedsCollapsedHeight(measured);
  setHeights(measured);
  setCanCollapse(exceeds);
  setExpanded(false);
  setSettled(false);
  setClamped(exceeds);
}

function toggleBubbleExpanded(
  state: Pick<
    CollapsibleBubbleTextState,
    | 'textRef'
    | 'expanded'
    | 'setHeights'
    | 'setExpanded'
    | 'setClamped'
    | 'setSettled'
  >,
): void {
  const {textRef, expanded, setHeights, setExpanded, setClamped, setSettled} =
    state;
  const element = textRef.current;
  if (element) setHeights(measureBubbleHeights(element));
  if (expanded) {
    // Use a concrete height before collapse; restore the clamp after the
    // transition.
    setSettled(false);
    if (prefersReducedMotion()) {
      setExpanded(false);
      setClamped(true);
    } else {
      requestAnimationFrame(() => setExpanded(false));
    }
  } else {
    // Drop the clamp before expansion so max-height can animate.
    setClamped(false);
    if (prefersReducedMotion()) {
      setExpanded(true);
      setSettled(true);
    } else {
      requestAnimationFrame(() => setExpanded(true));
    }
  }
}

function handleBubbleTransitionEndFor(
  expanded: boolean,
  setSettled: Dispatch<SetStateAction<boolean>>,
  setClamped: Dispatch<SetStateAction<boolean>>,
  event: TransitionEvent<HTMLSpanElement>,
): void {
  if (event.propertyName !== 'max-height') return;
  if (expanded) {
    setSettled(true);
  } else {
    setClamped(true);
  }
}

function deriveBubbleTextPresentation(
  collapsible: boolean,
  clamped: boolean,
  expanded: boolean,
  settled: boolean,
  heights: BubbleHeights,
): {bubbleTextClassName: string; bubbleTextStyle: CSSProperties | undefined} {
  return {
    bubbleTextClassName: collapsibleTextClassName(
      collapsible,
      clamped,
      expanded,
    ),
    bubbleTextStyle: collapsibleTextStyle(
      collapsible,
      expanded,
      settled,
      heights,
    ),
  };
}

function collapsibleBubbleTextResult(state: CollapsibleBubbleTextState) {
  const collapsible = state.isUser && state.canCollapse;
  return {
    textRef: state.textRef,
    collapsible,
    expanded: state.expanded,
    ...deriveBubbleTextPresentation(
      collapsible,
      state.clamped,
      state.expanded,
      state.settled,
      state.heights,
    ),
    toggleExpanded: () => toggleBubbleExpanded(state),
    handleBubbleTransitionEnd: (event: TransitionEvent<HTMLSpanElement>) =>
      handleBubbleTransitionEndFor(
        state.expanded,
        state.setSettled,
        state.setClamped,
        event,
      ),
  };
}

function useCollapsibleBubbleText(isUser: boolean, content: string) {
  const textRef = useRef<HTMLSpanElement>(null);
  const [canCollapse, setCanCollapse] = useState(false);
  const [expanded, setExpanded] = useState(false);
  const [clamped, setClamped] = useState(false);
  const [settled, setSettled] = useState(false);
  const [heights, setHeights] = useState<BubbleHeights>({
    collapsed: 0,
    full: 0,
  });

  const state: CollapsibleBubbleTextState = {
    textRef,
    isUser,
    canCollapse,
    expanded,
    clamped,
    settled,
    heights,
    setCanCollapse,
    setExpanded,
    setClamped,
    setSettled,
    setHeights,
  };

  useLayoutEffect(() => remeasureBubbleOnChange(state), [isUser, content]);

  return collapsibleBubbleTextResult(state);
}

interface BubbleTextProps {
  bubbleClassName: string;
  content: string;
  textRef: RefObject<HTMLSpanElement | null>;
  collapsible: boolean;
  expanded: boolean;
  bubbleTextClassName: string;
  bubbleTextStyle: CSSProperties | undefined;
  toggleExpanded: () => void;
  handleBubbleTransitionEnd: (event: TransitionEvent<HTMLSpanElement>) => void;
}

// Render scientist text literally; only assistant prose goes through markdown.
function BubbleText({
  bubbleClassName,
  content,
  textRef,
  collapsible,
  expanded,
  bubbleTextClassName,
  bubbleTextStyle,
  toggleExpanded,
  handleBubbleTransitionEnd,
}: BubbleTextProps) {
  return (
    <div className={bubbleClassName}>
      <span
        ref={textRef}
        className={bubbleTextClassName}
        style={bubbleTextStyle}
        onTransitionEnd={collapsible ? handleBubbleTransitionEnd : undefined}
      >
        {content}
      </span>
      {collapsible && (
        <CollapseToggleButton expanded={expanded} onToggle={toggleExpanded} />
      )}
    </div>
  );
}

// Pointer activation releases hover focus; keyboard activation keeps actions
// reachable.
function CollapseToggleButton({
  expanded,
  onToggle,
}: {
  expanded: boolean;
  onToggle: () => void;
}) {
  return (
    <IconButton
      icon={expanded ? 'expand_less' : 'expand_more'}
      label={expanded ? 'Collapse' : 'Expand'}
      tooltipPlacement="right"
      layoutClassName="reference-user-collapse"
      onClick={event => {
        onToggle();
        if (event.detail > 0) event.currentTarget.blur();
      }}
    />
  );
}
