import {captureViewport, CLIENT_ID, expect, test} from '../support/fixtures';
import {API_URL} from '../support/paths';

for (const theme of ['light', 'dark']) {
  test(`product name and header controls do not overlap in ${theme}`, async ({
    page,
    api,
  }) => {
    await page.addInitScript(
      mode => localStorage.setItem('cosci-theme', mode),
      theme,
    );
    const demos = await api.listDemoRuns();
    const copied = await page.request.post(
      `${API_URL}/api/runs/${demos[0].id}/example-chat`,
      {headers: {'X-Client-ID': CLIENT_ID}},
    );
    expect(copied.ok()).toBe(true);
    const chat = (await copied.json()) as {id: string};

    for (const width of [375, 390, 768, 1440]) {
      const viewport = {width, height: 844};
      await page.setViewportSize(viewport);
      for (const [state, path] of [
        ['home', '/'],
        ['session', `/chats/${chat.id}`],
      ]) {
        await page.goto(path);
        if (state === 'session') {
          await expect(
            page.getByRole('navigation', {name: 'Session view'}),
          ).toBeVisible();
        }
        const header = page.locator('.ucs-header-action-bar');
        const product = header.getByRole('link', {
          name: /Go to (Open )?Co-Scientist home/,
        });
        const feedback = header.getByRole('button', {
          name: 'Feedback',
          exact: true,
        });
        await expect(product).toBeInViewport();
        await expect(feedback).toBeInViewport();
        await page.evaluate(() => document.fonts.ready);
        const productBounds = await product.boundingBox();
        const actionsBounds = await header
          .locator('.ucs-header-actions')
          .boundingBox();
        expect(productBounds!.x + productBounds!.width).toBeLessThanOrEqual(
          actionsBounds!.x - 2,
        );
        const menu = header.getByRole('button', {name: 'Open navigation'});
        if (await menu.isVisible()) {
          const menuBounds = await menu.boundingBox();
          expect(menuBounds!.x + menuBounds!.width).toBeLessThanOrEqual(
            productBounds!.x - 2,
          );
        }
        await captureViewport(page, {
          ...viewport,
          name: `header-${state}-${width}-${theme}.png`,
        });
      }
    }
  });
}
