import type React from 'react';
import type {ReactNode} from 'react';
import {Component} from 'react';
import {logUiError} from '@/shared/lib/ui_logging';
import {Button} from '@/shared/ui';
import {Icon} from './icon';

interface ErrorBoundaryProps {
  children: ReactNode;
  fallback?: ReactNode;
}

interface ErrorBoundaryState {
  hasError: boolean;
  error: Error | null;
  // React supplies the component stack after the error itself.
  errorInfo: React.ErrorInfo | null;
}

function FallbackHeader() {
  return (
    <header className="p-6">
      <h1 className="flex items-center gap-2 text-lg font-semibold leading-none text-cosci-danger-fg">
        <span aria-hidden="true">
          <Icon name="warning" />
        </span>
        Something went wrong
      </h1>
      <p className="mt-2 text-sm text-th-muted-fg">
        An error occurred while rendering this component
      </p>
    </header>
  );
}

function FallbackErrorMessage({error}: {error: Error | null}) {
  return (
    <div className="rounded-lg border border-cosci-danger-border bg-cosci-danger-bg p-4">
      <p className="font-mono text-sm text-cosci-danger-fg">
        {error?.toString()}
      </p>
    </div>
  );
}

function FallbackComponentStack({
  errorInfo,
}: {
  errorInfo: React.ErrorInfo | null;
}) {
  if (!errorInfo) return null;
  return (
    <details className="text-sm">
      <summary className="mb-2 cursor-pointer font-medium">
        Component Stack
      </summary>
      <pre className="overflow-auto rounded bg-th-muted p-4 text-xs">
        {errorInfo.componentStack}
      </pre>
    </details>
  );
}

function FallbackActions({onReset}: {onReset: () => void}) {
  return (
    <div className="flex gap-2">
      <Button onClick={onReset}>Try Again</Button>
      <Button variant="outlined" onClick={() => window.location.reload()}>
        Reload Page
      </Button>
    </div>
  );
}

function ErrorFallbackCard({
  error,
  errorInfo,
  onReset,
}: {
  error: Error | null;
  errorInfo: React.ErrorInfo | null;
  onReset: () => void;
}) {
  return (
    <div className="flex min-h-screen items-center justify-center p-4">
      <section className="w-full max-w-2xl rounded-xl border border-th-border bg-th-card text-th-card-fg">
        <FallbackHeader />
        <div className="space-y-4 p-6 pt-0">
          <FallbackErrorMessage error={error} />
          <FallbackComponentStack errorInfo={errorInfo} />
          <FallbackActions onReset={onReset} />
        </div>
      </section>
    </div>
  );
}

// React has no hook error boundary; this covers descendant render/lifecycle
// errors, never handlers, async code or its own render.
export class ErrorBoundary extends Component<
  ErrorBoundaryProps,
  ErrorBoundaryState
> {
  constructor(props: ErrorBoundaryProps) {
    super(props);
    this.state = {hasError: false, error: null, errorInfo: null};
  }

  // Render-phase recovery must remain pure; logging belongs in the commit
  // phase.
  static getDerivedStateFromError(error: Error): Partial<ErrorBoundaryState> {
    return {hasError: true, error};
  }

  componentDidCatch(error: Error, errorInfo: React.ErrorInfo) {
    console.error('ErrorBoundary caught an error:', error, errorInfo);
    logUiError(
      `render error: ${String(error)}`,
      errorInfo.componentStack ?? undefined,
    );
    this.setState({errorInfo});
  }

  handleReset = () => {
    this.setState({hasError: false, error: null, errorInfo: null});
  };

  render() {
    if (this.state.hasError) {
      if (this.props.fallback) return this.props.fallback;
      return (
        <ErrorFallbackCard
          error={this.state.error}
          errorInfo={this.state.errorInfo}
          onReset={this.handleReset}
        />
      );
    }
    return this.props.children;
  }
}
