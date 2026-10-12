import {beforeEach, describe, expect, it, vi} from 'vitest';
import type {SessionState} from './use_chat_session';
import {sessionRuntime, stagedDocument} from './session_test_helpers';
import {cancelRun, createRun, getRun, startRun} from '@/shared/api/runs';
import {
  readPendingCreateIntent,
  promoteDraftToRun,
} from './chat_session_start_run';
import type {InferredRunSpec} from '@/shared/lib/run_spec';

vi.mock('@/shared/api/runs', async importActual => ({
  ...(await importActual<typeof import('@/shared/api/runs')>()),
  createRun: vi.fn(),
  startRun: vi.fn(),
  cancelRun: vi.fn(),
  getRun: vi.fn(),
  announceRunStart: vi.fn(async () => null),
}));

const SPEC: InferredRunSpec = {
  goal: 'g',
  interviewId: 'chat-1',
  requirements: [],
  attributes: [],
  criteria: [],
  focus: 'balance',
  tier: 'standard',
};

function deps(state: Partial<SessionState> = {}) {
  return sessionRuntime(
    {draft: {spec: SPEC, createdAt: 0}, ...state},
    {pubmedEnabled: false, webSearchEnabled: false},
  );
}

function runRecord(
  id: string,
  status: string,
  goal = 'g',
  setup: Record<string, unknown> = {},
) {
  return {
    id,
    status,
    research_goal: goal,
    config: {
      setup: {
        goal,
        requirements: [],
        attributes: [],
        criteria: [],
        focus: 'balance',
        tier: 'standard',
        ...setup,
      },
    },
  } as unknown as Awaited<ReturnType<typeof getRun>>;
}

function createKeys() {
  return vi
    .mocked(createRun)
    .mock.calls.map(([, options]) => options?.idempotencyKey);
}

describe('start run', () => {
  beforeEach(() => {
    window.localStorage.clear();
    window.sessionStorage.clear();
    vi.resetAllMocks();
    vi.mocked(createRun).mockResolvedValue(runRecord('r1', 'draft') as never);
    vi.mocked(startRun).mockResolvedValue({id: 'r1', status: 'queued'});
    vi.mocked(cancelRun).mockImplementation(async id => ({
      id,
      status: 'cancelled',
    }));
    vi.mocked(getRun).mockResolvedValue(runRecord('r1', 'draft'));
  });

  it('sends the confirmed plan and staged documents under one idempotency key, then clears it', async () => {
    await promoteDraftToRun(deps({pendingAttachments: [stagedDocument('d1')]}));

    const [payload, options] = vi.mocked(createRun).mock.calls[0];
    expect(payload).toStrictEqual({
      research_goal: 'g',
      interview_id: 'chat-1',
      requirements: [],
      attributes: [],
      criteria: [],
      focus: 'balance',
      tier: 'standard',
      notify_on_completion: false,
      enable_literature_review: false,
      enable_web_search: false,
      document_ids: ['d1'],
    });
    expect(options).toEqual({
      idempotencyKey: expect.stringMatching(/^run-create-/),
    });
    expect(await readPendingCreateIntent('chat-1')).toBeUndefined();
  });

  it('resolves a lost create before starting a manually edited visible plan', async () => {
    vi.mocked(createRun)
      .mockRejectedValueOnce(new TypeError('Failed to fetch'))
      .mockResolvedValueOnce(runRecord('r1', 'draft') as never)
      .mockResolvedValueOnce(runRecord('r2', 'draft') as never);
    const session = deps();

    await promoteDraftToRun(session);
    session.update({
      draft: {spec: {...SPEC, goal: 'edited visible plan'}, createdAt: 10},
      pendingAttachments: [stagedDocument('changed-doc')],
    });
    session.services.pubmedEnabled = true;
    session.services.webSearchEnabled = true;
    await promoteDraftToRun(session);

    const [first, replay, edited] = vi.mocked(createRun).mock.calls;
    const keys = createKeys();
    expect(replay[0]).toEqual(first[0]);
    expect(keys[1]).toBe(keys[0]);
    expect(edited[0]).toMatchObject({
      research_goal: 'edited visible plan',
      enable_literature_review: true,
      enable_web_search: true,
      document_ids: ['changed-doc'],
    });
    expect(keys[2]).toBeTruthy();
    expect(keys[2]).not.toBe(keys[0]);
    expect(cancelRun).toHaveBeenCalledWith('r1');
    expect(startRun).toHaveBeenCalledTimes(1);
    expect(startRun).toHaveBeenCalledWith('r2');
    expect(session.state.startedSession?.id).toBe('r2');
  });

  it('retires a create intent rejected before commit so an edited draft gets a fresh key', async () => {
    vi.mocked(createRun)
      .mockRejectedValueOnce(
        Object.assign(new Error('unprocessable request'), {status: 422}),
      )
      .mockResolvedValueOnce(runRecord('r2', 'draft') as never);
    const session = deps();

    await promoteDraftToRun(session);
    expect(await readPendingCreateIntent('chat-1')).toBeUndefined();
    session.update({
      draft: {spec: {...SPEC, goal: 'corrected plan'}, createdAt: 10},
    });
    await promoteDraftToRun(session);

    const keys = createKeys();
    expect(keys[1]).not.toBe(keys[0]);
    expect(startRun).toHaveBeenCalledWith('r2');
  });

  it.each(['failed', 'blocked'] as const)(
    'surfaces a %s receipt without presenting it as a started session',
    async status => {
      vi.mocked(createRun).mockResolvedValueOnce(
        runRecord('r1', status) as never,
      );
      vi.mocked(getRun).mockResolvedValue(runRecord('r1', status));
      const session = deps();
      const stage = session.state.draft;

      await promoteDraftToRun(session);
      await promoteDraftToRun(session);

      expect(createRun).toHaveBeenCalledTimes(1);
      expect(startRun).not.toHaveBeenCalled();
      expect(session.state.startedSession).toBeNull();
      expect(session.state.draft).toBe(stage);
      expect(session.state.error).toContain(`existing run is ${status}`);
      expect(await readPendingCreateIntent('chat-1')).toMatchObject({
        createdRunId: 'r1',
      });
    },
  );

  it('keeps an unresolved draft linked and retries that same run only after another click', async () => {
    vi.mocked(startRun).mockRejectedValueOnce(new TypeError('connection lost'));
    vi.mocked(cancelRun).mockRejectedValueOnce(new TypeError('cancel lost'));
    const session = deps();
    const stage = session.state.draft;

    await promoteDraftToRun(session);
    expect(await readPendingCreateIntent('chat-1')).toMatchObject({
      createdRunId: 'r1',
    });
    expect(session.state.draft).toBe(stage);

    await promoteDraftToRun(session);

    expect(createRun).toHaveBeenCalledTimes(1);
    expect(startRun).toHaveBeenCalledTimes(2);
    expect(await readPendingCreateIntent('chat-1')).toBeUndefined();
  });
});
