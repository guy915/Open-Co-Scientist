import {
  createContext,
  type ReactNode,
  useCallback,
  useContext,
  useLayoutEffect,
  useMemo,
  useState,
} from 'react';
import {flushSync} from 'react-dom';
import {useMediaQuery} from './use_media_query';
import {MD3_SCHEMES} from '@/shared/ui/md3_scheme';
import {
  readStorage,
  STORAGE_KEYS,
  writeStorage,
} from '@/shared/lib/safe_storage';

export type Mode = 'system' | 'light' | 'dark';
type ResolvedMode = 'light' | 'dark';

interface ThemeContextValue {
  mode: Mode;
  resolvedMode: ResolvedMode;
  setMode: (m: Mode) => void;
  toggle: () => void;
}

const ThemeContext = createContext<ThemeContextValue | null>(null);

function readStoredMode(): Mode {
  if (typeof window === 'undefined') return 'system';
  const stored = readStorage('local', STORAGE_KEYS.theme);
  return stored === 'light' || stored === 'dark' || stored === 'system'
    ? stored
    : 'system';
}

// Without matchMedia in jsdom/prerender, use the app’s dark reference theme.
const DARK_QUERY = '(prefers-color-scheme: dark)';

function useSystemColorScheme(): ResolvedMode {
  return useMediaQuery(DARK_QUERY, true) ? 'dark' : 'light';
}

// Apply before paint to avoid a stale-theme flash; persist preference, not
// resolved brightness, so system mode survives reload.
function useApplyTheme(mode: Mode, resolvedMode: ResolvedMode): void {
  useLayoutEffect(() => {
    const root = document.documentElement;
    root.classList.add('theme-switching');
    applyMd3Theme(resolvedMode === 'dark');
    root.dataset.theme = resolvedMode;
    root.dataset.themePreference = mode;
    root.classList.toggle('dark', resolvedMode === 'dark');
    writeStorage('local', STORAGE_KEYS.theme, mode);
    // The first animation frame commits styles; the second lifts transition
    // suppression after they are visible.
    const id = window.requestAnimationFrame(() => {
      window.requestAnimationFrame(() => {
        root.classList.remove('theme-switching');
      });
    });
    return () => window.cancelAnimationFrame(id);
  }, [mode, resolvedMode]);
}

// A chosen theme cross-fades the whole page (shared/ui/motion.css) instead of
// repainting at once. The update commits synchronously inside the transition so
// the new snapshot already carries the new palette.
function crossFade(update: () => void): void {
  if (typeof document.startViewTransition !== 'function') {
    update();
    return;
  }
  document.startViewTransition(() => flushSync(update));
}

export function ThemeProvider({children}: {children: ReactNode}) {
  const [mode, setModeState] = useState<Mode>(readStoredMode);
  const systemMode = useSystemColorScheme();
  const resolvedMode = mode === 'system' ? systemMode : mode;

  useApplyTheme(mode, resolvedMode);

  const setMode = useCallback((m: Mode) => {
    crossFade(() => setModeState(m));
  }, []);

  const toggle = useCallback(() => {
    crossFade(() =>
      setModeState(current => (current === 'dark' ? 'light' : 'dark')),
    );
  }, []);

  const value = useMemo(
    () => ({mode, resolvedMode, setMode, toggle}),
    [mode, resolvedMode, setMode, toggle],
  );

  return (
    <ThemeContext.Provider value={value}>{children}</ThemeContext.Provider>
  );
}

export function useTheme(): ThemeContextValue {
  const ctx = useContext(ThemeContext);
  if (!ctx) throw new Error('useTheme used outside ThemeProvider');
  return ctx;
}

function applyMd3Theme(dark: boolean): void {
  const style = document.documentElement.style;
  for (const [token, color] of Object.entries(
    MD3_SCHEMES[dark ? 'dark' : 'light'],
  )) {
    style.setProperty(token, color);
  }
}
