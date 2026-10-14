import {
  useEffect,
  useRef,
  type FormEventHandler,
  type ReactNode,
  type RefObject,
} from 'react';
import {createPortal} from 'react-dom';
import {
  useBackgroundInert,
  useEscapeKey,
  useFocusTrap,
  useRestoreFocusOnClose,
} from '@/shared/hooks/focus';
import {joinClasses} from './cx';
import {presenceProps, usePresence} from './use_presence';

type DialogSize = 'md' | 'lg';

const SCRIM_CLASSES = 'ui-motion-fade fixed inset-0 z-dialog-scrim bg-scrim';

// Centred with translate so the motion can scale without fighting it. The
// panel background equals the default hover tone in dark mode, so the panel
// re-points the hover tokens.
const PANEL_CLASSES =
  '[--button-outlined-hover:var(--cosci-menu-row-hover)] ' +
  '[--icon-button-hover-bg:var(--cosci-menu-row-hover)] ui-motion-pop fixed top-1/2 left-1/2 z-dialog flex -translate-1/2 flex-col ' +
  'rounded-[1.75rem] bg-cosci-menu-bg text-cosci-fg shadow-overlay ' +
  'outline-none phone:rounded-[1.25rem]';

const SIZE_CLASSES: Record<DialogSize, string> = {
  md: 'max-h-[calc(100dvh-2rem)] w-[min(32rem,calc(100vw-2rem))] gap-5 overflow-y-auto p-6',
  lg:
    'h-[min(34rem,calc(100dvh-3rem))] w-[min(52rem,calc(100vw-2rem))] px-7 py-6 ' +
    'phone:h-[calc(100dvh-1.5rem)] phone:w-[calc(100vw-1.5rem)] ' +
    'phone:px-4 phone:py-4',
};

export const DIALOG_TITLE_CLASSES =
  'm-0 font-gsans text-[1.375rem] font-medium tracking-[-0.01em]';

interface DialogProps {
  open: boolean;
  onClose: () => void;
  label: string;
  size?: DialogSize;
  // Receives focus on open; the panel itself is focused otherwise.
  initialFocusRef?: RefObject<HTMLElement | null>;
  // A dialog that is one form submits through the panel element.
  onSubmit?: FormEventHandler<HTMLFormElement>;
  children: ReactNode;
}

// Focus, inert background and Escape end the moment `open` turns false; only
// the fade-out lingers, inert and hidden from assistive technology.
export function Dialog({open, ...props}: DialogProps) {
  const {mounted, state} = usePresence(open);
  if (!mounted) return null;
  return createPortal(
    <DialogFrame {...props} open={open} state={state} />,
    document.body,
  );
}

function DialogFrame({
  open,
  state,
  onClose,
  label,
  size = 'md',
  initialFocusRef,
  onSubmit,
  children,
}: Omit<DialogProps, 'open'> & {
  open: boolean;
  state: 'open' | 'closed';
}) {
  const rootRef = useRef<HTMLDivElement>(null);
  const panelRef = useRef<HTMLElement>(null);
  const panelProps = {
    className: joinClasses(PANEL_CLASSES, SIZE_CLASSES[size]),
    role: 'dialog',
    'aria-modal': true,
    'aria-label': label,
    tabIndex: -1,
    'data-motion': 'long',
    ...presenceProps(state),
  } as const;
  return (
    <div ref={rootRef}>
      {open && (
        <DialogBehavior
          rootRef={rootRef}
          panelRef={panelRef}
          initialFocusRef={initialFocusRef}
          onClose={onClose}
        />
      )}
      <div
        className={SCRIM_CLASSES}
        data-motion="long"
        {...presenceProps(state)}
        aria-hidden="true"
        onClick={onClose}
      />
      {onSubmit ? (
        <form
          ref={panelRef as RefObject<HTMLFormElement | null>}
          noValidate
          onSubmit={onSubmit}
          {...panelProps}
        >
          {children}
        </form>
      ) : (
        <div ref={panelRef as RefObject<HTMLDivElement | null>} {...panelProps}>
          {children}
        </div>
      )}
    </div>
  );
}

function DialogBehavior({
  rootRef,
  panelRef,
  initialFocusRef,
  onClose,
}: {
  rootRef: RefObject<HTMLDivElement | null>;
  panelRef: RefObject<HTMLElement | null>;
  initialFocusRef?: RefObject<HTMLElement | null>;
  onClose: () => void;
}) {
  // Capture the opener before the focus effect below moves focus.
  useRestoreFocusOnClose();
  useFocusTrap(rootRef);
  useBackgroundInert(rootRef);
  useEscapeKey(onClose, true);
  useEffect(() => {
    (initialFocusRef?.current ?? panelRef.current)?.focus();
  }, [initialFocusRef, panelRef]);
  return null;
}
