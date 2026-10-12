import {captureViewport, expect, test} from '../support/fixtures';

for (const theme of ['light', 'dark']) {
  for (const width of [390, 768, 1440]) {
    test(`native interview focus keeps the header inside the workspace at ${width}px in ${theme}`, async ({
      page,
    }) => {
      const viewport = {width, height: 844};
      await page.setViewportSize(viewport);
      await page.addInitScript(mode => {
        if (window.top === window) localStorage.setItem('cosci-theme', mode);
      }, theme);
      await page.goto('/');
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
      await page.evaluate(async () => {
        await Promise.all(
          document
            .getAnimations()
            .filter(a => a.effect?.getComputedTiming().iterations !== Infinity)
            .map(a => a.finished.catch(() => {})),
        );
      });
      const workspace = page.locator('.ucs-workspace');
      const header = page.locator('.ucs-header-action-bar');
      await captureViewport(page, {
        ...viewport,
        name: `shell-focus-${width}-${theme}.png`,
      });
      await expect(async () => {
        expect(await workspace.evaluate(el => el.scrollLeft)).toBe(0);
        const workspaceBounds = await workspace.boundingBox();
        const headerBounds = await header.boundingBox();
        expect(headerBounds!.x).toBeGreaterThanOrEqual(workspaceBounds!.x);
        expect(headerBounds!.x + headerBounds!.width).toBeLessThanOrEqual(
          workspaceBounds!.x + workspaceBounds!.width,
        );
      }).toPass({timeout: 3000});
      const menu = header.getByRole('button', {name: 'Open navigation'});
      if (await menu.isVisible()) await expect(menu).toBeInViewport({ratio: 1});
      await page.reload();
      await expect(start).toBeVisible();
      expect(await workspace.evaluate(el => el.scrollLeft)).toBe(0);
    });
  }
}
