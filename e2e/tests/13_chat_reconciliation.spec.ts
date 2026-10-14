import {type Page} from '@playwright/test';
import {captureViewport, expect, test} from '../support/fixtures';

async function answerUntilPlan(page: Page): Promise<void> {
  const start = page.getByRole('button', {name: 'Start research'});
  const turns = page.getByRole('button', {name: 'Copy response'});
  for (const answer of ['Lipid repair.', 'Organoids.', 'Patients.', 'None.']) {
    if (await start.isVisible().catch(() => false)) return;
    const prior = await turns.count();
    await page.getByRole('textbox').last().fill(answer);
    await page.getByRole('button', {name: 'Send', exact: true}).click();
    await expect(async () => {
      const ready = await start.isVisible().catch(() => false);
      expect(ready || (await turns.count()) > prior).toBeTruthy();
    }).toPass({timeout: 30_000});
  }
  await expect(start).toBeVisible();
}

for (const theme of ['light', 'dark']) {
  for (const viewport of [
    {width: 390, height: 844},
    {width: 1440, height: 900},
  ]) {
    test(`a reopened chat shows one Start research request at ${viewport.width}px in ${theme}`, async ({
      page,
    }) => {
      await page.setViewportSize(viewport);
      await page.addInitScript(
        mode => localStorage.setItem('cosci-theme', mode),
        theme,
      );
      await page.goto('/');
      const composer = page.getByRole('textbox').last();
      await composer.fill('Which checkpoints govern ferroptosis escape?');
      await composer.press('Enter');
      await expect(
        page.getByRole('button', {name: 'Copy response'}).first(),
      ).toBeVisible({timeout: 30_000});
      await expect(page).toHaveURL(/\/chats\//);
      await page.reload();
      await answerUntilPlan(page);

      // Hold the chat-list refresh until the start announcement is saved: the
      // order a slow history refresh produces.
      let announced!: () => void;
      const announcement = new Promise<void>(resolve => (announced = resolve));
      page.on('response', response => {
        if (/\/messages\/started$/.test(new URL(response.url()).pathname)) {
          void response.finished().then(() => announced());
        }
      });
      await page.route('**/api/interviews', async route => {
        if (route.request().method() === 'GET') await announcement;
        await route.continue();
      });
      const refreshed = page.waitForResponse(
        response =>
          new URL(response.url()).pathname === '/api/interviews' &&
          response.request().method() === 'GET',
      );
      await page.getByRole('button', {name: 'Start research'}).click();
      await expect(
        page.getByRole('region', {name: 'Started research session'}),
      ).toBeVisible({timeout: 30_000});
      await refreshed;
      await page.waitForLoadState('networkidle');

      const requests = page
        .locator('.reference-user-bubble')
        .filter({hasText: /^Start research$/});
      await expect(requests).toHaveCount(1);
      await requests.scrollIntoViewIfNeeded();
      await captureViewport(page, {
        ...viewport,
        name: `chat-reconciliation-${viewport.width}-${theme}.png`,
      });
    });
  }
}
