import {conciseTitle, titleCase} from './text';

const UNTITLED_SESSION = 'Untitled session';

// The one session title every surface shows: a stored title wins once
// trimmed, otherwise each surface shortens the goal its own way (the report
// header deliberately shows the whole goal), and nothing at all reads
// "Untitled session".
export function displayTitle(
  title: string | null | undefined,
  goal: string | null | undefined,
  shorten: (goal: string) => string = conciseTitle,
): string {
  const goalText = (goal ?? '').trim();
  const text = title?.trim() || (goalText ? shorten(goalText).trim() : '');
  return text ? titleCase(text) : UNTITLED_SESSION;
}
