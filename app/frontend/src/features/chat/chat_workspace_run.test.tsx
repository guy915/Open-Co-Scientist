import {fireEvent, screen, waitFor} from '@testing-library/react';
import {beforeEach, expect, it, vi} from 'vitest';
import {
  ANNOUNCEMENT_TEXT,
  apiMock,
  installChatWorkspaceMocks,
  renderWorkspace,
} from './chat_workspace_test_helpers';

beforeEach(() => {
  installChatWorkspaceMocks();
});

const RESEARCH_GOAL = 'Investigate glucose homeostasis under cold stress.';

const RESPONSE_BLOB_URL = 'blob:co-scientist-response';

function submitResearchGoal(goal = RESEARCH_GOAL) {
  const input = screen.getByRole('textbox');
  fireEvent.change(input, {target: {value: goal}});
  fireEvent.submit(input.closest('form')!);
}

function installClipboardAndDownloadSpies() {
  const writeText = vi.fn().mockResolvedValue(undefined);
  Object.defineProperty(navigator, 'clipboard', {
    configurable: true,
    value: {writeText},
  });
  const createObjectURL = vi.fn((blob: Blob) => {
    expect(blob).toBeInstanceOf(Blob);
    return RESPONSE_BLOB_URL;
  });
  const revokeObjectURL = vi.fn();
  Object.defineProperty(URL, 'createObjectURL', {
    configurable: true,
    value: createObjectURL,
  });
  Object.defineProperty(URL, 'revokeObjectURL', {
    configurable: true,
    value: revokeObjectURL,
  });
  const downloadedNames: string[] = [];
  const anchorClick = vi
    .spyOn(HTMLAnchorElement.prototype, 'click')
    .mockImplementation(function (this: HTMLAnchorElement) {
      downloadedNames.push(this.download);
    });
  return {
    writeText,
    createObjectURL,
    revokeObjectURL,
    downloadedNames,
    anchorClick,
  };
}

async function driveToRunSpec() {
  renderWorkspace();
  submitResearchGoal();
  expect(
    await screen.findByText(
      /Review the research setup and select a focus and run type/,
    ),
  ).toBeInTheDocument();
}

async function startRunFromSpec({
  waitForSessionCard = true,
}: {waitForSessionCard?: boolean} = {}) {
  fireEvent.click(screen.getByLabelText(/Ultra/i));
  fireEvent.click(screen.getByText('Start research'));
  await waitFor(() => {
    expect(apiMock.createRun).toHaveBeenCalled();
    const [payload, options] = apiMock.createRun.mock.calls.at(-1)!;
    expect(payload).toStrictEqual({
      research_goal: RESEARCH_GOAL,
      interview_id: 'interview-1',
      requirements: ['Prioritize mechanistic novelty'],
      attributes: ['Cold-stress glucose regulation'],
      criteria: [],
      focus: 'balance',
      tier: 'ultra',
      notify_on_completion: false,
      enable_literature_review: true,
      enable_web_search: true,
      document_ids: [],
    });
    expect(options).toEqual({
      idempotencyKey: expect.stringMatching(/^run-create-/),
    });
    expect(apiMock.startRun).toHaveBeenCalledWith('run-1');
  });
  if (!waitForSessionCard) return;
  expect(await screen.findByText('Research session')).toBeInTheDocument();
}

it('starts the durable run on confirmation', async () => {
  await driveToRunSpec();
  await startRunFromSpec();

  expect(screen.getByTestId('location')).toHaveTextContent('/');
  expect(await screen.findByText(ANNOUNCEMENT_TEXT)).toBeInTheDocument();
  expect(apiMock.announceRunStart).toHaveBeenCalledWith(
    'run-1',
    'Start research',
    expect.anything(),
    expect.anything(),
  );
  expect(screen.getAllByText('Start research').length).toBeGreaterThan(1);
  expect(
    screen.getByRole('heading', {name: 'Research plan'}),
  ).toBeInTheDocument();
  expect(
    screen.getByRole('heading', {
      name: 'Investigate Glucose Homeostasis Under Cold Stress',
    }),
  ).toBeInTheDocument();
  expect(screen.getByRole('button', {name: 'Start research'})).toBeDisabled();
  expect(screen.getByText('Research session')).toBeInTheDocument();
  expect(screen.getByRole('link', {name: /Open/i})).toBeInTheDocument();
  expect(
    screen.getByRole('link', {name: 'View session details'}),
  ).toBeInTheDocument();
  expect(
    screen.getByRole('button', {
      name: 'Start a new research goal session on a new topic',
    }),
  ).toBeInTheDocument();
  expect(screen.queryByText('Report ready')).not.toBeInTheDocument();
  expect(
    screen.queryByText('Mitochondrial feedback hypothesis'),
  ).not.toBeInTheDocument();
});

it('keeps asking Co-Scientist through the interview and after the run starts', async () => {
  renderWorkspace();
  submitResearchGoal();
  expect(
    await screen.findByText(
      /Review the research setup and select a focus and run type/,
    ),
  ).toBeInTheDocument();
  expect(screen.getByPlaceholderText('Ask Co-Scientist')).toBeInTheDocument();

  fireEvent.click(screen.getByText('Start research'));
  expect(await screen.findByText(ANNOUNCEMENT_TEXT)).toBeInTheDocument();
  expect(screen.getByPlaceholderText('Ask Co-Scientist')).toBeInTheDocument();
  expect(
    screen.queryByPlaceholderText('Ask a question about this research session'),
  ).toBeNull();
});

it('shows request and response controls in the transcript', async () => {
  const spies = installClipboardAndDownloadSpies();

  renderWorkspace();
  submitResearchGoal();

  // Optimistic bubbles are replaced by durable turns; wait before retaining
  // their DOM node.
  expect(
    await screen.findByRole('heading', {name: 'Research plan'}),
  ).toBeInTheDocument();
  expect(screen.getByLabelText('Copy prompt')).toBeInTheDocument();
  expect(screen.getByLabelText('Edit prompt')).toBeInTheDocument();
  expect(screen.getAllByLabelText('Retry response')).not.toHaveLength(0);
  expect(screen.getAllByLabelText('Copy response')).not.toHaveLength(0);
  expect(screen.getAllByLabelText('Download response')).not.toHaveLength(0);

  fireEvent.click(screen.getByLabelText('Edit prompt'));
  expect(screen.getByLabelText('Edit prompt')).toHaveValue(RESEARCH_GOAL);
  fireEvent.click(screen.getByLabelText('Cancel edit'));

  fireEvent.click(screen.getByLabelText('Copy prompt'));
  await waitFor(() => {
    expect(spies.writeText).toHaveBeenCalledWith(RESEARCH_GOAL);
  });
  expect(
    screen.getByRole('heading', {name: 'Research plan'}),
  ).toBeInTheDocument();

  fireEvent.click(screen.getAllByLabelText('Copy response').at(-1)!);
  await waitFor(() => {
    expect(spies.writeText).toHaveBeenCalledWith(
      expect.stringContaining('# Investigate Glucose Homeostasis'),
    );
  });

  fireEvent.click(screen.getAllByLabelText('Download response').at(-1)!);
  expect(spies.createObjectURL).toHaveBeenCalled();
  expect(spies.createObjectURL.mock.calls[0][0].type).toBe(
    'text/markdown;charset=utf-8',
  );
  expect(spies.downloadedNames).toContain('co-scientist-research-plan.md');
  expect(spies.anchorClick).toHaveBeenCalled();
  expect(spies.revokeObjectURL).toHaveBeenCalledWith(RESPONSE_BLOB_URL);
});
