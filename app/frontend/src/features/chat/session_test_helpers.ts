import {vi} from 'vitest';
import {type StagedDocument} from '@/shared/api/runs';
import {
  type ChatSessionDeps,
  type SessionState,
  type SessionUpdate,
  initialSessionState,
  sessionReducer,
} from './use_chat_session';

export function sessionRuntime(
  state: Partial<SessionState> = {},
  services: Partial<ChatSessionDeps> = {},
) {
  let current = {...initialSessionState(), ...state};
  const observed: SessionState[] = [];
  return {
    get state() {
      return current;
    },
    update: vi.fn((action: SessionUpdate) => {
      current = sessionReducer(current, action);
      observed.push(current);
    }),
    services: {
      reloadHistory: vi.fn(async () => undefined),
      onChatStarted: vi.fn(),
      focusComposer: vi.fn(),
      setToast: vi.fn(),
      pubmedEnabled: true,
      webSearchEnabled: true,
      ...services,
    },
    turnAbortRef: {current: null as AbortController | null},
    observed,
  };
}

export function stagedDocument(id: string): StagedDocument {
  return {
    id,
    title: id,
    sha256: id,
    byte_size: 1,
    mime_type: 'text/plain',
    extraction_tool: 'text',
  };
}
