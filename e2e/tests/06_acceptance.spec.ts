import {DESKTOP_VIEWPORT, MOBILE_VIEWPORT, captureViewport, createCompletedRun, expect, test} from '../support/fixtures';
import {type Page} from '@playwright/test';

test.describe('visual acceptance', () => {

test('home renders at the required desktop viewport', async ({page}) => {
  await page.setViewportSize(DESKTOP_VIEWPORT);
  await page.goto('/');

  await expect(
    page.getByRole('heading', {
      name: 'What breakthrough should we make today?',
    }),
  ).toBeVisible();
  await expect(page.locator('.reference-home-main')).toHaveCSS('opacity', '1');
  await expect(
    page.getByRole('button', {name: 'Feedback', exact: true}),
  ).toBeVisible();
  await captureViewport(page, {
    ...DESKTOP_VIEWPORT,
    name: 'faithful-home-desktop-2026-07-13.png',
  });
});

test('home renders at the required mobile viewport', async ({page}) => {
  await page.setViewportSize(MOBILE_VIEWPORT);
  await page.goto('/');

  await expect(
    page.getByRole('heading', {
      name: 'What breakthrough should we make today?',
    }),
  ).toBeVisible();
  await expect(page.locator('.reference-home-main')).toHaveCSS('opacity', '1');
  await captureViewport(page, {
    ...MOBILE_VIEWPORT,
    name: 'faithful-home-mobile-2026-07-13.png',
  });
});

for (const theme of ['light', 'dark']) {
  test(`settings anchors the worker menu in ${theme} mode`, async ({page}) => {
    await page.setViewportSize(DESKTOP_VIEWPORT);
    await page.addInitScript(mode => localStorage.setItem('cosci-theme', mode), theme);
    await page.route('**/api/byok-models', route => route.fulfill({
      json: {providers: {deepseek: ['deepseek/verification-model']}},
    }));
    await page.goto('/');
    await page.getByRole('button', {name: 'Settings', exact: true}).click();
    await page.getByRole('menuitem', {name: 'Model', exact: true}).click();
    await expect(page.locator('html')).toHaveAttribute('data-theme', theme);

    const trigger = page.locator('#cosci-settings-worker-model');
    await trigger.click();
    const menu = page.getByRole('menu', {name: 'Worker model', exact: true});
    await expect(menu).toBeVisible();
    await expect.poll(async () => {
      const anchor = await trigger.boundingBox();
      const bounds = await menu.boundingBox();
      return anchor && bounds
        ? Math.abs(bounds.x + bounds.width - anchor.x - anchor.width)
        : Infinity;
    }).toBeLessThan(1);
  });
}

test('Goal Report renders at the desktop viewport', async ({page, api}) => {
  const id = await createCompletedRun(api, {
    research_goal: 'Visual acceptance goal report',
    tier: 'standard',
  });
  await page.setViewportSize(DESKTOP_VIEWPORT);
  await page.goto(`/runs/${id}/ideas`);

  await expect(
    page.getByRole('list', {name: /ranked hypothesis list/i}),
  ).toBeVisible();
  await captureViewport(page, {
    ...DESKTOP_VIEWPORT,
    name: 'faithful-goal-report-desktop-2026-07-13.png',
  });
});

test('Goal Report remains reachable at the required mobile viewport', async ({
  page,
  api,
}) => {
  const id = await createCompletedRun(api, {
    research_goal: 'Mobile visual acceptance goal report',
    tier: 'standard',
  });
  await page.setViewportSize(MOBILE_VIEWPORT);
  await page.goto(`/runs/${id}/ideas`);

  await expect(
    page.getByRole('list', {name: /ranked hypothesis list/i}),
  ).toBeVisible();
  await captureViewport(page, {
    ...MOBILE_VIEWPORT,
    name: 'faithful-goal-report-mobile-2026-07-13.png',
  });
});
});

test.describe('failure guidance', () => {
const RECORDED_ERROR = 'engine task exhausted its provider-call budget';
const BUDGET_GUIDANCE =
  'The run reached its configured model-call limit before it completed. Start a new run with a narrower research goal.';

async function routeRunFailure(
  page: Page,
  id: string,
  status: 'failed' | 'blocked',
  failureKind: string,
  error: string,
): Promise<void> {
  await page.route(`**/api/runs/${id}`, async route => {
    if (route.request().method() !== 'GET') return route.continue();
    const response = await route.fetch();
    const run = (await response.json()) as Record<string, unknown>;
    await route.fulfill({
      response,
      json: {...run, status, failure_kind: failureKind, error},
    });
  });
}

test('shows owned-run failure guidance with the recorded error after reload', async ({
  page,
  api,
}) => {
  const {id} = await api.createRun({
    research_goal: 'Offline browser test for call-budget failure guidance',
    tier: 'express',
  });
  await routeRunFailure(
    page,
    id,
    'failed',
    'llm_call_budget_exceeded',
    RECORDED_ERROR,
  );

  await page.goto(`/runs/${id}/details`);

  const guidance = page.getByRole('region', {name: 'Suggested next step'});
  await expect(guidance).toBeVisible();
  await expect(guidance).toContainText(BUDGET_GUIDANCE);
  await expect(page.getByText('Recorded error')).toBeVisible();
  await expect(page.getByText(RECORDED_ERROR, {exact: true})).toBeVisible();

  await page.reload();
  await expect(guidance).toBeVisible();
  await expect(guidance).toContainText(BUDGET_GUIDANCE);
  await expect(page.getByText(RECORDED_ERROR, {exact: true})).toBeVisible();
});

test('shows actionable guidance for the exact provider-timeout kind', async ({
  page,
  api,
}) => {
  const {id} = await api.createRun({
    research_goal: 'Offline browser test for provider-timeout guidance',
    tier: 'express',
  });
  await routeRunFailure(
    page,
    id,
    'failed',
    'llm_timeout',
    'Provider request timed out after 180 seconds',
  );

  await page.goto(`/runs/${id}/details`);

  const guidance = page.getByRole('region', {name: 'Suggested next step'});
  await expect(guidance).toBeVisible();
  await expect(guidance).toContainText(
    'The model provider did not respond within the request timeout. Try the research again later.',
  );
  await expect(
    page.getByText('Provider request timed out after 180 seconds', {
      exact: true,
    }),
  ).toBeVisible();
});

test('keeps blocked and unknown failures generic', async ({page, api}) => {
  const blocked = await api.createRun({
    research_goal: 'Offline browser test for blocked failure guidance',
    tier: 'express',
  });
  await routeRunFailure(
    page,
    blocked.id,
    'blocked',
    'llm_timeout',
    'Safety review is required',
  );

  await page.goto(`/runs/${blocked.id}/details`);
  await expect(page.getByRole('heading', {name: 'Run blocked'})).toBeVisible();
  await expect(
    page.getByRole('region', {name: 'Suggested next step'}),
  ).toHaveCount(0);
  await expect(page.getByText('Safety review is required')).toBeVisible();

  const unknown = await api.createRun({
    research_goal: 'Offline browser test for unknown failure guidance',
    tier: 'express',
  });
  await routeRunFailure(
    page,
    unknown.id,
    'failed',
    'llm_timeoutish',
    'Unclassified task error',
  );

  await page.goto(`/runs/${unknown.id}/details`);
  await expect(page.getByRole('heading', {name: 'Run failed'})).toBeVisible();
  await expect(
    page.getByRole('region', {name: 'Suggested next step'}),
  ).toHaveCount(0);
  await expect(page.getByText('Unclassified task error')).toBeVisible();
});
});
