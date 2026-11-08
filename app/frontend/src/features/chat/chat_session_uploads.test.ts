import {expect, it, vi} from 'vitest';
import type {FormEvent} from 'react';
import {createInterview, stageDocument} from '@/shared/api/runs';
import {buildChatHandlers} from './chat_session_handlers';
import {sessionRuntime, stagedDocument} from './session_test_helpers';

vi.mock('@/shared/api/runs', async importActual => ({
  ...(await importActual<typeof import('@/shared/api/runs')>()),
  stageDocument: vi.fn(),
  createInterview: vi.fn(),
}));

it('stages attached files one at a time', async () => {
  let active = 0;
  let peak = 0;
  vi.mocked(stageDocument).mockImplementation(async file => {
    active += 1;
    peak = Math.max(peak, active);
    await new Promise(resolve => setTimeout(resolve, 5));
    active -= 1;
    return stagedDocument(file.name);
  });
  vi.mocked(createInterview).mockResolvedValue({
    id: 'chat-1',
    turns: [],
  } as unknown as Awaited<ReturnType<typeof createInterview>>);
  const runtime = sessionRuntime({input: 'goal'});
  const files = ['a.txt', 'b.txt', 'c.txt'].map(name => new File(['x'], name));
  await buildChatHandlers(runtime).handleSubmit(
    {preventDefault: vi.fn()} as unknown as FormEvent<HTMLFormElement>,
    files,
  );
  expect(peak).toBe(1);
  expect(vi.mocked(createInterview).mock.calls[0][2]).toEqual([
    'a.txt',
    'b.txt',
    'c.txt',
  ]);
});
