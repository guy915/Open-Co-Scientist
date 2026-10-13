import {expect, it} from 'vitest';
import {chatPath, examplePath, routeIds, runPath} from './routes';

it('builds every in-app path', () => {
  expect(chatPath('c1')).toBe('/chats/c1');
  expect(examplePath('r1')).toBe('/examples/r1');
  expect(runPath('r1')).toBe('/runs/r1/details');
  expect(runPath('r1', 'ideas')).toBe('/runs/r1/ideas');
});

it('reads the run or chat id from a path', () => {
  expect(routeIds('/runs/r1/ideas')).toEqual({runId: 'r1', chatId: undefined});
  expect(routeIds('/chats/c1')).toEqual({runId: undefined, chatId: 'c1'});
  expect(routeIds('/')).toEqual({runId: undefined, chatId: undefined});
});
