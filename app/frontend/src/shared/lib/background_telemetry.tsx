import {useEffect, useRef} from 'react';
import {afterFirstView} from './after_first_view';

const dsn = import.meta.env.VITE_SENTRY_DSN as string | undefined;
const beaconToken = import.meta.env.VITE_CF_WEB_ANALYTICS_TOKEN as
  string | undefined;

// Cookieless page-view counting; `_headers` allows exactly this origin.
export const WEB_ANALYTICS_SRC =
  'https://static.cloudflareinsights.com/beacon.min.js';

function loadWebAnalytics(token: string) {
  const script = document.createElement('script');
  script.defer = true;
  script.src = WEB_ANALYTICS_SRC;
  script.dataset.cfBeacon = JSON.stringify({token, spa: true});
  document.head.append(script);
}

export function BackgroundTelemetry() {
  const requested = useRef(false);
  useEffect(() => {
    if ((!dsn && !beaconToken) || requested.current) return;
    requested.current = true;
    afterFirstView(() => {
      if (beaconToken) loadWebAnalytics(beaconToken);
      if (dsn) {
        void import('./error_tracking').then(({initErrorTracking}) =>
          initErrorTracking(dsn),
        );
      }
    });
  }, []);
  return null;
}
