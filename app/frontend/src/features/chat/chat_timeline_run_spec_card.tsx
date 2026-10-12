import type {RunFocus, RunTier} from '@/shared/api/runs';
import {displayTitle} from '@/shared/lib/titles';
import {Link} from 'react-router-dom';
import {Button, buttonClasses, IconButton, StatusText} from '@/shared/ui';
import {
  joinClasses,
  OPTION_MARKER_CLASSES,
  OPTION_MARKER_SELECTED_CLASSES,
  SETUP_ACTIONS_CLASSES,
} from '@/shared/ui/classes';
import {TruncatedLabel} from '@/shared/ui/truncated_label';
import {useLaunchStatus} from '@/shared/hooks/launch_status_context';
import {LAUNCH_NOTICE_ID} from '@/shared/ui/launch_status_banner';
import {
  type InferredRunSpec,
  availableTierOptions,
  FOCUS_OPTIONS,
  runOptionLabel,
  TIER_OPTIONS,
} from '@/shared/lib/run_spec';
import {AssistantMessage, MessageAttachment} from './chat_timeline_bubble';
import {responseActions} from './chat_timeline_message_actions';
import {
  type SpecFieldsEditor,
  SpecFieldsSection,
  useSpecFieldsEditor,
} from './chat_timeline_run_spec_editor';

interface RunSpecCardProps {
  spec: InferredRunSpec;
  // Anchor only newly arriving draft cards; confirmed cards replace a turn
  // already on screen.
  anchorId?: string;
  isStarting: boolean;
  locked?: boolean;
  recoveryAction?: boolean;
  recoveryLookupStatus?: 'checking' | 'error' | 'cancelled';
  onRetryStatusLookup?: () => void;
  intro?: string;
  introReasoning?: string;
  introFallback?: boolean;
  onFocusChange: (focus: RunFocus) => void;
  onTierChange: (tier: RunTier) => void;
  onFieldsChange: (patch: Partial<InferredRunSpec>) => void;
  onCancel: () => void;
  onRetry: () => void;
  onStart: () => void;
}

// Use || so an empty closing message also receives the fallback lead-in.
function planIntroText(intro?: string): string {
  return (
    planLeadIn(intro) ||
    'The interview is complete. I derived the research setup below from ' +
      'your answers.'
  );
}

function planInstructions(recoveryAction?: boolean, locked?: boolean): string {
  if (recoveryAction) {
    return 'This research setup is saved as a draft. Continue research to start the same session.';
  }
  if (locked) return 'This saved plan belongs to the research session below.';
  return 'Review the research setup and select a focus and run type. Once ready, click "Start research" to begin.';
}

function startActionLabel(
  isStarting: boolean,
  recoveryAction: boolean,
): string {
  if (isStarting) return recoveryAction ? 'Continuing...' : 'Starting...';
  return recoveryAction ? 'Continue research' : 'Start research';
}

function startActionDisabled({
  isStarting,
  locked,
  recoveryAction,
  runBlocked,
}: {
  isStarting: boolean;
  locked: boolean;
  recoveryAction: boolean;
  runBlocked: boolean;
}): boolean {
  return isStarting || (locked && !recoveryAction) || runBlocked;
}

function startStatusMessage(recoveryAction: boolean): string {
  return recoveryAction ? 'Continuing research' : 'Starting research';
}

function runSpecResponseActions(props: RunSpecCardProps, responseText: string) {
  return responseActions(
    props.recoveryAction ? null : props.onRetry,
    responseText,
    'co-scientist-research-plan.md',
  );
}

function runPlanEditAction(
  props: RunSpecCardProps,
  editor: SpecFieldsEditor,
): (() => void) | undefined {
  if (props.locked || !props.spec.interviewId || editor.editing) {
    return undefined;
  }
  return editor.startEditing;
}

export function RunSpecCard(props: RunSpecCardProps) {
  const responseText = formatRunSpecResponse(props.spec);
  const locked = props.locked ?? false;
  const editor = useSpecFieldsEditor(props.spec, props.onFieldsChange);

  return (
    <AssistantMessage
      content={planIntroText(props.intro)}
      fallback={props.introFallback}
      reasoning={props.introReasoning}
      ariaLabel="Inferred run setup"
      anchorId={props.anchorId}
      attachment={
        <MessageAttachment>
          <p className="reference-review-copy m-0 text-base leading-6">
            {planInstructions(props.recoveryAction, locked)}
          </p>
          <RecoveryLookupStatus
            status={props.recoveryLookupStatus}
            onRetry={props.onRetryStatusLookup}
          />
          <PlanHeading onEdit={runPlanEditAction(props, editor)} />
          <p className="reference-plan-subheading -mt-1.5 m-0 text-cosci-muted">
            Here's my plan to tackle the topic:
          </p>
          <RunSpecDocument
            spec={props.spec}
            locked={locked}
            isStarting={props.isStarting}
            recoveryAction={props.recoveryAction ?? false}
            editor={editor}
            onFocusChange={props.onFocusChange}
            onTierChange={props.onTierChange}
            onCancel={props.onCancel}
            onStart={props.onStart}
          />
        </MessageAttachment>
      }
      actions={runSpecResponseActions(props, responseText)}
    />
  );
}

// Hide editing when no durable interview can save it; the open editor has its
// own Save and Cancel.
function PlanHeading({onEdit}: {onEdit?: () => void}) {
  return (
    <div className="reference-plan-heading flex items-center gap-2">
      <h2 className="m-0 text-[2rem] leading-[1.2] font-normal tracking-normal text-cosci-fg max-[720px]:text-[clamp(1.5rem,6.8vw,2rem)]">
        Research plan
      </h2>
      {onEdit && <IconButton icon="edit" label="Edit plan" onClick={onEdit} />}
    </div>
  );
}

function SpecOptionGroups({
  spec,
  locked,
  onFocusChange,
  onTierChange,
}: {
  spec: InferredRunSpec;
  locked: boolean;
  onFocusChange: (focus: RunFocus) => void;
  onTierChange: (tier: RunTier) => void;
}) {
  return (
    <>
      <RunOptionGroup
        label="Focus"
        name="focus"
        value={spec.focus}
        options={FOCUS_OPTIONS}
        locked={locked}
        onChange={value => onFocusChange(value as RunFocus)}
      />
      <RunOptionGroup
        label="Run type"
        name="tier"
        value={spec.tier}
        options={availableTierOptions()}
        locked={locked}
        onChange={value => onTierChange(value as RunTier)}
      />
    </>
  );
}

interface RunSpecDocumentProps {
  spec: InferredRunSpec;
  locked: boolean;
  isStarting: boolean;
  recoveryAction: boolean;
  editor: SpecFieldsEditor;
  onFocusChange: (focus: RunFocus) => void;
  onTierChange: (tier: RunTier) => void;
  onCancel: () => void;
  onStart: () => void;
}

function RunSpecDocument(props: RunSpecDocumentProps) {
  const {
    spec,
    locked,
    isStarting,
    recoveryAction,
    editor,
    onFocusChange,
    onTierChange,
    onCancel,
    onStart,
  } = props;
  return (
    <div className="reference-setup-document grid gap-5 rounded-2xl bg-cosci-setup-doc-bg p-[1.5rem_1.45rem]">
      <h3 className="m-0 text-[1.45rem] leading-[1.25] font-semibold">
        {displayTitle(null, spec.goal)}
      </h3>
      <SpecFieldsSection spec={spec} editor={editor} />
      <SpecOptionGroups
        spec={spec}
        locked={locked}
        onFocusChange={onFocusChange}
        onTierChange={onTierChange}
      />
      <RunSpecActions
        locked={locked}
        isStarting={isStarting}
        recoveryAction={recoveryAction}
        onCancel={onCancel}
        onStart={onStart}
      />
    </div>
  );
}

// A linked draft stays locked for editing but remains explicitly startable
// after refresh.
function RunSpecActions({
  locked,
  isStarting,
  recoveryAction,
  onCancel,
  onStart,
}: {
  locked: boolean;
  isStarting: boolean;
  recoveryAction: boolean;
  onCancel: () => void;
  onStart: () => void;
}) {
  const {status, runBlocked} = useLaunchStatus();
  return (
    <div className={SETUP_ACTIONS_CLASSES}>
      {!locked && (
        <Button variant="outlined" onClick={onCancel} disabled={isStarting}>
          Cancel
        </Button>
      )}
      <Button
        aria-describedby={status?.message ? LAUNCH_NOTICE_ID : undefined}
        title={status?.message ?? undefined}
        aria-busy={isStarting}
        onClick={onStart}
        disabled={startActionDisabled({
          isStarting,
          locked,
          recoveryAction,
          runBlocked,
        })}
      >
        {startActionLabel(isStarting, recoveryAction)}
      </Button>
      {status?.message && (
        <p className="m-0 basis-full break-words text-sm text-cosci-muted">
          {status.message}
        </p>
      )}
      {isStarting && (
        <span className="sr-only" role="status" aria-live="polite">
          {startStatusMessage(recoveryAction)}
        </span>
      )}
    </div>
  );
}

function formatRunSpecResponse(spec: InferredRunSpec): string {
  return [
    `# ${displayTitle(null, spec.goal)}`,
    '',
    'Agent interview-derived research setup.',
    '',
    '## Research Challenge',
    spec.goal,
    '',
    '## Focus Area',
    ...spec.attributes.map(value => `* ${value}`),
    '',
    '## Preferences',
    ...spec.requirements.map(value => `* ${value}`),
    '',
    ...(spec.criteria.length
      ? ['## Criteria', ...spec.criteria.map(value => `* ${value}`), '']
      : []),
    '## Setup Options',
    `* **Focus:** ${runOptionLabel(FOCUS_OPTIONS, spec.focus, spec.focus)}`,
    `* **Run type:** ${runOptionLabel(TIER_OPTIONS, spec.tier, spec.tier)}`,
  ].join('\n');
}

interface RunOptionGroupOption {
  id: string;
  label: string;
  description: string;
  disabled?: boolean;
  hint?: string;
}

interface OptionCardProps {
  option: RunOptionGroupOption;
  name: string;
  selected: boolean;
  locked: boolean;
  onChange: (value: string) => void;
}

// A saved plan is a record, so it reads at full contrast without hover
// affordances; only an option unavailable to choose is faded.
function optionCardState(locked: boolean, unavailable: boolean): string {
  if (locked) return 'cursor-default';
  if (unavailable) return 'cursor-not-allowed opacity-50';
  return 'cursor-pointer hover:bg-cosci-option-hover-bg has-[:focus-visible]:border-cosci-option-hover-border has-[:focus-visible]:bg-cosci-option-hover-bg';
}

function OptionCard(props: OptionCardProps) {
  const {option, name, selected, locked, onChange} = props;
  const unavailable = Boolean(option.disabled);
  return (
    <label
      className={joinClasses(
        'reference-option-card relative grid min-h-[4.75rem] grid-cols-[1.6rem_minmax(0,1fr)] content-start gap-x-3 rounded-xl border border-transparent bg-cosci-option-bg px-4 py-3.5 text-cosci-fg',
        optionCardState(locked, unavailable),
      )}
    >
      <input
        type="radio"
        className="absolute pointer-events-none opacity-0"
        name={name}
        value={option.id}
        checked={selected}
        disabled={locked || unavailable}
        onChange={() => onChange(option.id)}
      />
      <span
        className={joinClasses(
          OPTION_MARKER_CLASSES,
          selected && OPTION_MARKER_SELECTED_CLASSES,
        )}
        aria-hidden="true"
      />
      <strong className="min-w-0 text-base leading-[1.2] font-bold">
        {option.label}
      </strong>
      <small className="col-start-2 text-[0.92rem] leading-[1.3] text-cosci-muted">
        {option.description}
        {option.hint && (
          <span className="mt-1 block text-xs">{option.hint}</span>
        )}
      </small>
    </label>
  );
}

function RunOptionGroup({
  label,
  name,
  value,
  options,
  locked,
  onChange,
}: {
  label: string;
  name: string;
  value: string;
  options: readonly RunOptionGroupOption[];
  locked: boolean;
  onChange: (value: string) => void;
}) {
  return (
    <fieldset
      className="reference-option-group m-0 grid min-w-0 gap-3.5 border-0 p-0"
      aria-label={label}
    >
      <legend className="text-[1.18rem] font-bold text-cosci-fg">
        {label}
      </legend>
      <div className="grid grid-cols-2 gap-3.5 max-[720px]:grid-cols-1">
        {options.map(option => (
          <OptionCard
            key={option.id}
            option={option}
            name={name}
            selected={option.id === value}
            locked={locked}
            onChange={onChange}
          />
        ))}
      </div>
    </fieldset>
  );
}

type LookupStatus = 'checking' | 'error' | 'cancelled' | undefined;

function RecoveryLookupStatus({
  status,
  onRetry,
}: {
  status: LookupStatus;
  onRetry?: () => void;
}) {
  if (status === 'checking') {
    return <StatusText>Checking saved research session status…</StatusText>;
  }
  if (status === 'error') {
    return (
      <StatusText tone="danger" as="div">
        <p>Could not verify the saved run status.</p>
        <Button variant="outlined" onClick={onRetry}>
          Retry status check
        </Button>
      </StatusText>
    );
  }
  if (status === 'cancelled') {
    return <StatusText>The linked research session was cancelled.</StatusText>;
  }
  return null;
}

export interface StartedSession {
  id: string;
  title: string;
  at: number;
  intro?: string;
  reasoning?: string;
  // Distinguish a reply still filling from one absent permanently so fallback
  // copy cannot flash before model prose.
  announcing?: boolean;
}

const STARTED_SESSION_STANDBY_COPY =
  'Your session has been started and Co-Scientist has started research!' +
  '\n\n' +
  'You can view and interact with your session at any time, but note that ' +
  'it might take a few minutes for the first ideas to be ready to view.';

function introCopy(session: StartedSession): string {
  const written = session.intro?.trim();
  if (written) return written;
  return session.announcing ? '' : STARTED_SESSION_STANDBY_COPY;
}

// Use real links for browser navigation; omit retry because the server has
// already started this run.
export function StartedSessionCard({
  session,
  href,
  onNewTopic,
}: {
  session: StartedSession;
  href: string;
  onNewTopic: () => void;
}) {
  const intro = introCopy(session);
  const responseText = formatStartedSessionResponse(session, intro);

  return (
    <AssistantMessage
      content={intro}
      reasoning={session.reasoning}
      live={session.announcing}
      ariaLabel="Started research session"
      attachment={
        // Wait for the announcement before showing its session attachment;
        // reopened history has no announcement to await.
        session.announcing ? undefined : (
          <MessageAttachment>
            <SessionLinkCard session={session} href={href} />
            <SessionNextActions href={href} onNewTopic={onNewTopic} />
          </MessageAttachment>
        )
      }
      actions={responseActions(
        null,
        responseText,
        'co-scientist-session-started.md',
      )}
    />
  );
}

function SessionLinkCard({
  session,
  href,
}: {
  session: StartedSession;
  href: string;
}) {
  return (
    <Link
      to={href}
      className="reference-started-session-card grid min-h-[5.3rem] cursor-pointer grid-cols-[minmax(0,1fr)_auto] items-center gap-5 rounded-2xl border-0 p-[1rem_1rem_1rem_1.35rem] text-left text-started-card-fg no-underline"
    >
      <span className="block min-w-0">
        <strong className="block min-w-0 text-[1.18rem] leading-[1.25]">
          <TruncatedLabel
            className="block min-w-0 overflow-hidden whitespace-nowrap"
            text={session.title}
          />
        </strong>
        <small className="mt-1 block text-[0.9rem] text-started-card-fg/80">
          Research session
        </small>
      </span>
      <span className="reference-started-open min-w-[5.4rem] rounded-full border border-started-card-fg/75 px-5 py-2.5 text-center font-semibold text-started-card-fg/90 hover:bg-started-card-fg/12 focus-visible:bg-started-card-fg/12">
        Open
      </span>
    </Link>
  );
}

function SessionNextActions({
  href,
  onNewTopic,
}: {
  href: string;
  onNewTopic: () => void;
}) {
  return (
    <div className="reference-started-next flex flex-wrap items-center gap-2">
      <p className="basis-full m-0 mb-0.5 text-[0.95rem] font-semibold text-cosci-muted">
        What would you like to do next?
      </p>
      <Link to={href} className={buttonClasses({variant: 'accent'})}>
        View session details
      </Link>
      <Button variant="accent" onClick={onNewTopic}>
        Start a new research goal session on a new topic
      </Button>
    </div>
  );
}

// Copy the reply actually displayed rather than a second wording that could
// drift.
function formatStartedSessionResponse(
  session: StartedSession,
  intro: string,
): string {
  return [
    `# ${session.title}`,
    '',
    intro || STARTED_SESSION_STANDBY_COPY,
    '',
    '* **Type:** Research session',
    '* **Action:** Open the session details when you want to inspect progress.',
  ].join('\n');
}

// Accept paraphrased plan headings because the model is not required to echo
// exact field labels.
const PLAN_FIELD_HEADINGS = new Set([
  'research challenge',
  'research goal',
  'research question',
  'challenge',
  'goal',
  'focus area',
  'focus areas',
  'focus',
  'preference',
  'preferences',
  'lab constraint',
  'lab constraints',
  'laboratory constraints',
  'constraints',
  'title',
]);

const HEADING_PATTERN = /^ {0,3}#{1,6} +(.*?) *#* *$/;

const STRUCTURED_LINE = /^ {0,3}([#>|]|[-*+] |\d+[.)] )/;

const RULE_LINE = /^ {0,3}(-{3,}|\*{3,}|_{3,}) *$/;

// Ignore emphasis, numbering and parentheticals when recognizing model-written
// plan headings.
function headingLabel(line: string): string | null {
  const match = HEADING_PATTERN.exec(line);
  if (!match) return null;
  return match[1]
    .replace(/[*_`\\]/g, '')
    .replace(/^\d+[.)]\s*/, '')
    .replace(/\s*\([^)]*\)\s*$/, '')
    .replace(/[:.\-–—]+\s*$/, '')
    .trim()
    .toLowerCase();
}

function withoutFieldSections(source: string): string {
  const kept: string[] = [];
  let dropping = false;
  for (const line of source.split('\n')) {
    const label = headingLabel(line);
    if (label !== null) dropping = PLAN_FIELD_HEADINGS.has(label);
    if (!dropping) kept.push(line);
  }
  return kept.join('\n');
}

function isBlankOrRule(line: string): boolean {
  return !line.trim() || RULE_LINE.test(line);
}

function tidy(text: string): string {
  const lines = text.split('\n');
  while (lines.length && isBlankOrRule(lines[0])) lines.shift();
  while (lines.length && isBlankOrRule(lines[lines.length - 1])) lines.pop();
  return lines.join('\n').replace(/\n{3,}/g, '\n\n');
}

// Preserve the completing turn's prose sign-off even when it falls inside the
// last stripped plan section.
function signOff(source: string): string {
  const blocks = source.split(/\n[ \t]*\n/);
  const last = (blocks[blocks.length - 1] ?? '').trim();
  if (!last) return '';
  const structured = last.split('\n').some(line => STRUCTURED_LINE.test(line));
  return structured ? '' : last;
}

export function planLeadIn(intro?: string): string {
  const source = (intro ?? '').trim();
  if (!source) return '';
  return withSignOff(tidy(withoutFieldSections(source)), signOff(source));
}

function withSignOff(kept: string, closing: string): string {
  if (!closing || kept.endsWith(closing)) return kept;
  return kept ? `${kept}\n\n${closing}` : closing;
}
