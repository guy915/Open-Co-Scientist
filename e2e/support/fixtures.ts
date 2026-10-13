import {fileURLToPath} from 'node:url';

import {
  type APIRequestContext,
  type APIResponse,
  type Page,
  expect,
  request,
  test as base,
} from '@playwright/test';
import {API_URL} from './paths';

// Browser and direct API calls need the same owner identity to share scoped
// runs.
export const CLIENT_ID = 'e2e-client';

const CLIENT_ID_KEY = 'co_scientist_client_id';

export interface BackendApi {
  createRun(body: Record<string, unknown>): Promise<{id: string}>;
  startRun(id: string): Promise<void>;
  cancelRun(id: string): Promise<void>;
  getRun(id: string): Promise<{status: string; [k: string]: unknown}>;
  listDemoRuns(): Promise<{id: string; research_goal: string}[]>;
}

async function send(
  label: string,
  call: () => Promise<APIResponse>,
): Promise<APIResponse> {
  const res = await call();
  if (!res.ok()) {
    throw new Error(`${label} failed: ${res.status()} ${await res.text()}`);
  }
  return res;
}

function makeBackendApi(ctx: APIRequestContext): BackendApi {
  return {
    async createRun(body) {
      const res = await send('createRun', () =>
        ctx.post('/api/runs', {data: body}),
      );
      return (await res.json()) as {id: string};
    },
    async startRun(id) {
      await send('startRun', () =>
        ctx.post(`/api/runs/${id}/start`, {data: {}}),
      );
    },
    async cancelRun(id) {
      await send('cancelRun', () =>
        ctx.post(`/api/runs/${id}/cancel`, {data: {}}),
      );
    },
    async getRun(id) {
      const res = await send('getRun', () =>
        ctx.get(`/api/runs/${id}`),
      );
      return (await res.json()) as {status: string};
    },
    async listDemoRuns() {
      const res = await send('listDemoRuns', () => ctx.get('/api/runs/demo'));
      const payload = (await res.json()) as {
        runs: {id: string; research_goal: string}[];
      };
      return payload.runs;
    },
  };
}

export const test = base.extend<{api: BackendApi}>({
  page: async ({page}, use) => {
    await page.route('https://www.youtube-nocookie.com/**', route =>
      route.fulfill({contentType: 'text/html', body: '<p>Trailer player</p>'}),
    );
    await page.addInitScript(
      ([clientKey, id]) => {
        window.localStorage.setItem(clientKey, id);
      },
      [CLIENT_ID_KEY, CLIENT_ID] as const,
    );
    await use(page);
  },
  api: async ({}, use) => {
    const ctx = await request.newContext({
      baseURL: API_URL,
      extraHTTPHeaders: {'X-Client-ID': CLIENT_ID},
    });
    await use(makeBackendApi(ctx));
    await ctx.dispose();
  },
});

export {expect};

export interface Viewport {
  width: number;
  height: number;
}

export const DESKTOP_VIEWPORT: Viewport = {width: 1440, height: 720};

export const MOBILE_VIEWPORT: Viewport = {width: 390, height: 780};

// Write screenshots to ignored test artifacts so later commits cannot capture
// them accidentally.
function assetPath(name: string): string {
  return fileURLToPath(new URL(`../test-results/${name}`, import.meta.url));
}

export async function assertNoHorizontalOverflow(
  page: Page,
  width: number,
): Promise<void> {
  expect(await page.evaluate(() => document.documentElement.scrollWidth)).toBe(
    width,
  );
}

export async function captureViewport(
  page: Page,
  opts: Viewport & {name: string},
): Promise<void> {
  await assertNoHorizontalOverflow(page, opts.width);
  await page.screenshot({path: assetPath(opts.name), fullPage: false});
}

export async function createCompletedRun(
  api: BackendApi,
  body: Record<string, unknown>,
): Promise<string> {
  const {id} = await api.createRun(body);
  await api.startRun(id);
  // Loaded CI runners can exceed the default completion timeout; allow genuine
  // offline completion.
  await expect
    .poll(async () => (await api.getRun(id)).status, {timeout: 60_000})
    .toBe('completed');
  return id;
}
