import {render} from '@testing-library/react';
import type {ReactNode} from 'react';
import {MemoryRouter} from 'react-router-dom';
import {
  ChatHistoryProvider,
  RunHistoryProvider,
} from '@/shared/hooks/history_context';
import {ThemeProvider} from '@/shared/hooks/theme_context';

interface ProviderStackProps {
  path?: string;
  theme?: boolean;
  children: ReactNode;
}

// Shared history keeps running pages from flashing report chrome; match the
// app's provider stack.
export function ProviderStack({
  path = '/',
  theme = false,
  children,
}: ProviderStackProps) {
  const stack = (
    <MemoryRouter initialEntries={[path]}>
      <RunHistoryProvider>
        <ChatHistoryProvider>{children}</ChatHistoryProvider>
      </RunHistoryProvider>
    </MemoryRouter>
  );
  return theme ? <ThemeProvider>{stack}</ThemeProvider> : stack;
}

export function renderWithProviders(
  ui: ReactNode,
  options: Omit<ProviderStackProps, 'children'> = {},
) {
  return render(<ProviderStack {...options}>{ui}</ProviderStack>);
}
