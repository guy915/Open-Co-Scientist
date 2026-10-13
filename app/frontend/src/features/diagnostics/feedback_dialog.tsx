import {
  useEffect,
  useRef,
  useState,
  type FormEvent,
  type RefObject,
} from 'react';
import {useLocation} from 'react-router-dom';
import {
  FEEDBACK_CATEGORIES,
  submitFeedback,
  type FeedbackCategory,
} from '@/shared/api/feedback';
import {joinClasses, SETTINGS_FIELD_LABEL_CLASSES} from '@/shared/ui/classes';
import {
  Button,
  Dialog,
  DIALOG_TITLE_CLASSES,
  Select,
  TextArea,
  StatusText,
} from '@/shared/ui';

import {sessionDiagnosticExport} from './diagnostics';
import {routeIds} from '@/shared/lib/routes';

export function FeedbackControl({runId}: {runId?: string}) {
  const [open, setOpen] = useState(false);
  const messageRef = useRef<HTMLTextAreaElement>(null);
  const {pathname} = useLocation();
  const currentRunId = runId ?? routeIds(pathname).runId;
  const close = () => setOpen(false);
  return (
    <>
      <Button
        variant="tonal"
        size="sm"
        icon="stars"
        tooltip="Send feedback"
        tooltipPlacement="bottom"
        aria-label="Feedback"
        aria-haspopup="dialog"
        aria-expanded={open}
        layoutClassName="ucs-feedback-control"
        onClick={() => setOpen(current => !current)}
      >
        <span className="phone:hidden">Feedback</span>
      </Button>
      <Dialog
        open={open}
        onClose={close}
        label="Feedback"
        initialFocusRef={messageRef}
      >
        <FeedbackForm
          open={open}
          runId={currentRunId}
          messageRef={messageRef}
          onClose={close}
        />
      </Dialog>
    </>
  );
}

// Remounts with each opening, so a new report starts empty.
function FeedbackForm({
  open,
  runId,
  messageRef,
  onClose,
}: {
  open: boolean;
  runId?: string;
  messageRef: RefObject<HTMLTextAreaElement | null>;
  onClose: () => void;
}) {
  // A closed dialog lingers for its exit motion; a submission that resolves
  // after the user closed it must still be dropped.
  const live = useRef(open);
  live.current = open;
  const [category, setCategory] = useState<FeedbackCategory>('Bug');
  const [message, setMessage] = useState('');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  useEffect(
    () => () => {
      live.current = false;
    },
    [],
  );

  async function submit(event: FormEvent) {
    event.preventDefault();
    if (busy || !message.trim()) return;
    setBusy(true);
    setError('');
    try {
      const diagnostics = await sessionDiagnosticExport();
      if (!live.current) return;
      await submitFeedback({
        category,
        message: message.trim(),
        diagnostics,
        url: window.location.href.slice(0, 2048),
        ...(runId ? {run_id: runId} : {}),
      });
      if (live.current) onClose();
    } catch {
      if (live.current) {
        setError(
          'Feedback could not be sent. Please wait a minute and try again.',
        );
        setBusy(false);
      }
    }
  }

  return (
    <form
      className="grid gap-5"
      noValidate
      onSubmit={event => void submit(event)}
    >
      <h2 className={DIALOG_TITLE_CLASSES}>Feedback</h2>
      <div>
        <label
          id="feedback-category-label"
          className={joinClasses('grid gap-2', SETTINGS_FIELD_LABEL_CLASSES)}
          htmlFor="feedback-category"
        >
          Category
        </label>
        <Select
          value={category}
          options={FEEDBACK_CATEGORIES}
          optionLabel={option => option}
          name="Category"
          triggerId="feedback-category"
          labelId="feedback-category-label"
          disabled={busy}
          onChange={setCategory}
        />
      </div>
      <label
        className={joinClasses('grid gap-2', SETTINGS_FIELD_LABEL_CLASSES)}
      >
        Message
        <TextArea
          ref={messageRef}
          layoutClassName="min-h-32 resize-y"
          rows={6}
          maxLength={8000}
          value={message}
          onChange={event => setMessage(event.target.value)}
          required
          disabled={busy}
        />
      </label>
      {error && <StatusText tone="danger">{error}</StatusText>}
      <div className="flex gap-3 [justify-content:end]">
        <Button variant="outlined" onClick={onClose}>
          Cancel
        </Button>
        <Button type="submit" disabled={busy || !message.trim()}>
          {busy ? 'Submitting…' : 'Submit'}
        </Button>
      </div>
    </form>
  );
}
