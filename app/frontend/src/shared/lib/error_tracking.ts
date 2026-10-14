import * as Sentry from '@sentry/browser';

const ERROR_TYPES = new Set([
  'Error',
  'TypeError',
  'RangeError',
  'ReferenceError',
  'SyntaxError',
  'URIError',
  'EvalError',
  'AggregateError',
]);

function position(value: number | undefined): number | undefined {
  return value !== undefined && Number.isSafeInteger(value) && value >= 0
    ? value
    : undefined;
}

// Stack URLs and arbitrary exception strings may contain private text. Only
// scripts declared by this page can supply a filename; discard URL queries.
function bundledFilename(value: string | undefined): string | undefined {
  if (!value) return undefined;
  const scripts = Array.from(
    document.querySelectorAll<HTMLScriptElement | HTMLLinkElement>(
      'script[src], link[rel="modulepreload"][href]',
    ),
  ).map(element =>
    element instanceof HTMLScriptElement ? element.src : element.href,
  );
  try {
    const url = new URL(value, location.href);
    if (
      url.origin !== location.origin ||
      !scripts.includes(`${url.origin}${url.pathname}`)
    )
      return undefined;
    return url.pathname;
  } catch {
    return undefined;
  }
}

export function privateErrorEvent(event: Sentry.ErrorEvent): Sentry.ErrorEvent {
  return {
    type: undefined,
    event_id: /^[a-f0-9]{32}$/i.test(event.event_id ?? '')
      ? event.event_id
      : undefined,
    timestamp: Number.isFinite(event.timestamp) ? event.timestamp : undefined,
    environment: import.meta.env.MODE,
    level: 'error',
    platform: 'javascript',
    message: 'Application error',
    exception: {
      values: event.exception?.values?.slice(0, 5).map(exception => ({
        type: ERROR_TYPES.has(exception.type ?? '') ? exception.type : 'Error',
        value: 'Error details withheld for privacy',
        stacktrace: {
          frames: exception.stacktrace?.frames?.slice(-50).map(frame => ({
            filename: bundledFilename(frame.filename),
            lineno: position(frame.lineno),
            colno: position(frame.colno),
          })),
        },
      })),
    },
  };
}

export function initErrorTracking(dsn: string): void {
  Sentry.init({
    dsn,
    environment: import.meta.env.MODE,
    dataCollection: {
      userInfo: false,
      cookies: false,
      httpHeaders: false,
      httpBodies: [],
      urlQueryParams: false,
      graphQL: {document: false, variables: false},
      genAI: {inputs: false, outputs: false},
      databaseQueryData: false,
      queues: false,
      stackFrameVariables: false,
      frameContextLines: 0,
    },
    maxBreadcrumbs: 0,
    beforeBreadcrumb: () => null,
    tracesSampleRate: 0,
    beforeSendTransaction: () => null,
    beforeSend: (event, hint) => {
      // Attachments bypass event-field scrubbing and may contain diagnostics.
      hint.attachments = [];
      return privateErrorEvent(event);
    },
  });
}
