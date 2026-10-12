import {type Page} from '@playwright/test';
import {CLIENT_ID, expect, test} from '../support/fixtures';

test.describe('create run', () => {
const GOAL =
  'What molecular checkpoints govern ferroptosis escape in ' +
  'glioblastoma stem cells?';

async function draftGoalInComposer(page: Page): Promise<void> {
  const composer = page.getByRole('textbox');
  await composer.click();
  await composer.fill(GOAL);
  await page.locator('input[type="file"]').setInputFiles({
    name: 'private-lactate-result.txt',
    mimeType: 'text/plain',
    buffer: Buffer.from(
      'Private pilot result: MCT1 perturbation delayed synaptic ATP recovery.',
    ),
  });
  await expect(page.getByText('private-lactate-result.txt')).toBeVisible();
  await composer.press('Enter');
}

async function answerInterviewUntilPlan(page: Page): Promise<void> {
  const startButton = page.getByRole('button', {name: 'Start research'});
  const assistantTurns = page.getByRole('button', {name: 'Copy response'});
  await expect(assistantTurns.first()).toBeVisible({timeout: 30_000});
  const answers = [
    'Prioritize GPX4-independent lipid repair mechanisms.',
    'Use patient-derived organoids and isogenic controls.',
    'Optimize for translational relevance to recurrent glioblastoma.',
    'No further constraints; proceed with the strongest directions.',
  ];
  for (const answer of answers) {
    if (await startButton.isVisible().catch(() => false)) break;
    const priorTurns = await assistantTurns.count();
    await page.getByRole('textbox').last().fill(answer);
    await page.getByRole('button', {name: 'Send'}).click();
    await expect(async () => {
      const ready = await startButton.isVisible().catch(() => false);
      const advanced = (await assistantTurns.count()) > priorTurns;
      expect(ready || advanced).toBeTruthy();
    }).toPass({timeout: 30_000});
  }

  await expect(startButton).toBeVisible({timeout: 30_000});
  await expect(page.getByText(GOAL).first()).toBeVisible();
}

// Only a real browser exposes plan-anchor geometry; zero targets the
// conversation, not the plan.
async function assertPlanOpensAtItsOwnTop(page: Page): Promise<void> {
  await expect(async () => {
    const metrics = await page.evaluate(() => {
      const scroller = document.querySelector('.reference-chat-timeline');
      const card = document.querySelector('[data-timeline-anchor="draft-spec"]');
      if (!scroller || !card) return null;
      return {
        scrollTop: scroller.scrollTop,
        overflows: scroller.scrollHeight > scroller.clientHeight,
        viewport: scroller.clientHeight,
        cardOffset:
          card.getBoundingClientRect().top -
          scroller.getBoundingClientRect().top,
      };
    });
    expect(metrics).not.toBeNull();
    if (!metrics!.overflows) return;
    expect(metrics!.scrollTop).toBeGreaterThan(0);
    expect(metrics!.cardOffset).toBeGreaterThan(-8);
    expect(metrics!.cardOffset).toBeLessThan(metrics!.viewport);
  }).toPass({timeout: 10_000});
}

async function startRunFromPlan(page: Page): Promise<string> {
  const createdPromise = page.waitForResponse(
    response =>
      new URL(response.url()).pathname === '/api/runs' &&
      response.request().method() === 'POST',
  );
  const startAttemptPromise = page.waitForResponse(
    response =>
      /\/api\/runs\/[^/]+\/start$/.test(new URL(response.url()).pathname) &&
      response.request().method() === 'POST',
  );
  // Observe start frames because standby copy can hide an incorrect route or
  // frame vocabulary.
  const announcedPromise = page.waitForResponse(
    response =>
      /\/api\/runs\/[^/]+\/messages\/started$/.test(
        new URL(response.url()).pathname,
      ) && response.request().method() === 'POST',
  );
  await page.getByRole('button', {name: 'Start research'}).click();
  const created = await createdPromise;
  expect(created.status()).toBe(200);
  const {id} = (await created.json()) as {id: string};
  const startAttempt = await startAttemptPromise;
  expect(startAttempt.status()).toBe(200);
  const announced = await announcedPromise;
  expect(announced.status()).toBe(200);
  expect(await announced.text()).toContain('"type": "done"');
  await expect(
    page.getByRole('region', {name: 'Started research session'}),
  ).toBeVisible();
  return id;
}

async function openRunDetail(page: Page, id: string): Promise<void> {
  const ssePromise = page.waitForResponse(
    response => /\/api\/runs\/[^/]+\/events/.test(response.url()),
    {timeout: 30_000},
  );
  await page.goto(`/runs/${id}/specifications`);
  const sse = await ssePromise;
  expect(sse.status()).toBe(200);
  await expect(page).toHaveURL(/\/runs\/[^/]+\/specifications/);
}

async function assertIdeasTabShowsMatureReviews(page: Page): Promise<void> {
  const detail = page.getByRole('region', {name: 'Hypothesis detail'});
  const fullReviewHeading = detail.getByRole('heading', {
    name: 'Full review',
    exact: true,
  });
  await expect(fullReviewHeading).toBeVisible();
  await expect(
    detail.getByRole('heading', {name: 'Simulation review', exact: true}),
  ).toBeVisible();
  // Full and recurrent reviews share labels; scope queries to the intended
  // review container.
  const fullReviewSection = fullReviewHeading.locator('xpath=..');
  await expect(
    fullReviewSection.getByText('Verdict:', {exact: true}),
  ).toBeVisible();
  await expect(
    fullReviewSection.getByText('Time to verdict:', {exact: true}),
  ).toBeVisible();
  // Critique prose repeats structured labels; exact matching avoids multiple
  // elements.
  await expect(
    detail.getByText('Failure point:', {exact: true}),
  ).toBeVisible();
  await expect(
    detail.getByText('Decisive step:', {exact: true}),
  ).toBeVisible();
}

async function assertOverviewTabShowsDirectionsPreview(
  page: Page,
): Promise<void> {
  await expect(
    page.getByRole('heading', {name: 'Research directions', level: 3}),
  ).toBeVisible();
  const preview = page
    .getByText('We will be focusing on these research directions:')
    .locator('xpath=following-sibling::ul[1]');
  await expect(preview.getByRole('listitem')).toHaveCount(3);
}

async function assertStreamedRunCompletes(page: Page): Promise<void> {
  const ideasTab = page.getByRole('link', {name: 'All Ideas', exact: true});
  await expect(ideasTab).toBeVisible({timeout: 30_000});
  await ideasTab.click();
  await expect(
    page.getByRole('list', {name: /ranked hypothesis list/i}),
  ).toBeVisible();
  await assertIdeasTabShowsMatureReviews(page);
  await page
    .getByRole('link', {name: 'Research Overview', exact: true})
    .click();
  // The section and per-aim headings share a phrase; distinguish them by
  // heading level.
  await expect(
    page.getByRole('heading', {name: /specific aims/i, level: 3}),
  ).toBeVisible();
  await expect(
    page.getByRole('heading', {name: /agent insights/i}),
  ).toBeVisible();
  await assertOverviewTabShowsDirectionsPreview(page);
  await page.getByRole('link', {name: 'Learning', exact: true}).click();
  await expect(page.getByText('private-lactate-result.txt')).toBeVisible();
}

test('creates a run from chat, starts it, and watches it complete', async ({
  page,
}) => {
  await page.goto('/');
  await draftGoalInComposer(page);
  await answerInterviewUntilPlan(page);
  await assertPlanOpensAtItsOwnTop(page);
  const id = await startRunFromPlan(page);
  await openRunDetail(page, id);
  await assertStreamedRunCompletes(page);
});

test('manually continues a linked draft after a lost create response', async ({
  browser,
  page,
  api,
}) => {
  const createBodies: Record<string, unknown>[] = [];
  const createKeys: string[] = [];
  const startRequests: string[] = [];
  let resolveCreated!: (id: string) => void;
  const createdPromise = new Promise<string>(resolve => {
    resolveCreated = resolve;
  });
  let releaseCreateResponse!: () => void;
  const createResponseGate = new Promise<void>(resolve => {
    releaseCreateResponse = resolve;
  });
  let resolveStartForwarded!: () => void;
  const startForwarded = new Promise<void>(resolve => {
    resolveStartForwarded = resolve;
  });
  let releaseStartResponse!: () => void;
  const startResponseGate = new Promise<void>(resolve => {
    releaseStartResponse = resolve;
  });

  await page.context().route('**/api/runs', async route => {
    if (route.request().method() !== 'POST') return route.continue();
    createBodies.push(
      route.request().postDataJSON() as Record<string, unknown>,
    );
    createKeys.push(route.request().headers()['idempotency-key'] ?? '');
    if (createBodies.length > 1) return route.continue();

    // Simulate response loss after the API commits the run/interview link.
    const response = await route.fetch();
    expect(response.status()).toBe(200);
    const {id} = (await response.json()) as {id: string};
    resolveCreated(id);
    await createResponseGate;
    await route.abort('failed');
  });
  await page.context().route('**/api/runs/*/start', async route => {
    if (route.request().method() !== 'POST') return route.continue();
    startRequests.push(route.request().url());
    if (startRequests.length > 1) return route.continue();
    const response = await route.fetch();
    expect(response.status()).toBe(200);
    const body = await response.text();
    const upstreamHeaders = response.headers();
    const headers = Object.fromEntries(
      Object.entries(upstreamHeaders).filter(([name]) =>
        [
          'access-control-allow-origin',
          'access-control-allow-credentials',
          'access-control-expose-headers',
          'content-type',
          'vary',
        ].includes(name),
      ),
    );
    resolveStartForwarded();
    await startResponseGate;
    await route.fulfill({
      status: response.status(),
      headers,
      body,
    });
  });

  await page.goto('/');
  await draftGoalInComposer(page);
  await answerInterviewUntilPlan(page);
  const chatUrl = page.url();
  const firstStartClick = page
    .getByRole('button', {name: 'Start research'})
    .click();
  const runId = await createdPromise;
  await expect(page.getByRole('button', {name: 'Starting...'})).toBeDisabled();
  releaseCreateResponse();
  await firstStartClick;
  await expect(page.getByRole('alert')).toBeVisible();
  expect(await api.getRun(runId)).toMatchObject({status: 'draft'});

  await page.reload();
  const continueButton = page.getByRole('button', {
    name: 'Continue research',
  });
  await expect(continueButton).toBeEnabled();
  expect(startRequests).toHaveLength(0);
  expect(createBodies).toHaveLength(1);
  const chatId = new URL(chatUrl).pathname.split('/').at(-1) ?? '';
  const storedRequestMatches = await page.evaluate(
    ({id, key, payload}) => {
      const saved = JSON.parse(
        sessionStorage.getItem(
          `co_scientist_pending_run_create:${encodeURIComponent(id)}`,
        ) ?? 'null',
      ) as {key?: string; payloadJson?: string} | null;
      if (!saved?.payloadJson) return false;
      return (
        saved.key === key &&
        JSON.stringify(JSON.parse(saved.payloadJson)) ===
          JSON.stringify(payload)
      );
    },
    {id: chatId, key: createKeys[0], payload: createBodies[0]},
  );
  expect(storedRequestMatches).toBe(true);

  const otherContext = await browser.newContext();
  await otherContext.addInitScript(clientId => {
    window.localStorage.setItem('co_scientist_client_id', clientId);
  }, `${CLIENT_ID}-other`);
  const otherPage = await otherContext.newPage();
  await otherPage.goto(chatUrl);
  await expect(
    otherPage.getByRole('button', {name: 'Continue research'}),
  ).toHaveCount(0);
  expect(startRequests).toHaveLength(0);
  expect(await api.getRun(runId)).toMatchObject({status: 'draft'});
  await otherContext.close();

  await continueButton.focus();
  const continueByKeyboard = continueButton.press('Enter');
  await startForwarded;
  await expect(
    page.getByRole('button', {name: 'Continuing...'}),
  ).toBeDisabled();
  await expect(
    page.getByText('Continuing research', {exact: true}),
  ).toBeVisible();
  releaseStartResponse();
  await continueByKeyboard;

  await expect(
    page.getByRole('region', {name: 'Started research session'}),
  ).toBeVisible();
  expect(startRequests).toHaveLength(1);
  expect(createBodies).toHaveLength(1);
  expect(await api.getRun(runId)).not.toMatchObject({status: 'draft'});

  await page.reload();
  await expect(
    page.getByRole('region', {name: 'Started research session'}),
  ).toBeVisible();
  expect(startRequests).toHaveLength(1);
  expect(createBodies).toHaveLength(1);
});
});

test.describe('run detail tabs', () => {
test('run detail tabs render hypotheses, Elo, and report content', async ({
  page,
  api,
}) => {
  const demos = await api.listDemoRuns();
  const demo = demos.find(run =>
    /staphylococcus aureus/i.test(run.research_goal),
  );
  expect(demo).toBeTruthy();
  await page.goto(`/runs/${demo!.id}/specifications`);

  await expect(
    page.getByRole('heading', {name: /run specifications/i}),
  ).toBeVisible();
  await expect(page.getByText(/staphylococcus aureus/i).first()).toBeVisible();

  await page.getByRole('link', {name: 'All Ideas', exact: true}).click();
  await expect(
    page.getByRole('list', {name: /ranked hypothesis list/i}),
  ).toBeVisible();
  await expect(page.getByText(/elo rating:/i).first()).toBeVisible();

  await page.getByRole('link', {name: 'Learning', exact: true}).click();
  await expect(page.getByRole('heading', {name: /references/i})).toBeVisible();
  await expect(
    page.getByRole('textbox', {name: /search references/i}),
  ).toBeVisible();

  await page
    .getByRole('link', {name: 'Research Overview', exact: true})
    .click();
  await expect(
    page.getByRole('heading', {name: /agent insights/i}),
  ).toBeVisible();
  await expect(
    page.getByRole('heading', {name: /winning ideas/i}),
  ).toBeVisible();
  await expect(
    page.getByText(/tournament matches have been recorded/i),
  ).toBeVisible();
});
});

test.describe('cancel', () => {
// Use a large admitted run so cooperative cancellation can land before offline
// execution finishes.
test('cancels a running run and shows the cancelled state on home', async ({
  page,
  api,
}) => {
  const goal = 'Long-running cancellation probe for the browser e2e harness';

  const {id} = await api.createRun({
    research_goal: goal,
    tier: 'extended',
    max_iterations: 6,
    initial_hypotheses_count: 30,
  });
  await api.startRun(id);

  await expect
    .poll(async () => (await api.getRun(id)).status, {
      timeout: 20_000,
      intervals: [200],
    })
    .toMatch(/running|synthesizing/);

  // API cancellation isolates Recents rendering from the separately tested
  // header stop control.
  await api.cancelRun(id);

  // Cancellation settles cooperatively at the next checkpoint.
  await expect
    .poll(async () => (await api.getRun(id)).status, {
      timeout: 20_000,
      intervals: [200],
    })
    .toBe('cancelled');

  await page.goto('/');
  const recents = page.getByRole('complementary', {name: 'Recent runs'});
  const card = recents.getByRole('link').filter({hasText: goal});
  await expect(card).toBeVisible();
  await expect(card.getByText(/status:\s*cancelled/i)).toBeVisible();
});
});
