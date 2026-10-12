import {captureViewport, expect, test} from '../support/fixtures';

for (const theme of ['light', 'dark']) {
  for (const width of [390, 768, 1440]) {
    test(`a run refusal is readable above the composer at ${width}px in ${theme}`, async ({
      page,
    }) => {
      const viewport = {width, height: 844};
      await page.setViewportSize(viewport);
      await page.addInitScript(mode => {
        if (window.top === window) localStorage.setItem('cosci-theme', mode);
      }, theme);
      await page.goto('/');
      await expect(page.locator('html')).toHaveAttribute('data-theme', theme);
      await page
        .getByRole('textbox')
        .last()
        .fill('Which molecular checkpoints govern ferroptosis escape?');
      await page.getByRole('button', {name: 'Send', exact: true}).click();
      await expect(
        page.getByRole('button', {name: 'Copy response'}).first(),
      ).toBeVisible({timeout: 30_000});
      const start = page.getByRole('button', {
        name: 'Start research',
        exact: true,
      });
      for (const answer of [
        'Lipid repair mechanisms.',
        'Patient-derived organoids.',
        'Translational relevance.',
        'No further constraints.',
      ]) {
        if (await start.isVisible().catch(() => false)) break;
        const prior = await page
          .getByRole('button', {name: 'Copy response'})
          .count();
        await page.getByRole('textbox').last().fill(answer);
        await page.getByRole('button', {name: 'Send', exact: true}).click();
        await expect(async () => {
          expect(
            (await start.isVisible().catch(() => false)) ||
              (await page
                .getByRole('button', {name: 'Copy response'})
                .count()) > prior,
          ).toBeTruthy();
        }).toPass({timeout: 30_000});
      }
      await expect(start).toBeVisible();
      await page.getByRole('radio', {name: /Express/}).check();
      await page.evaluate(() => document.fonts.ready);
      await page.route('**/api/runs', async route => {
        if (route.request().method() !== 'POST') return route.continue();
        return route.fulfill({
          status: 429,
          json: {
            detail:
              'You have used your 3 free runs for today. Add your own API key in Settings > Model, or try again tomorrow (UTC).',
          },
        });
      });
      await start.click();
      const alert = page.getByRole('alert');
      await expect(alert).toContainText('You have used your 3 free runs');
      await page.evaluate(async () => {
        await Promise.all(
          document
            .getAnimations()
            .filter(a => a.effect?.getComputedTiming().iterations !== Infinity)
            .map(a => a.finished.catch(() => {})),
        );
      });
      await captureViewport(page, {
        ...viewport,
        name: `run-refusal-${width}-${theme}.png`,
      });
      await expect(async () => {
        const errorBounds = await alert.boundingBox();
        const composerBounds = await page
          .locator('.reference-composer')
          .boundingBox();
        expect(errorBounds).not.toBeNull();
        expect(composerBounds).not.toBeNull();
        expect(errorBounds!.y).toBeGreaterThanOrEqual(64);
        expect(errorBounds!.y + errorBounds!.height).toBeLessThanOrEqual(
          composerBounds!.y - 4,
        );
      }).toPass({timeout: 3000});
      await expect(start).toBeEnabled();
    });
  }
}
