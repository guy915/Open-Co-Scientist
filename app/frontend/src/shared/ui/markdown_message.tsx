import {lazy, Suspense} from 'react';
import {Button} from '@/shared/ui';
import {ErrorBoundary} from './error_boundary';

const MarkdownRenderer = lazy(() =>
  import('./markdown_message_renderer').then(module => ({
    default: module.MarkdownMessageRenderer,
  })),
);

function PlainText({
  content,
  className,
  unavailable = false,
}: {
  content: string;
  className: string;
  unavailable?: boolean;
}) {
  return (
    <div className={`min-w-0 break-words ${className}`}>
      <div className="whitespace-pre-wrap" aria-busy={!unavailable}>
        {content}
      </div>
      {unavailable && (
        <p role="status" className="mt-2 text-xs text-cosci-muted">
          Formatting unavailable.{' '}
          <Button variant="link" onClick={() => window.location.reload()}>
            Reload page
          </Button>
        </p>
      )}
    </div>
  );
}

// Keep heavy parsing/highlighting behind the lazy boundary; pending model prose
// is React-escaped plain text.
export function MarkdownMessage({
  content,
  className = '',
}: {
  content: string;
  className?: string;
}) {
  return (
    <ErrorBoundary
      fallback={
        <PlainText content={content} className={className} unavailable />
      }
    >
      <Suspense
        fallback={<PlainText content={content} className={className} />}
      >
        <MarkdownRenderer content={content} className={className} />
      </Suspense>
    </ErrorBoundary>
  );
}
