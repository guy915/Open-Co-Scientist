export const HEADER_TITLE_EVENT = 'cosci-header-title';

export const NEW_CHAT_EVENT = 'cosci-new-chat';

export const RUNS_CHANGED_EVENT = 'cosci-runs-changed';

export const CHATS_CHANGED_EVENT = 'cosci-chats-changed';

// Modified clicks open elsewhere but still run React handlers; guard mutations
// so a new-tab action cannot erase this tab’s chat.
export function isModifiedClick(event: {
  metaKey: boolean;
  ctrlKey: boolean;
  shiftKey: boolean;
  altKey: boolean;
  button: number;
}): boolean {
  if (event.button !== 0) return true;
  return event.metaKey || event.ctrlKey || event.shiftKey || event.altKey;
}
