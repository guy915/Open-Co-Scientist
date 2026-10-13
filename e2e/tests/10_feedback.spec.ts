import {captureViewport, expect, test} from '../support/fixtures';

for (const theme of ['light', 'dark']) {
  for (const width of [1440, 768]) {
    test(`feedback silently attaches session diagnostics at ${width}px in ${theme}`, async ({
      page,
      api,
    }) => {
      await page.setViewportSize({width, height: 812});
      await page.addInitScript(
        mode => localStorage.setItem('cosci-theme', mode),
        theme,
      );
      await page.route('https://www.youtube-nocookie.com/**', route =>
        route.fulfill({body: '<html>Trailer</html>', contentType: 'text/html'}),
      );
      await page.goto('/');
      const header = page.locator('.ucs-header-action-bar');
      const feedback = header.getByRole('button', {
        name: 'Feedback',
        exact: true,
      });
      await expect(feedback).toBeInViewport();
      await expect(header.getByRole('button', {name: /^Logs/})).toHaveCount(0);
      await feedback.click();
      let dialog = page.getByRole('dialog', {name: 'Feedback', exact: true});
      await expect(dialog.getByLabel('Message')).toBeFocused();
      await expect(
        dialog.getByRole('button', {name: 'Submit', exact: true}),
      ).toBeDisabled();
      await dialog.getByRole('button', {name: /^Category/}).click();
      expect(await dialog.getByRole('menuitemradio').allTextContents()).toEqual(
        ['Bug', 'Security', 'Results quality', 'Feature request', 'Other'],
      );
      await captureViewport(page, {
        width,
        height: 812,
        name: `feedback-category-${theme}-${width}.png`,
      });
      await page.keyboard.press('Escape');
      await expect(dialog).toBeVisible();
      await expect(dialog.getByRole('menu')).toHaveCount(0);
      await page.keyboard.press('Escape');
      await expect(dialog).toHaveCount(0);
      await expect(feedback).toBeFocused();
      const examples = await api.listDemoRuns();
      await page.goto(`/examples/${examples[0].id}`);
      await expect(page).toHaveURL(/\/chats\//);
      const linkedRun = await page
        .getByRole('link', {name: 'View session details'})
        .getAttribute('href');
      const runId = linkedRun!.split('/')[2];
      await feedback.click();
      dialog = page.getByRole('dialog', {name: 'Feedback', exact: true});
      await expect(dialog.getByLabel('Message')).toBeFocused();
      const box = await dialog.boundingBox();
      expect(box!.x).toBeGreaterThanOrEqual(0);
      expect(box!.x + box!.width).toBeLessThanOrEqual(width);
      expect(box!.y).toBeGreaterThanOrEqual(0);
      expect(box!.y + box!.height).toBeLessThanOrEqual(812);
      expect(box!.x + box!.width / 2).toBeCloseTo(width / 2, 0);
      const message = `Feedback browser verification ${theme} ${width}`;
      await dialog.getByRole('button', {name: /^Category/}).click();
      await dialog
        .getByRole('menuitemradio', {name: 'Results quality'})
        .click();
      await dialog.getByLabel('Message').fill(message);
      await expect(dialog.getByText(/diagnostic|logs|attach/i)).toHaveCount(0);
      await captureViewport(page, {
        width,
        height: 812,
        name: `feedback-${theme}-${width}.png`,
      });
      const submittedRequest = page.waitForRequest(
        request =>
          request.url().endsWith('/api/feedback') &&
          request.method() === 'POST',
      );
      const submitted = page.waitForResponse(
        response =>
          response.url().endsWith('/api/feedback') &&
          response.request().method() === 'POST',
      );
      await dialog.getByRole('button', {name: 'Submit', exact: true}).click();
      expect((await submitted).status()).toBe(201);
      await expect(dialog).toHaveCount(0);
      const row = (await submittedRequest).postDataJSON() as {
        message: string;
        category: string;
        url: string;
        run_id: string;
        diagnostics: string;
      };
      expect(row.message).toBe(message);
      expect(row.category).toBe('Results quality');
      expect(row.run_id).toBe(runId);
      expect(row.url).toBe(page.url());
      for (const marker of [
        '## About these logs',
        '## Session details',
        '## Statistics (loaded window)',
        '## Logs (JSON)',
      ])
        expect(row.diagnostics).toContain(marker);
      expect(row.diagnostics).toContain('modal_open: Feedback');
      await expect(page.getByRole('group', {name: /logs/i})).toHaveCount(0);
    });
  }
}
