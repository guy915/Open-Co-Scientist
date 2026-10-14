import type {ChatEntry} from './chat_timeline_bubble';

export function makeMessage(over: Partial<ChatEntry> = {}): ChatEntry {
  return {
    id: 'm1',
    role: 'user',
    content: 'Hello',
    created_at: 1,
    ...over,
  };
}
