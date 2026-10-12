import {render, screen} from '@testing-library/react';
import {MemoryRouter} from 'react-router-dom';
import {afterEach, beforeEach, describe, expect, it} from 'vitest';
import type {ChatSummary} from '@/shared/api/runs';
import {makeRun} from '@/shared/testing/fixtures';
import {ChatHistorySidebar, withExamples} from './layout_nav_rail';
import {HomeRecentsPanel} from '@/features/chat/chat_home_stage';
import {STORAGE_KEYS} from '@/shared/lib/safe_storage';
import {resultsPath, useRecordRunTab} from '@/shared/hooks/session_side';

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
  beforeEach(() => window.localStorage.clear());
  afterEach(() => window.localStorage.clear());

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
        '/runs/run-1/ideas',
      );
      expect(screen.getByRole('link', {name: 'Sidebar Goal'})).toHaveAttribute(
        'href',
        '/chats/chat-1',
      );
    });
  }
});

describe('run tab memory', () => {
  beforeEach(() => window.localStorage.clear());
  afterEach(() => window.localStorage.clear());

  function RecordAt({path}: {path: string}) {
    return (
      <MemoryRouter initialEntries={[path]}>
        <TabRecorder />
      </MemoryRouter>
    );
  }

  function TabRecorder() {
    useRecordRunTab();
    return null;
  }

  it('reopens Results on the tab last viewed for that run', () => {
    expect(resultsPath('run-1')).toBe('/runs/run-1/details');
    render(<RecordAt path="/runs/run-1/ideas" />);
    expect(resultsPath('run-1')).toBe('/runs/run-1/ideas');
    expect(resultsPath('run-2')).toBe('/runs/run-2/details');
  });
});
