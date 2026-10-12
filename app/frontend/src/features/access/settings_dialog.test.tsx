import {render, screen, waitFor, within} from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import {useState} from 'react';
import {afterEach, beforeEach, expect, it, vi} from 'vitest';
import {
  type ByokProvider,
  getStoredModel,
  setStoredApiKey,
} from '@/shared/lib/client_id';
import {jsonResponse} from '@/shared/api/testing';
import {ThemeProvider} from '@/shared/hooks/theme_context';
import {ModelSection, SettingsDialog} from './settings_dialog';

const CATALOG = {
  deepseek: ['deepseek/deepseek-flash', 'deepseek/deepseek-v4-pro'],
  gemini: ['gemini/gemini-3.8-flash', 'gemini/gemini-3.1-pro-preview'],
  openai: ['openai/gpt-6.1-sol', 'openai/gpt-6-luna'],
};

const FREE_USAGE = {
  enforced: true,
  tier: 'express',
  limit: 3,
  used: 1,
  remaining: 2,
  resets_at: 0,
};

beforeEach(() => {
  window.localStorage.clear();
  vi.stubGlobal(
    'fetch',
    vi.fn(async (url: string) =>
      jsonResponse(
        url.endsWith('/api/byok-models') ? {providers: CATALOG} : FREE_USAGE,
      ),
    ),
  );
});

afterEach(() => {
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
  window.localStorage.clear();
});

function DialogHarness() {
  const [open, setOpen] = useState(false);
  return (
    <ThemeProvider>
      <button onClick={() => setOpen(true)}>Open settings</button>
      {open && (
        <SettingsDialog
          section="appearance"
          onSectionChange={() => {}}
          onClose={() => setOpen(false)}
        />
      )}
    </ThemeProvider>
  );
}

it('returns focus to the control that opened it once it closes', async () => {
  const user = userEvent.setup();
  render(<DialogHarness />);

  const opener = screen.getByRole('button', {name: 'Open settings'});
  await user.click(opener);
  await screen.findByRole('button', {name: 'Close settings'});

  await user.keyboard('{Escape}');

  await waitFor(() => expect(opener).toHaveFocus());
});

function renderSection(
  apiKey: string,
  provider: ByokProvider,
  savedProviders: ByokProvider[] = apiKey ? [provider] : [],
) {
  return render(
    <ModelSection
      apiKey={apiKey}
      onApiKeyChange={vi.fn()}
      provider={provider}
      onProviderChange={vi.fn()}
      onSave={vi.fn()}
      savedProviders={savedProviders}
    />,
  );
}

function trigger(name: RegExp | string) {
  return screen.getByRole('button', {name});
}

it('stores a chosen supervisor model with its provider', async () => {
  const user = userEvent.setup();
  renderSection('sk-key', 'deepseek');
  await screen.findAllByText('deepseek-flash');
  await user.click(trigger(/Supervisor model/));
  const menu = screen.getByRole('menu', {name: 'Supervisor model'});
  await user.click(
    within(menu).getByRole('menuitemradio', {name: 'deepseek-v4-pro'}),
  );
  expect(getStoredModel('supervisor')).toEqual({
    provider: 'deepseek',
    model: 'deepseek/deepseek-v4-pro',
  });
  expect(getStoredModel('worker')).toEqual({
    provider: 'deepseek',
    model: 'deepseek/deepseek-flash',
  });
});

it('checks Other on blur, stores the exact ID, and keeps it when reopened', async () => {
  const user = userEvent.setup();
  setStoredApiKey('synthetic-key', 'deepseek');
  vi.stubGlobal(
    'fetch',
    vi.fn(async (url: string) =>
      jsonResponse(
        url.endsWith('/validate')
          ? {
              model: 'deepseek/new-model',
              exists: true,
              supported: true,
              error: null,
            }
          : url.endsWith('/api/byok-models')
            ? {providers: CATALOG}
            : FREE_USAGE,
      ),
    ),
  );
  const section = renderSection('synthetic-key', 'deepseek');
  await screen.findAllByText('deepseek-flash');
  await user.click(trigger(/Worker model/));
  await user.click(screen.getByRole('menuitemradio', {name: /^Other$/}));
  const input = screen.getByLabelText(/Custom worker model ID/);
  await user.type(input, 'new-model');
  await user.tab();
  await screen.findByText('Model checked');
  expect(input).toHaveValue('deepseek/new-model');
  expect(getStoredModel('worker')).toEqual({
    provider: 'deepseek',
    model: 'deepseek/new-model',
    custom: true,
  });
  section.unmount();
  renderSection('synthetic-key', 'deepseek');
  expect(await screen.findByLabelText(/Custom worker model ID/)).toHaveValue(
    'deepseek/new-model',
  );
});

it('reports an unsupported Other model on submit', async () => {
  const user = userEvent.setup();
  setStoredApiKey('synthetic-key', 'deepseek');
  vi.stubGlobal(
    'fetch',
    vi.fn(async (url: string) =>
      jsonResponse(
        url.endsWith('/validate')
          ? {
              model: 'deepseek/no-tools',
              exists: true,
              supported: false,
              error:
                'This model does not support tool calling, which Co-Scientist needs',
            }
          : url.endsWith('/api/byok-models')
            ? {providers: CATALOG}
            : FREE_USAGE,
      ),
    ),
  );
  renderSection('synthetic-key', 'deepseek');
  await screen.findAllByText('deepseek-flash');
  await user.click(trigger(/Supervisor model/));
  await user.click(screen.getByRole('menuitemradio', {name: /^Other$/}));
  await user.type(
    screen.getByLabelText(/Custom supervisor model ID/),
    'no-tools{Enter}',
  );
  expect(await screen.findByRole('alert')).toHaveTextContent(
    'does not support tool calling',
  );
});

it('does not mark edited text valid when an earlier check returns', async () => {
  const user = userEvent.setup();
  setStoredApiKey('synthetic-key', 'deepseek');
  let finish: (response: Response) => void = () => {};
  vi.stubGlobal(
    'fetch',
    vi.fn((url: string) =>
      url.endsWith('/validate')
        ? new Promise<Response>(resolve => {
            finish = resolve;
          })
        : Promise.resolve(
            jsonResponse(
              url.endsWith('/api/byok-models')
                ? {providers: CATALOG}
                : FREE_USAGE,
            ),
          ),
    ),
  );
  renderSection('synthetic-key', 'deepseek');
  await screen.findAllByText('deepseek-flash');
  await user.click(trigger(/Worker model/));
  await user.click(screen.getByRole('menuitemradio', {name: /^Other$/}));
  const input = screen.getByLabelText(/Custom worker model ID/);
  await user.type(input, 'first{Enter}');
  await screen.findByText('Checking…');
  await user.clear(input);
  await user.type(input, 'second');
  finish(
    jsonResponse({model: 'deepseek/first', exists: true, supported: true}),
  );
  await waitFor(() => expect(input).toHaveValue('second'));
  expect(screen.queryByText('Model checked')).not.toBeInTheDocument();
  expect(getStoredModel('worker')?.model).toBe('second');
});
