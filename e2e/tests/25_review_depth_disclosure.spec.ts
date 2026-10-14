import {captureViewport, expect, test} from '../support/fixtures';

for (const theme of ['light', 'dark']) {
  for (const width of [390, 768, 1440]) {
    test(`screened depth stays visible and keyboard reachable at ${width}px in ${theme}`, async ({
      page,
      api,
    }) => {
      const viewport = {width, height: 844};
      await page.setViewportSize(viewport);
      await page.addInitScript(mode => {
        if (window.top === window) localStorage.setItem('cosci-theme', mode);
      }, theme);
      const run = await api.createRun({
        research_goal: 'Which molecular checkpoints govern ferroptosis escape?',
        tier: 'express',
      });
      await api.startRun(run.id);
      await expect
        .poll(async () => (await api.getRun(run.id)).status, {timeout: 60_000})
        .toBe('completed');
      await page.goto(`/runs/${run.id}/ideas`);
      await expect(page.locator('html')).toHaveAttribute('data-theme', theme);
      const chip = page.locator('.idea-screened-chip').first();
      await expect(chip).toBeVisible();
      await chip.scrollIntoViewIfNeeded();
      await page.mouse.move(0, 0);
      await expect(chip).toHaveText('Screened, not deep-verified');
      const row = chip.locator('xpath=ancestor::a[1]');
      const chipBounds = (await chip.boundingBox())!;
      const rowBounds = (await row.boundingBox())!;
      expect(chipBounds.x).toBeGreaterThanOrEqual(rowBounds.x);
      expect(chipBounds.x + chipBounds.width).toBeLessThanOrEqual(
        rowBounds.x + rowBounds.width,
      );
      expect(chipBounds.y + chipBounds.height).toBeLessThanOrEqual(
        rowBounds.y + rowBounds.height,
      );
      await captureViewport(page, {
        ...viewport,
        name: `depth-list-${width}-${theme}.png`,
      });
      await row.click();
      const detail = page.getByRole('region', {name: 'Hypothesis detail'});
      await expect(detail).toBeVisible();
      const disclosure = detail
        .locator('summary')
        .filter({hasText: 'Screened, not deep-verified'});
      await expect(disclosure).toBeVisible();
      const selectedURL = page.url();
      await page.reload();
      await expect(disclosure).toBeVisible();
      expect(page.url()).toBe(selectedURL);
      await page.evaluate(() => {
        (document.activeElement as HTMLElement | null)?.blur();
        document.body.focus();
      });
      for (
        let tabs = 0;
        tabs < 60 &&
        !(await disclosure.evaluate(el => el === document.activeElement));
        tabs++
      ) {
        await page.keyboard.press('Tab');
      }
      await expect(disclosure).toBeFocused();
      await page.keyboard.press('Enter');
      const explanation = detail.getByText(
        'This idea passed the safety screen and a screening review. It was not selected for finalist review or deep verification.',
        {exact: true},
      );
      await expect(explanation).toBeVisible();
      await expect(async () => {
        const textBounds = (await explanation.boundingBox())!;
        const disclosureBounds = (await disclosure
          .locator('..')
          .boundingBox())!;
        expect(textBounds.y + textBounds.height).toBeLessThanOrEqual(
          disclosureBounds.y + disclosureBounds.height - 4,
        );
      }).toPass({timeout: 3000});
      await captureViewport(page, {
        ...viewport,
        name: `depth-detail-${width}-${theme}.png`,
      });
      await page.keyboard.press('Enter');
      await expect(disclosure.locator('..')).not.toHaveAttribute('open', '');
      expect(
        await page.evaluate(
          () => document.documentElement.scrollWidth > window.innerWidth,
        ),
      ).toBe(false);
    });
  }
}
