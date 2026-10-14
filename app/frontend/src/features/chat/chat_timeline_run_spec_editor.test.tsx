import type {Interview} from '@/shared/api/runs';
import {editInterviewFields} from '@/shared/api/runs';
import {makeSpec} from '@/shared/testing/fixtures';
import {fireEvent, render, screen} from '@testing-library/react';
import {beforeEach, expect, test, vi} from 'vitest';
import type {InferredRunSpec} from '@/shared/lib/run_spec';
import {RunSpecCard} from './chat_timeline_run_spec_card';
import {
  SpecFieldsSection,
  useSpecFieldsEditor,
} from './chat_timeline_run_spec_editor';

vi.mock('@/shared/api/runs', async importOriginal => {
  const actual = await importOriginal<typeof import('@/shared/api/runs')>();
  return {...actual, editInterviewFields: vi.fn()};
});

beforeEach(() => {
  vi.clearAllMocks();
});

function Harness({
  spec,
  onFieldsChange,
}: {
  spec: InferredRunSpec;
  onFieldsChange: (patch: Partial<InferredRunSpec>) => void;
}) {
  const editor = useSpecFieldsEditor(spec, onFieldsChange);
  return (
    <>
      {!editor.editing && (
        <button type="button" onClick={editor.startEditing}>
          Edit plan
        </button>
      )}
      <SpecFieldsSection spec={spec} editor={editor} />
    </>
  );
}

function renderSection(overrides: Parameters<typeof makeSpec>[0] = {}) {
  const onFieldsChange = vi.fn();
  const spec = makeSpec({interviewId: 'interview-1', ...overrides});
  render(<Harness spec={spec} onFieldsChange={onFieldsChange} />);
  return {spec, onFieldsChange};
}

function cardProps(spec: InferredRunSpec, locked = false) {
  return {
    spec,
    isStarting: false,
    locked,
    onFocusChange: vi.fn(),
    onTierChange: vi.fn(),
    onFieldsChange: vi.fn(),
    onCancel: vi.fn(),
    onRetry: vi.fn(),
    onStart: vi.fn(),
  };
}

function renderCard(spec: InferredRunSpec, locked = false) {
  render(<RunSpecCard {...cardProps(spec, locked)} />);
}

function updatedInterview(over: Partial<Interview['fields']> = {}): Interview {
  return {
    id: 'interview-1',
    client_id: 'c1',
    status: 'active',
    fields: {
      research_challenge: 'Study liver fibrosis, revised',
      focus_area: ['Attr A'],
      preferences: ['Req A'],
      lab_constraints: [],
      title: null,
      ...over,
    },
    current_question: null,
    turns: [],
    documents: [],
    created_at: 0,
    updated_at: 0,
    completed_at: null,
  };
}

test('the plan card offers its pencil, and hides it once the form is open', () => {
  renderCard(makeSpec({interviewId: 'interview-1'}));

  fireEvent.click(screen.getByLabelText('Edit plan'));

  expect(screen.queryByLabelText('Edit plan')).not.toBeInTheDocument();
  expect(screen.getByLabelText('Research Challenge')).toBeInTheDocument();
});

test('a saved completion email does not block starting the plan', () => {
  renderCard(
    makeSpec({
      notifyOnCompletion: true,
      completionEmail: 'not-an-email',
    }),
  );

  expect(
    screen.queryByLabelText('Email me when the Goal Report is ready'),
  ).not.toBeInTheDocument();
  expect(
    screen.queryByText('Email delivery is not configured on this server.'),
  ).not.toBeInTheDocument();
  expect(screen.getByRole('button', {name: 'Start research'})).toBeEnabled();
});

test('saving calls editInterviewFields with the mapped payload and applies the result', async () => {
  vi.mocked(editInterviewFields).mockResolvedValue(updatedInterview());
  const {onFieldsChange} = renderSection({
    goal: 'Study liver fibrosis',
    attributes: ['Attr A'],
    requirements: ['Req A'],
  });

  fireEvent.click(screen.getByText('Edit plan'));
  fireEvent.change(screen.getByLabelText('Research Challenge'), {
    target: {value: 'Study liver fibrosis, revised'},
  });
  fireEvent.click(screen.getByText('Save'));

  await screen.findByText('Edit plan');
  expect(editInterviewFields).toHaveBeenCalledWith('interview-1', {
    research_challenge: 'Study liver fibrosis, revised',
    focus_area: ['Attr A'],
    preferences: ['Req A'],
  });
  expect(onFieldsChange).toHaveBeenCalledWith({
    interviewId: 'interview-1',
    goal: 'Study liver fibrosis, revised',
    attributes: ['Attr A'],
    requirements: ['Req A'],
  });
});

test('adds and removes list entries', async () => {
  vi.mocked(editInterviewFields).mockResolvedValue(updatedInterview());
  renderSection({attributes: ['Attr A'], requirements: ['Req A']});

  fireEvent.click(screen.getByText('Edit plan'));
  fireEvent.click(screen.getByText('Add focus area'));
  fireEvent.change(screen.getByLabelText('Focus Area item 2'), {
    target: {value: 'Attr B'},
  });
  fireEvent.click(screen.getByLabelText('Remove "Attr A" from Focus Area'));
  fireEvent.click(screen.getByLabelText('Remove "Req A" from Preferences'));
  fireEvent.click(screen.getByText('Save'));

  await screen.findByText('Edit plan');
  expect(editInterviewFields).toHaveBeenCalledWith(
    'interview-1',
    expect.objectContaining({focus_area: ['Attr B'], preferences: []}),
  );
});
