import {StrictMode} from 'react';
import {createRoot} from 'react-dom/client';
import {BrowserRouter} from 'react-router-dom';
import './index.css';
import './styles/tokens.css';
import './styles/shell_surface.css';
import './styles/home_surface.css';
import './styles/home_landing.css';
import './styles/tooltips.css';
import './styles/boot_skeleton.css';
import './shared/ui/motion.css';
import {
  installDiagnosticLogging,
  installUiErrorLogging,
  installUiInteractionLogging,
} from '@/shared/lib/ui_logging';
import {WorkbenchApp} from '@/app/workbench_app';
import {BackgroundTelemetry} from '@/shared/lib/background_telemetry';
import {
  readStorage,
  STORAGE_KEYS,
  writeStorage,
} from '@/shared/lib/safe_storage';

// Install error capture before mounting so first-render crashes survive outside
// the browser console.
installUiErrorLogging();
installUiInteractionLogging();
installDiagnosticLogging();

// A deploy deletes the old hashed chunks; reload once to fetch the new index.
// The timestamp guard stops a reload loop if the chunk is genuinely missing.
window.addEventListener('vite:preloadError', event => {
  const key = STORAGE_KEYS.chunkReloadAt;
  const last = Number(readStorage('session', key));
  if (Date.now() - last < 10_000) return;
  // Without storage there is no loop guard, so do not reload at all.
  if (!writeStorage('session', key, String(Date.now()))) return;
  event.preventDefault();
  window.location.reload();
});

const rootElement = document.getElementById('root');
if (!rootElement) {
  throw new Error('Missing #root element in index.html.');
}

createRoot(rootElement).render(
  <StrictMode>
    <BrowserRouter>
      <WorkbenchApp />
      <BackgroundTelemetry />
    </BrowserRouter>
  </StrictMode>,
);
