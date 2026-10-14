import {fireEvent, render, screen} from '@testing-library/react';
import {Link, MemoryRouter, useLocation} from 'react-router-dom';
import {afterEach, beforeEach, describe, expect, it} from 'vitest';
import type {ChatSummary} from '@/shared/api/runs';
import {makeRun} from '@/shared/testing/fixtures';
import {ChatHistorySidebar, withExamples} from './layout_nav_rail';
import {HomeRecentsPanel} from '@/features/chat/chat_home_stage';
import {STORAGE_KEYS} from '@/shared/lib/safe_storage';
import {resultsPath} from '@/shared/hooks/session_side';
import {
  SessionSwitch,
  type SessionSwitchData,
} from '@/features/runs/session_switch';

const ownChat: ChatSummary = {
  id: 'chat-1',
  title: 'My goal',
  challenge: 'My goal',
  status: 'active',
  run_id: null,
  created_at: 1,
  updated_at: 1,
};

describe('sidebar example entries', () => {
  it('lists unopened examples after the visitor’s own chats', () => {
    const demo = makeRun({id: 'demo-1', is_demo: true, title: 'Example: A'});
    const chats = withExamples([ownChat], [demo, makeRun({id: 'r2'})]);
    expect(chats.map(chat => chat.id)).toEqual(['chat-1', 'example:demo-1']);
    expect(chats[1].title).toBe('Example: A');
  });

  it('drops an example once the visitor has opened their copy', () => {
    const demo = makeRun({id: 'demo-1', is_demo: true});
    const copy = makeRun({
      id: 'copy-1',
      config: {...demo.config, example_source_id: 'demo-1'},
    });
    expect(withExamples([ownChat], [demo, copy])).toEqual([ownChat]);
  });
});

describe('session entry destinations', () => {
  beforeEach(() => {
    window.localStorage.clear();
    window.sessionStorage.clear();
  });
  afterEach(() => {
    window.localStorage.clear();
    window.sessionStorage.clear();
  });

  for (const side of ['chat', 'results']) {
    it(`opens recents in Results and sidebar entries in Chat after viewing ${side}`, () => {
      window.localStorage.setItem(STORAGE_KEYS.lastSessionSide, side);
      window.localStorage.setItem(
        STORAGE_KEYS.sessionSidePrefix + 'run-1',
        side,
      );
      window.localStorage.setItem(
        STORAGE_KEYS.sessionTabPrefix + 'run-1',
        'ideas',
      );
      render(
        <MemoryRouter>
          <HomeRecentsPanel
            runs={[makeRun({id: 'run-1', title: 'Recent goal'})]}
            showAll={false}
            onToggleShowAll={() => {}}
          />
          <ChatHistorySidebar
            sideContentClasses=""
            rail={{
              chats: [{...ownChat, title: 'Sidebar goal', run_id: 'run-1'}],
              activeChatId: undefined,
              activeRunId: 'run-1',
              showAllChats: true,
              onToggleShowAllChats: () => {},
            }}
          />
        </MemoryRouter>,
      );
      expect(screen.getByRole('link', {name: /Recent Goal/})).toHaveAttribute(
        'href',
        '/runs/run-1/details',
      );
      expect(screen.getByRole('link', {name: 'Sidebar Goal'})).toHaveAttribute(
        'href',
        '/chats/chat-1',
      );
    });
  }
});

describe('run tab memory', () => {
  beforeEach(() => {
    window.localStorage.clear();
    window.sessionStorage.clear();
  });
  afterEach(() => {
    window.localStorage.clear();
    window.sessionStorage.clear();
  });

  it('opens recent runs on Goal Details instead of a previously viewed tab', () => {
    expect(resultsPath('run-1')).toBe('/runs/run-1/details');
    window.localStorage.setItem(
      STORAGE_KEYS.sessionTabPrefix + 'run-1',
      'ideas',
    );
    expect(resultsPath('run-1')).toBe('/runs/run-1/details');
  });

  function SessionNavigation() {
    const {pathname} = useLocation();
    const session: SessionSwitchData | null =
      pathname === '/chats/chat-1' || pathname.startsWith('/runs/run-1/')
        ? {
            chatId: 'chat-1',
            runId: 'run-1',
            active: pathname.startsWith('/runs/') ? 'results' : 'chat',
          }
        : pathname === '/chats/chat-2'
          ? {chatId: 'chat-2', runId: 'run-2', active: 'chat'}
          : null;
    return (
      <>
        <SessionSwitch session={session} />
        <Link to="/">Home</Link>
        <Link to="/chats/chat-1">Open first chat</Link>
        <Link to="/chats/chat-2">Open another chat</Link>
        <output data-testid="current-path">{pathname}</output>
        <HomeRecentsPanel
          runs={[makeRun({id: 'run-1', title: 'Recent goal'})]}
          showAll={false}
          onToggleShowAll={() => {}}
        />
      </>
    );
  }

  it('keeps the Results tab within Chat↔Results and resets it after leaving the chat', () => {
    render(
      <MemoryRouter initialEntries={['/runs/run-1/ideas']}>
        <SessionNavigation />
      </MemoryRouter>,
    );

    fireEvent.click(screen.getByRole('link', {name: 'Chat'}));
    fireEvent.click(screen.getByRole('link', {name: 'Results'}));
    expect(screen.getByTestId('current-path')).toHaveTextContent(
      '/runs/run-1/ideas',
    );

    fireEvent.click(screen.getByRole('link', {name: 'Home'}));
    fireEvent.click(screen.getByRole('link', {name: /Recent Goal/i}));
    expect(screen.getByTestId('current-path')).toHaveTextContent(
      '/runs/run-1/details',
    );
    fireEvent.click(screen.getByRole('link', {name: 'Home'}));
    fireEvent.click(screen.getByRole('link', {name: 'Open first chat'}));
    fireEvent.click(screen.getByRole('link', {name: 'Results'}));
    expect(screen.getByTestId('current-path')).toHaveTextContent(
      '/runs/run-1/details',
    );
  });

  it('resets the previous chat tab when another chat is selected', () => {
    render(
      <MemoryRouter initialEntries={['/runs/run-1/ideas']}>
        <SessionNavigation />
      </MemoryRouter>,
    );

    fireEvent.click(screen.getByRole('link', {name: 'Chat'}));
    fireEvent.click(screen.getByRole('link', {name: 'Open another chat'}));
    fireEvent.click(screen.getByRole('link', {name: 'Open first chat'}));
    fireEvent.click(screen.getByRole('link', {name: 'Results'}));
    expect(screen.getByTestId('current-path')).toHaveTextContent(
      '/runs/run-1/details',
    );
  });
});
