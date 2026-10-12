import {fireEvent, screen} from '@testing-library/react';
import {beforeEach, expect, it, describe} from 'vitest';
import {
  installChatWorkspaceMocks,
  renderWorkspace,
  stubStatusConnectors,
} from './chat_workspace_test_helpers';

describe('chat workspace composer', () => {
  beforeEach(() => {
    installChatWorkspaceMocks();
  });

  it('toggles web search independently of PubMed', async () => {
    stubStatusConnectors();
    renderWorkspace();

    fireEvent.click(screen.getByRole('button', {name: 'Connectors'}));
    const webSearch = await screen.findByRole('menuitemcheckbox', {
      name: 'Web search',
    });

    fireEvent.click(webSearch);

    expect(
      screen.getByRole('menuitemcheckbox', {name: 'Web search'}),
    ).toHaveAttribute('aria-checked', 'false');
    const pubmed = screen.getByRole('menuitemcheckbox', {name: 'PubMed'});
    expect(pubmed).toHaveAttribute('aria-checked', 'true');

    fireEvent.click(pubmed);

    expect(
      screen.getByRole('menuitemcheckbox', {name: 'PubMed'}),
    ).toHaveAttribute('aria-checked', 'false');
    expect(
      screen.getByRole('menuitemcheckbox', {name: 'Web search'}),
    ).toHaveAttribute('aria-checked', 'false');
  });
});

describe('chat workspace composer attachments', () => {
  beforeEach(() => {
    installChatWorkspaceMocks();
  });

  it('shows uploaded file previews in the composer', async () => {
    renderWorkspace();

    const fileInput = screen.getByLabelText('Upload files');
    fireEvent.change(fileInput, {
      target: {
        files: [
          new File(['abstract'], 'deep-research-report.md', {
            type: 'text/markdown',
          }),
        ],
      },
    });

    const filename = screen.getByText('deep-research-report.md');
    expect(filename).toBeInTheDocument();
    expect(filename.closest('.reference-attachment-card')).toHaveAttribute(
      'data-tooltip',
      'deep-research-report.md',
    );
    expect(screen.getByText('TXT')).toBeInTheDocument();
    expect(screen.getByText('Markdown')).toBeInTheDocument();
    expect(
      screen.getByRole('button', {name: 'Remove deep-research-report.md'}),
    ).toHaveAttribute('data-tooltip', 'Remove deep-research-report.md');
  });
});
