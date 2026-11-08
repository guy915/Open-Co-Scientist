// When a deploy has removed an old chunk, main.tsx cancels Vite's preload
// error and reloads the page. Vite then resolves the dynamic import to
// undefined instead of rejecting. Callers would crash on the missing module
// (and log that crash) in the moment before the reload, so wait for the
// reload instead.
const UNTIL_RELOAD = new Promise<never>(() => undefined);

export function loadChunk<T>(load: () => Promise<T>): Promise<T> {
  return load().then(module => (module === undefined ? UNTIL_RELOAD : module));
}
