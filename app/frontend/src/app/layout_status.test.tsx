import {render, screen, waitFor} from '@testing-library/react';
import {beforeEach, describe, expect, it, vi} from 'vitest';
import type {SystemStatus} from '@/shared/api/system';
import {SystemStatusIndicator} from './layout_header';

const apiMock = vi.hoisted(() => ({getSystemStatus: vi.fn()}));

vi.mock('@/shared/api/system', async importOriginal => ({
  ...(await importOriginal<typeof import('@/shared/api/system')>()),
  ...apiMock,
}));

function statusFixture(overrides: Partial<SystemStatus> = {}): SystemStatus {
  return {
    mcp_available: false,
    pubmed_available: false,
    literature_review_available: false,
    web_search_available: false,
    email_notifications_available: false,
    probes: {
      mcp: {state: 'down', error: null},
      pubmed: {state: 'down', error: null},
    },
    mcp_server_url: '',
    provider: 'engine',
    llm_backend: 'offline',
    has_provider_key: false,
    byok_enabled: null,
    engine_importable: true,
    model_name: 'test/model',
    supervisor_model_name: 'test/model',
    enabled_tools: null,
    connectors: [],
    ...overrides,
  };
}

describe('SystemStatusIndicator', () => {
  beforeEach(() => {
    apiMock.getSystemStatus.mockReset();
  });

  it('shows the Offline mode chip when /status reports offline', async () => {
    apiMock.getSystemStatus.mockResolvedValue(statusFixture());

    render(<SystemStatusIndicator />);

    await waitFor(() =>
      expect(screen.getByRole('status')).toHaveTextContent('Offline mode'),
    );
  });
});
