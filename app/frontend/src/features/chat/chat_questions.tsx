import {
  type Interview,
  type InterviewQuestion,
  type InterviewTurn,
} from '@/shared/api/runs';
import {type ReactNode, useState} from 'react';
import {Icon} from '@/shared/ui/icon';
import {Button, Chip, IconButton, TextField} from '@/shared/ui';
import {joinClasses} from '@/shared/ui/classes';
import {MarkdownMessage} from '@/shared/ui/markdown_message';
import {
  OPTION_MARKER_CLASSES,
  OPTION_MARKER_SELECTED_CLASSES,
} from '@/shared/ui/classes';

// Key answers by question position: separate turns can ask identical text.
export interface QuestionSelections {
  chosen: Record<number, string[]>;
  other: Record<number, string>;
}

export function emptySelections(): QuestionSelections {
  return {chosen: {}, other: {}};
}

function nextChosen(
  current: string[],
  label: string,
  multiSelect: boolean,
): string[] {
  if (current.includes(label)) {
    return current.filter(chosen => chosen !== label);
  }
  return multiSelect ? [...current, label] : [label];
}

// A single-select question holds one answer, so a click and the scientist's
// own wording replace each other.
export function toggleOption(
  selections: QuestionSelections,
  index: number,
  label: string,
  multiSelect: boolean,
): QuestionSelections {
  const other = multiSelect
    ? selections.other
    : {...selections.other, [index]: ''};
  return {
    other,
    chosen: {
      ...selections.chosen,
      [index]: nextChosen(selections.chosen[index] ?? [], label, multiSelect),
    },
  };
}

function setOther(
  selections: QuestionSelections,
  index: number,
  text: string,
  multiSelect: boolean,
): QuestionSelections {
  const chosen =
    multiSelect || !text.trim()
      ? selections.chosen
      : {...selections.chosen, [index]: []};
  return {chosen, other: {...selections.other, [index]: text}};
}

function answerParts(selections: QuestionSelections, index: number): string[] {
  const other = (selections.other[index] ?? '').trim();
  const chosen = selections.chosen[index] ?? [];
  return other ? [...chosen, other] : chosen;
}

// Include question headers so a turn answering several questions stays
// unambiguous.
function answerLine(
  question: InterviewQuestion,
  selections: QuestionSelections,
  index: number,
): string {
  const parts = answerParts(selections, index);
  if (!parts.length) return '';
  const answer = parts.join(', ');
  return question.header ? `${question.header}: ${answer}` : answer;
}

// Clicked answers use ordinary interview turns so the model sees the same
// transcript as typed answers.
export function answerText(
  questions: InterviewQuestion[],
  selections: QuestionSelections,
): string {
  return questions
    .map((question, index) => answerLine(question, selections, index))
    .filter(Boolean)
    .join('\n');
}

export interface PendingQuestions {
  turnId: number;
  questions: InterviewQuestion[];
}

// Only the last unanswered Agent turn can be pending; completed interviews no
// longer ask questions.
export function pendingQuestions(
  interview: Pick<Interview, 'turns' | 'status'> | null,
): PendingQuestions | null {
  const last = lastPendingTurn(interview);
  if (!last) return null;
  // Legacy or malformed question fields must not take down the composer.
  const questions = last.questions ?? [];
  return questions.length ? {turnId: last.id, questions} : null;
}

function lastPendingTurn(
  interview: Pick<Interview, 'turns' | 'status'> | null,
): InterviewTurn | null {
  if (!interview || interview.status !== 'active') return null;
  const last = interview.turns.at(-1);
  return last?.role === 'agent' ? last : null;
}

const ROW_BASE_CLASSES =
  'relative grid min-h-[3rem] grid-cols-[1.6rem_minmax(0,1fr)] items-center gap-x-3 rounded-xl border border-transparent bg-cosci-option-bg px-4 py-2.5 text-cosci-fg hover:bg-cosci-option-hover-bg';

const QUESTION_OPTION_ROW_CLASSES = `${ROW_BASE_CLASSES} has-[:focus-visible]:border-cosci-option-hover-border has-[:focus-visible]:bg-cosci-option-hover-bg`;

// A text field matches :focus-visible on every focus, so the row would draw
// a second outline around the field's own.
const OTHER_ROW_CLASSES = ROW_BASE_CLASSES;

// Offer free text for answers the model did not propose; an empty field skips
// rather than rejects a question.
const OTHER_LABEL = 'Something else';

const OTHER_PLACEHOLDER = 'Type your own answer';

export interface QuestionChooserProps {
  questions: InterviewQuestion[];
  onAnswer: (text: string) => void;
}

// Selection never sends a turn; the chooser owns commit. Remount on turn id to
// reset selections and dismissal. Several questions show one at a time, as
// tabs the scientist steps through, never as one scrolling stack. Only the
// options scroll, and only past a height a typical question fits within on
// desktop (dvh follows the Safari toolbar on phones); the head stays outside
// the scroller so its tooltips cannot widen it into a horizontal scrollbar.
export function QuestionChooser({questions, onAnswer}: QuestionChooserProps) {
  const [selections, setSelections] = useState(emptySelections);
  const [active, setActive] = useState(0);
  const [minimized, setMinimized] = useState(false);
  const [dismissed, setDismissed] = useState(false);
  if (dismissed) return null;

  const current = Math.min(active, questions.length - 1);
  const question = questions[current];
  const answer = answerText(questions, selections);
  const sendAnswer = () => onAnswer(answer);
  // A single-select pick settles its question, so the next one opens.
  const choose = (index: number, label: string) => {
    const next = toggleOption(selections, index, label, question.multi_select);
    setSelections(next);
    const picked = (next.chosen[index] ?? []).length > 0;
    if (!question.multi_select && picked && index < questions.length - 1) {
      setActive(index + 1);
    }
  };

  return (
    <section
      className="mb-2.5 grid gap-2.5 border-b border-cosci-composer-border pb-2.5"
      aria-label="Answer options"
    >
      <ChooserHead
        answered={answer.length > 0}
        onSend={sendAnswer}
        minimized={minimized}
        onToggleMinimize={() => setMinimized(open => !open)}
        onDismiss={() => setDismissed(true)}
      >
        <QuestionTabs
          questions={questions}
          selections={selections}
          active={current}
          onSelect={setActive}
        />
      </ChooserHead>
      {!minimized && (
        <div
          id={questionPanelId(current)}
          role={questions.length > 1 ? 'tabpanel' : undefined}
          aria-labelledby={
            questions.length > 1 ? questionTabId(current) : undefined
          }
          className="grid max-h-[min(18rem,40dvh)] min-w-0 gap-2.5 overflow-x-hidden overflow-y-auto overscroll-contain above-phone:max-h-[min(36rem,70dvh)]"
        >
          <QuestionGroup
            key={current}
            question={question}
            index={current}
            selections={selections}
            setSelections={setSelections}
            onChoose={choose}
          />
        </div>
      )}
    </section>
  );
}

function questionLabel(question: InterviewQuestion): string {
  return question.header || question.question;
}

function questionTabId(index: number): string {
  return `question-tab-${index}`;
}

function questionPanelId(index: number): string {
  return `question-panel-${index}`;
}

const HEAD_LABEL_CLASSES =
  'min-w-0 truncate text-[0.82rem] font-medium tracking-[0.04em] uppercase';

const TAB_KEY_STEPS: Record<string, number> = {ArrowLeft: -1, ArrowRight: 1};

// One question keeps a plain label; several become tabs with arrows either
// side, so the scientist steps between them instead of scrolling.
function QuestionTabs({
  questions,
  selections,
  active,
  onSelect,
}: {
  questions: InterviewQuestion[];
  selections: QuestionSelections;
  active: number;
  onSelect: (index: number) => void;
}) {
  if (questions.length === 1) {
    return (
      <span className={joinClasses(HEAD_LABEL_CLASSES, 'text-cosci-muted')}>
        {questionLabel(questions[0])}
      </span>
    );
  }
  return (
    <div className="flex min-w-0 flex-1 items-center gap-0.5">
      <IconButton
        label="Previous question"
        icon="chevron_left"
        disabled={active === 0}
        onClick={() => onSelect(active - 1)}
      />
      <div
        role="tablist"
        aria-label="Questions"
        className="flex min-w-0 items-center gap-1 overflow-x-auto [scrollbar-width:none]"
        onKeyDown={event => {
          const step = TAB_KEY_STEPS[event.key];
          const next = active + (step ?? 0);
          if (!step || next < 0 || next >= questions.length) return;
          event.preventDefault();
          onSelect(next);
          document.getElementById(questionTabId(next))?.focus();
        }}
      >
        {questions.map((question, index) => (
          <QuestionTab
            key={index}
            index={index}
            label={questionLabel(question)}
            selected={index === active}
            answered={answerParts(selections, index).length > 0}
            onSelect={() => onSelect(index)}
          />
        ))}
      </div>
      <IconButton
        label="Next question"
        icon="chevron_right"
        disabled={active === questions.length - 1}
        onClick={() => onSelect(active + 1)}
      />
    </div>
  );
}

function QuestionTab({
  index,
  label,
  selected,
  answered,
  onSelect,
}: {
  index: number;
  label: string;
  selected: boolean;
  answered: boolean;
  onSelect: () => void;
}) {
  return (
    <Button
      variant={selected ? 'tonal' : 'text'}
      size="sm"
      icon={answered ? 'check' : undefined}
      role="tab"
      id={questionTabId(index)}
      aria-selected={selected}
      aria-controls={selected ? questionPanelId(index) : undefined}
      tabIndex={selected ? 0 : -1}
      // Phones have room for the open tab only; the arrows reach the others.
      layoutClassName={selected ? 'min-w-0' : 'shrink-0 phone:hidden'}
      onClick={onSelect}
    >
      <span className="min-w-0 truncate">{label}</span>
    </Button>
  );
}

// One send commits every question's answer, so it sits in the head rather
// than beside any single question's field.
function ChooserHead({
  children,
  answered,
  onSend,
  minimized,
  onToggleMinimize,
  onDismiss,
}: {
  children: ReactNode;
  answered: boolean;
  onSend: () => void;
  minimized: boolean;
  onToggleMinimize: () => void;
  onDismiss: () => void;
}) {
  return (
    <div className="flex min-w-0 items-center justify-between gap-2.5">
      {children}
      <div className="flex shrink-0 items-center gap-0.5">
        <Button
          size="sm"
          icon="send"
          aria-label="Send answer"
          layoutClassName="mr-1.5"
          disabled={!answered}
          onClick={onSend}
        >
          {/* Phones keep the room for the question tabs. */}
          <span className="phone:sr-only">Send</span>
        </Button>
        <IconButton
          label={minimized ? 'Show the questions' : 'Minimize the questions'}
          icon={minimized ? 'expand_less' : 'expand_more'}
          onClick={onToggleMinimize}
        />
        <IconButton
          label="Dismiss the questions"
          icon="close"
          onClick={onDismiss}
        />
      </div>
    </div>
  );
}

interface QuestionGroupProps {
  question: InterviewQuestion;
  index: number;
  selections: QuestionSelections;
  setSelections: (selections: QuestionSelections) => void;
  onChoose: (index: number, label: string) => void;
}

function QuestionGroup(props: QuestionGroupProps) {
  const {question, index, selections, setSelections, onChoose} = props;
  return (
    <fieldset className="m-0 grid min-w-0 gap-2 border-0 p-0">
      <legend className="text-base leading-[1.35] font-medium text-cosci-fg">
        <MarkdownMessage content={question.question} inline />
      </legend>
      <div className={joinClasses('grid grid-cols-1', 'mt-0.5')}>
        {question.options.map(option => (
          <AnswerRow
            key={option.label}
            label={option.label}
            description={option.description}
            recommended={option.recommended === true}
            multiSelect={question.multi_select}
            selected={(selections.chosen[index] ?? []).includes(option.label)}
            onSelect={() => onChoose(index, option.label)}
          />
        ))}
        <OtherAnswerRow
          multiSelect={question.multi_select}
          text={selections.other[index] ?? ''}
          onChangeText={text =>
            setSelections(
              setOther(selections, index, text, question.multi_select),
            )
          }
        />
      </div>
    </fieldset>
  );
}

function OtherAnswerRow({
  multiSelect,
  text,
  onChangeText,
}: {
  multiSelect: boolean;
  text: string;
  onChangeText: (text: string) => void;
}) {
  return (
    <div className={OTHER_ROW_CLASSES}>
      <AnswerMarker
        multiSelect={multiSelect}
        selected={text.trim().length > 0}
      />
      <TextField
        aria-label={OTHER_LABEL}
        placeholder={OTHER_PLACEHOLDER}
        value={text}
        onChange={event => onChangeText(event.target.value)}
        // Swallow Enter inside this nested field; only the head's send
        // control commits an answer.
        onKeyDown={event => {
          if (event.key === 'Enter') event.preventDefault();
        }}
      />
    </div>
  );
}

function AnswerMarker({
  multiSelect,
  selected,
}: {
  multiSelect: boolean;
  selected: boolean;
}) {
  if (!multiSelect) {
    return (
      <span
        aria-hidden="true"
        className={joinClasses(
          OPTION_MARKER_CLASSES,
          selected && OPTION_MARKER_SELECTED_CLASSES,
        )}
      />
    );
  }
  return (
    <span
      aria-hidden="true"
      className={joinClasses(
        'mt-px grid size-[1.28rem] place-items-center rounded-md border-2 border-cosci-option-marker',
        selected && 'border-cosci-option-marker-on bg-cosci-option-marker-on',
      )}
    >
      {selected && (
        <Icon name="check" className="size-[0.95rem] text-cosci-composer-bg" />
      )}
    </span>
  );
}

interface AnswerRowProps {
  label: string;
  description: string;
  recommended: boolean;
  multiSelect: boolean;
  selected: boolean;
  onSelect: () => void;
}

// Clicks can unpick a selected radio; a radio input does not fire change for
// that.
function AnswerRow(props: AnswerRowProps) {
  const {label, description, recommended, multiSelect, selected, onSelect} =
    props;
  return (
    <label
      className={joinClasses(QUESTION_OPTION_ROW_CLASSES, 'cursor-pointer')}
    >
      <input
        type={multiSelect ? 'checkbox' : 'radio'}
        className="absolute pointer-events-none opacity-0"
        checked={selected}
        onChange={() => undefined}
        onClick={onSelect}
      />
      <AnswerMarker multiSelect={multiSelect} selected={selected} />
      {/* The description always takes its own line under the label. */}
      <span className="grid min-w-0 gap-0.5">
        <span className="flex min-w-0 flex-wrap items-center gap-x-2 gap-y-0.5">
          <strong className="min-w-0 text-base leading-[1.2] font-bold">
            <MarkdownMessage content={label} inline />
          </strong>
          {recommended && (
            <Chip tone="accent" size="xs">
              Recommended
            </Chip>
          )}
        </span>
        {description && (
          <small className="min-w-0 text-[0.92rem] leading-[1.3] text-cosci-muted">
            <MarkdownMessage content={description} inline />
          </small>
        )}
      </span>
    </label>
  );
}
