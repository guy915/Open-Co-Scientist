import {useEffect, useRef} from 'react';
import {afterFirstView} from './after_first_view';

const dsn = import.meta.env.VITE_SENTRY_DSN as string | undefined;

export function BackgroundTelemetry() {
  const requested = useRef(false);
  useEffect(() => {
    if (!dsn || requested.current) return;
    requested.current = true;
    afterFirstView(() => {
      void import('./error_tracking').then(({initErrorTracking}) =>
        initErrorTracking(dsn),
      );
    });
  }, []);
  return null;
}
