export const TABS = ['details', 'learning', 'overview', 'ideas'] as const;

export type TabName = (typeof TABS)[number];

// Legacy aliases preserve existing tab links.
const TAB_ALIASES: Record<string, TabName> = {
  specifications: 'details',
  specs: 'details',
  knowledge: 'learning',
  evidence: 'learning',
  summary: 'overview',
  report: 'overview',
  hypotheses: 'ideas',
};

export function normalizeTab(tab: string | undefined): TabName {
  if (!tab) return 'details';
  if ((TABS as readonly string[]).includes(tab)) return tab as TabName;
  return TAB_ALIASES[tab] ?? 'details';
}

// All tab links share one required-param route so switching tabs cannot remount
// RunDetail.
export function tabPath(id: string, tab: string | undefined): string {
  return `/runs/${id}/${normalizeTab(tab)}`;
}
