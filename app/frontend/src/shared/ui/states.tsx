import type {ReactNode} from 'react';
import {Card} from './card';
import {joinClasses} from './cx';

type StatusTone = 'muted' | 'danger';

// A danger tone is announced as an alert; anything else waits politely.
export function StatusText({
  tone = 'muted',
  as: Tag = 'p',
  layoutClassName,
  children,
}: {
  tone?: StatusTone;
  as?: 'p' | 'div';
  layoutClassName?: string;
  children: ReactNode;
}) {
  return (
    <Tag
      role={tone === 'danger' ? 'alert' : 'status'}
      className={joinClasses(
        'ui-motion-enter m-0 text-sm',
        tone === 'danger' ? 'text-cosci-danger-fg' : 'text-cosci-muted',
        layoutClassName,
      )}
    >
      {children}
    </Tag>
  );
}

// A failure that blocks the surface it sits on, with an optional way out.
export function ErrorNotice({
  action,
  layoutClassName,
  children,
}: {
  action?: ReactNode;
  layoutClassName?: string;
  children: ReactNode;
}) {
  return (
    <Card
      role="alert"
      tone="danger"
      layoutClassName={joinClasses('ui-motion-enter text-sm', layoutClassName)}
    >
      {children}
      {action && <div className="mt-3">{action}</div>}
    </Card>
  );
}
