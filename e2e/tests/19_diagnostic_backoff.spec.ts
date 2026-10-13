import {captureViewport, expect, test} from '../support/fixtures';
import {API_URL} from '../support/paths';

for (const theme of ['light', 'dark']) {
  for (const width of [390, 768, 1440]) {
    test(`rate-limited diagnostics pause across reload and recover at ${width}px in ${theme}`, async ({
      page,
    }) => {
      const viewport = {width, height: 844};
      await page.setViewportSize(viewport);
      await page.clock.install();
      await page.addInitScript(
        mode => localStorage.setItem('cosci-theme', mode),
        theme,
      );
      let submissions = 0;
      await page.route(`${API_URL}/api/logs`, route => {
        if (route.request().method() !== 'POST') return route.continue();
        submissions++;
        return route.fulfill({
          status: 429,
          json: {detail: 'log ingestion rate exceeded'},
        });
      });
      const rejected = page.waitForResponse(
        response =>
          response.url() === `${API_URL}/api/logs` &&
          response.request().method() === 'POST',
      );
      await page.goto('/');
      await (await rejected).finished();
      await expect(page.getByRole('textbox')).toBeEditable();
      await page.clock.fastForward(3_000);
      const initialSubmissions = submissions;
      expect(initialSubmissions).toBeGreaterThan(0);

      const feedback = page.getByRole('button', {
        name: 'Feedback',
        exact: true,
      });
      await feedback.click();
      await expect(page.getByLabel('Message', {exact: true})).toBeFocused();
      await captureViewport(page, {
        ...viewport,
        name: `diagnostic-rate-limit-${width}-${theme}.png`,
      });
      await page.keyboard.press('Escape');
      await page.clock.fastForward(3_000);
      expect(submissions).toBe(initialSubmissions);

      await page.reload();
      await expect(page.getByRole('textbox')).toBeEditable();
      await page.clock.fastForward(3_000);
      expect(submissions).toBe(initialSubmissions);

      await page.clock.fastForward(60_001);
      await page.getByRole('button', {name: 'Feedback', exact: true}).click();
      await expect.poll(() => submissions).toBeGreaterThan(initialSubmissions);
      await expect(page.getByLabel('Message', {exact: true})).toBeEditable();
      await page.keyboard.press('Escape');
      await expect(page.getByRole('textbox')).toBeEditable();
    });
  }
}
