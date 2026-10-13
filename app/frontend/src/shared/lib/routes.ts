import {tabPath} from './run_tabs';

// Every in-app path is built and read here. Which side of a session to open
// stays with each caller: the rail, the home cards and the switch decide it
// differently on purpose.
export function chatPath(id: string): string {
  return `/chats/${id}`;
}

export function examplePath(runId: string): string {
  return `/examples/${runId}`;
}

export function runPath(id: string, tab?: string): string {
  return tabPath(id, tab);
}

export function routeIds(pathname: string): {
  runId: string | undefined;
  chatId: string | undefined;
} {
  const [, section, id] = pathname.split('/');
  return {
    runId: section === 'runs' ? id : undefined,
    chatId: section === 'chats' ? id : undefined,
  };
}
