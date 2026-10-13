import {
  type Interview,
  type InterviewQuestion,
  type InterviewTurn,
} from '@/shared/api/runs';
import {useState} from 'react';
import {Icon} from '@/shared/ui/icon';
import {Button, IconButton, TextField} from '@/shared/ui';
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
// reset selections and dismissal. Only the options scroll, and only past a
// height a typical question fits within on desktop (dvh follows the Safari
// toolbar on phones); the head stays outside the scroller so its tooltips
// cannot widen it into a horizontal scrollbar.
export function QuestionChooser({questions, onAnswer}: QuestionChooserProps) {
  const [selections, setSelections] = useState(emptySelections);
  const [minimized, setMinimized] = useState(false);
  const [dismissed, setDismissed] = useState(false);
  if (dismissed) return null;

  const answer = answerText(questions, selections);
  const sendAnswer = () => onAnswer(answer);
  const choose = (index: number, label: string) => {
    setSelections(
      toggleOption(selections, index, label, questions[index].multi_select),
    );
  };

  return (
    <section
      className="mb-2.5 grid gap-2.5 border-b border-cosci-composer-border pb-2.5"
      aria-label="Answer options"
    >
      <ChooserHead
        label={headLabel(questions)}
        answered={answer.length > 0}
        onSend={sendAnswer}
        minimized={minimized}
        onToggleMinimize={() => setMinimized(open => !open)}
        onDismiss={() => setDismissed(true)}
      />
      {!minimized && (
        <div className="grid max-h-[min(18rem,40dvh)] min-w-0 gap-2.5 overflow-x-hidden overflow-y-auto overscroll-contain above-phone:max-h-[min(36rem,70dvh)]">
          <ChooserBody
            questions={questions}
            selections={selections}
            setSelections={setSelections}
            onChoose={choose}
          />
        </div>
      )}
    </section>
  );
}

function headLabel(questions: InterviewQuestion[]): string {
  return questions
    .map(question => question.header || question.question)
    .join(' · ');
}

// One send commits every question's answer, so it sits in the head rather
// than beside any single question's field.
function ChooserHead({
  label,
  answered,
  onSend,
  minimized,
  onToggleMinimize,
  onDismiss,
}: {
  label: string;
  answered: boolean;
  onSend: () => void;
  minimized: boolean;
  onToggleMinimize: () => void;
  onDismiss: () => void;
}) {
  return (
    <div className="flex min-w-0 items-center justify-between gap-2.5">
      <span className="min-w-0 truncate text-[0.82rem] font-medium tracking-[0.04em] uppercase text-cosci-muted">
        {label}
      </span>
      <div className="flex shrink-0 items-center gap-0.5">
        <Button
          size="sm"
          icon="send"
          aria-label="Send answer"
          layoutClassName="mr-1.5"
          disabled={!answered}
          onClick={onSend}
        >
          Send
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

interface ChooserBodyProps {
  questions: InterviewQuestion[];
  selections: QuestionSelections;
  setSelections: (selections: QuestionSelections) => void;
  onChoose: (index: number, label: string) => void;
}

function ChooserBody(props: ChooserBodyProps) {
  const {questions, selections, setSelections, onChoose} = props;
  return (
    <>
      {questions.map((question, index) => (
        <QuestionGroup
          key={question.question}
          question={question}
          index={index}
          selections={selections}
          setSelections={setSelections}
          onChoose={onChoose}
        />
      ))}
    </>
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
  multiSelect: boolean;
  selected: boolean;
  onSelect: () => void;
}

// Clicks can unpick a selected radio; a radio input does not fire change for
// that.
function AnswerRow(props: AnswerRowProps) {
  const {label, description, multiSelect, selected, onSelect} = props;
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
        <strong className="min-w-0 text-base leading-[1.2] font-bold">
          <MarkdownMessage content={label} inline />
        </strong>
        {description && (
          <small className="min-w-0 text-[0.92rem] leading-[1.3] text-cosci-muted">
            <MarkdownMessage content={description} inline />
          </small>
        )}
      </span>
    </label>
  );
}
