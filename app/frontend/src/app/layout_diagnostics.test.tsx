import {screen, waitFor} from '@testing-library/react';
import {beforeEach, describe, expect, it} from 'vitest';
import {
  installLayoutMocks,
  logsApiMock,
  renderLayout,
} from '@/app/layout_test_support';

describe('session diagnostics', () => {
  beforeEach(() => {
    installLayoutMocks();
  });

  it('anchors the feedback export when the shell mounts', async () => {
    renderLayout();
    await waitFor(() =>
      expect(logsApiMock.getAppLogs).toHaveBeenCalledWith(0, 1, 'WARNING'),
    );
  });

  it('has no header Logs button', async () => {
    renderLayout();
    await waitFor(() => expect(logsApiMock.getAppLogs).toHaveBeenCalled());
    expect(screen.queryByRole('button', {name: /^Logs/})).toBeNull();
  });
});
