import {fireEvent, screen} from '@testing-library/react';
import {makeChat} from '@/shared/testing/fixtures';
import {beforeEach, expect, it, describe} from 'vitest';
import {installLayoutMocks, renderLayout, apiMock} from './layout_test_support';

describe('layout sidebar', () => {
  beforeEach(() => {
    installLayoutMocks();
  });

  function renderTwelveSidebarChats() {
    apiMock.listInterviews.mockResolvedValue(
      Array.from({length: 12}, (_, index) =>
        makeChat(
          {
            id: `chat-${index + 1}`,
            challenge: `Very long sidebar research question ${index + 1}`,
          },
          'active',
        ),
      ),
    );
    renderLayout();
  }

  it('opens started sessions and drafts in the chat view', async () => {
    apiMock.listInterviews.mockResolvedValue([
      makeChat(
        {
          id: 'chat-running',
          challenge: 'Map senolytic clearance in aged tissue.',
          title: 'Senolytic clearance',
          run_id: 'run-senolytic',
          status: 'completed',
        },
        'active',
      ),
      makeChat(
        {
          id: 'chat-draft',
          challenge: 'Explore ferroptosis in pancreatic cancer.',
          title: 'Ferroptosis draft',
        },
        'active',
      ),
    ]);

    renderLayout();

    expect(
      await screen.findByRole('link', {name: 'Senolytic Clearance'}),
    ).toHaveAttribute('href', '/chats/chat-running');
    expect(
      screen.getByRole('link', {name: 'Ferroptosis Draft'}),
    ).toHaveAttribute('href', '/chats/chat-draft');
  });

  it('names an untitled chat the same in its row and its pop-up', async () => {
    apiMock.listInterviews.mockResolvedValue([
      makeChat({id: 'chat-blank', title: null, challenge: ''}, 'active'),
      makeChat(
        {id: 'chat-lower', title: 'hello', challenge: 'hello'},
        'active',
      ),
    ]);

    renderLayout();

    const blank = await screen.findByRole('link', {name: 'Untitled session'});
    expect(blank).toHaveAttribute('data-tooltip', 'Untitled session');
    expect(screen.getByRole('link', {name: 'Hello'})).toBeInTheDocument();
  });

  it('shows the chats that fit before expanding the rest', async () => {
    renderTwelveSidebarChats();

    expect(
      await screen.findByRole('link', {
        name: /Very long sidebar research question 10/i,
      }),
    ).toHaveAttribute('data-tooltip', 'Very long sidebar research question 10');
    expect(
      screen.queryByRole('link', {
        name: /Very long sidebar research question 11/i,
      }),
    ).toBeNull();

    fireEvent.click(screen.getByRole('button', {name: 'Show more'}));

    expect(
      screen.getByRole('link', {
        name: /Very long sidebar research question 11/i,
      }),
    ).toBeInTheDocument();
    expect(screen.queryByRole('button', {name: 'Show more'})).toBeNull();

    fireEvent.click(screen.getByRole('button', {name: 'Show less'}));

    expect(
      screen.queryByRole('link', {
        name: /Very long sidebar research question 11/i,
      }),
    ).toBeNull();
    expect(screen.getByRole('button', {name: 'Show more'})).toBeInTheDocument();
  });
});
