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
  inline = false,
}: {
  content: string;
  className: string;
  unavailable?: boolean;
  inline?: boolean;
}) {
  const Wrapper = inline ? 'span' : 'div';
  const Text = inline ? 'span' : 'div';
  const Notice = inline ? 'span' : 'p';
  return (
    <Wrapper className={`min-w-0 break-words ${className}`}>
      <Text className="whitespace-pre-wrap" aria-busy={!unavailable}>
        {content}
      </Text>
      {unavailable && (
        <Notice role="status" className="mt-2 text-xs text-cosci-muted">
          Formatting unavailable.{' '}
          <Button variant="link" onClick={() => window.location.reload()}>
            Reload page
          </Button>
        </Notice>
      )}
    </Wrapper>
  );
}

// Keep heavy parsing/highlighting behind the lazy boundary; pending model prose
// is React-escaped plain text.
export function MarkdownMessage({
  content,
  className = '',
  inline = false,
}: {
  content: string;
  className?: string;
  inline?: boolean;
}) {
  return (
    <ErrorBoundary
      fallback={
        <PlainText
          content={content}
          className={className}
          unavailable
          inline={inline}
        />
      }
    >
      <Suspense
        fallback={
          <PlainText content={content} className={className} inline={inline} />
        }
      >
        <MarkdownRenderer
          content={content}
          className={className}
          inline={inline}
        />
      </Suspense>
    </ErrorBoundary>
  );
}
