import {useState, type ReactNode} from 'react';
import {createPortal} from 'react-dom';
import {joinClasses} from './cx';
import {presenceProps, usePresence} from './use_presence';

type ToastTone = 'neutral' | 'danger';
type ToastPlacement = 'start' | 'end';

// `reference-toast` scopes the toast's link-button colour (tokens.css).
const TONE_CLASSES: Record<ToastTone, string> = {
  neutral:
    'reference-toast bg-cosci-toast-bg py-3 text-[0.92rem] font-medium text-cosci-toast-fg',
  danger:
    'border border-cosci-danger-border bg-cosci-danger-bg py-3 text-cosci-danger-fg',
};

const PLACEMENT_CLASSES: Record<ToastPlacement, string> = {
  start: 'left-4',
  end: 'right-4',
};

// A transient status at the bottom of the window. It portals above every
// stacking context, rises in, and keeps its last content while it fades out
// after `children` becomes null.
export function Toast({
  tone = 'neutral',
  placement = 'start',
  children,
}: {
  tone?: ToastTone;
  placement?: ToastPlacement;
  children: ReactNode | null;
}) {
  const open =
    children !== null && children !== undefined && children !== false;
  const [last, setLast] = useState<ReactNode>(children);
  if (open && children !== last) setLast(children);
  const {mounted, state} = usePresence(open);
  if (!mounted) return null;
  return createPortal(
    <div
      role="status"
      {...presenceProps(state)}
      className={joinClasses(
        'ui-motion-rise fixed bottom-4 z-toast flex items-center gap-4 rounded-xl px-4 shadow-overlay',
        TONE_CLASSES[tone],
        PLACEMENT_CLASSES[placement],
      )}
    >
      {open ? children : last}
    </div>,
    document.body,
  );
}
