// highlight.js ships no typings for its per-language entry points.
declare module 'highlight.js/lib/languages/*' {
  import type {LanguageFn} from 'highlight.js';
  const language: LanguageFn;
  export default language;
}
