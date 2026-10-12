import {resetRunDetailMocks, setStream} from './run_detail_api_test_support';
import * as runsApi from '@/shared/api/runs';
import {type SafetyDecision} from '@/shared/api/runs';
import {ProviderStack} from '@/shared/testing/render';
import {render, screen, waitFor} from '@testing-library/react';
import {Route, Routes} from 'react-router-dom';
import {beforeEach, expect, it, vi} from 'vitest';
import {RunDetail} from './run_detail';
import {renderAt} from './run_detail_test_support';

function decision(fields: Partial<SafetyDecision> & {id: number}) {
  return {
    stage: 'claim_gate',
    decision: 'block',
    reason: `hypothesis ${fields.id}: 2 categorical claim(s) lack support`,
    matches: [],
    risk_domains: [],
    requires_review: false,
    resolution: null,
    ...fields,
  } as SafetyDecision;
}

const heldIntakeDecision = decision({
  id: 7,
  stage: 'intake',
  decision: 'hold',
  reason: 'Ambiguous dual-use intent.',
  category: 'uncertain',
  policy_version: 'coscientist-safety-v2',
  risk_domains: ['biology'],
  requires_review: true,
  assessor: 'semantic:test-model',
});

it('refetches on a coalesced batch ending in status with data', async () => {
  const getRun = vi.mocked(runsApi.getRun);
  // Create a fresh React element per render so mutated stream state is reread.
  const makeUi = () => (
    <ProviderStack path="/runs/run-1/specifications">
      <Routes>
        <Route path="/runs/:id" element={<RunDetail />} />
        <Route path="/runs/:id/:tab" element={<RunDetail />} />
      </Routes>
    </ProviderStack>
  );
  const {rerender} = render(makeUi());
  await screen.findByText('Run Specifications');
  const afterMount = getRun.mock.calls.length;

  setStream([{seq: 1, type: 'status', payload: {}}]);
  rerender(makeUi());
  expect(getRun.mock.calls.length).toBe(afterMount);

  setStream([
    {seq: 1, type: 'status', payload: {}},
    {seq: 2, type: 'generate', payload: {}},
    {seq: 3, type: 'status', payload: {}},
  ]);
  rerender(makeUi());
  await waitFor(() =>
    expect(getRun.mock.calls.length).toBeGreaterThan(afterMount),
  );
});

beforeEach(() => {
  resetRunDetailMocks();
});

it('shows a recorded resolution when one exists', async () => {
  vi.mocked(runsApi.getSafety).mockResolvedValue([
    decision({...heldIntakeDecision, id: 7, resolution: 'approved'}),
  ]);
  renderAt('/runs/run-1/specifications');

  await screen.findByRole('heading', {name: 'Safety audit'});
  expect(screen.getByText('Resolution: approved')).toBeInTheDocument();
});
