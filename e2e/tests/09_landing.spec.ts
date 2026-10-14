import {captureViewport, expect, test} from '../support/fixtures';

for (const theme of ["light", "dark"]) {
  for (const notice of [false, true]) {
    for (const viewport of [
      { width: 1280, height: 720 },
      { width: 1440, height: 812 },
      { width: 768, height: 812 },
    ]) {
      test(`home prompt grows upward at ${viewport.width}px in ${theme} with notice ${notice}`, async ({
        page,
      }) => {
        await page.setViewportSize(viewport);
        await page.addInitScript(
          (mode) => localStorage.setItem("cosci-theme", mode),
          theme,
        );
        if (notice) {
          await page.route("**/api/launch-status", (route) =>
            route.fulfill({
              json: {
                paused: false,
                reason: "unavailable",
                message: "No model is available right now",
                resumes_at: null,
                free_runs_allowed: false,
                byok_runs_allowed: false,
              },
            }),
          );
        }
        await page.goto("/");
        const composer = page.locator(".reference-composer");
        await expect(page.getByRole("textbox")).toBeVisible();
        await page.waitForLoadState("networkidle");
        await expect(
          page.getByRole("button", { name: "Send", exact: true }),
        ).toBeInViewport({ ratio: 1 });
        await expect(
          page.getByRole("button", {
            name: "Scroll to see how Open Co-Scientist works",
          }),
        ).toBeInViewport({ ratio: 1 });
        const empty = await composer.boundingBox();
        await page
          .getByRole("button", { name: /Repurpose an approved drug/ })
          .click();
        await expect
          .poll(async () => (await composer.boundingBox())!.height)
          .toBeGreaterThan(empty!.height);
        await expect
          .poll(
            async () =>
              (await composer.boundingBox())!.y +
              (await composer.boundingBox())!.height,
          )
          .toBeCloseTo(empty!.y + empty!.height, 0);
        await expect(
          page.getByRole("button", { name: "Send", exact: true }),
        ).toBeInViewport({ ratio: 1 });
        await expect(
          page.getByRole("button", {
            name: "Scroll to see how Open Co-Scientist works",
          }),
        ).toBeInViewport({ ratio: 1 });
        expect(
          await page
            .locator(".ucs-page--home")
            .evaluate((node) => node.scrollTop),
        ).toBe(0);
        await page
          .getByRole("textbox")
          .fill("A long research goal.\n".repeat(100));
        await expect(
          page.getByRole("button", { name: "Send", exact: true }),
        ).toBeInViewport({ ratio: 1 });
        await expect
          .poll(
            async () =>
              (await composer.boundingBox())!.y +
              (await composer.boundingBox())!.height,
          )
          .toBeCloseTo(empty!.y + empty!.height, 0);
        await page.getByRole("textbox").fill("");
        await expect
          .poll(async () => (await composer.boundingBox())!.height)
          .toBeCloseTo(empty!.height, 0);
      });
    }
  }
}

for (const theme of ['light', 'dark']) {
  for (const width of [1440, 768]) {
    test(`landing trailer, joined header and panels at ${width}px in ${theme}`, async ({
      page,
    }) => {
      await page.setViewportSize({width, height: 812});
      await page.addInitScript(
        mode => localStorage.setItem('cosci-theme', mode),
        theme,
      );
      const landingRequests: string[] = [];
      page.on('request', request => {
        if (/\/home_landing[^/]*\.(?:tsx?|js)(?:\?|$)/.test(request.url())) {
          landingRequests.push(request.url());
        }
      });
      await page.goto('/');
      await expect(page.getByRole('textbox')).toBeVisible();
      await page.waitForLoadState('networkidle');
      const trailer = page.getByTitle('Open Co-Scientist trailer');
      expect(landingRequests).toEqual([]);
      await expect(trailer).toHaveCount(0);
      await page
        .getByRole('button', {name: 'Scroll to see how Open Co-Scientist works'})
        .click();
      await expect(trailer).toHaveAttribute(
        'src',
        'https://www.youtube-nocookie.com/embed/Wnhe8a8kKc0',
      );
      await expect(page.locator('iframe')).toHaveCount(1);
      await expect(page.getByRole('link', {name: 'GitHub', exact: true})).toHaveAttribute(
        'href',
        'https://github.com/guy915/Open-Co-Scientist',
      );
      await expect(page.getByRole('navigation', {name: 'Legal information'})).toHaveCount(0);
      await trailer.scrollIntoViewIfNeeded();
      const box = await trailer.boundingBox();
      expect(box!.width / box!.height).toBeCloseTo(16 / 9, 1);
      await captureViewport(page, {
        width,
        height: 812,
        name: `landing-hero-${width}-${theme}.png`,
      });
      await page.locator('#landing-overview').evaluate(node => {
        const pane = node.closest('.ucs-page--home')!;
        pane.scrollTop +=
          node.getBoundingClientRect().top - pane.getBoundingClientRect().top;
      });
      const header = page.locator('.ucs-header-action-bar');
      const tabs = page.getByRole('navigation', {name: 'Landing sections'});
      await expect(tabs).toHaveCount(1);
      await expect(tabs).toBeVisible();
      const tabBox = await tabs.boundingBox();
      const headerBox = await header.boundingBox();
      const headerBottom = headerBox!.y + headerBox!.height;
      expect(headerBox!.y).toBeGreaterThanOrEqual(0);
      expect(headerBox!.x + headerBox!.width).toBeLessThanOrEqual(width);
      if (width > 1000) {
        // Wide headers take the tabs into their own row.
        expect(tabBox!.y).toBeGreaterThanOrEqual(headerBox!.y);
        expect(tabBox!.y + tabBox!.height).toBeLessThanOrEqual(
          headerBottom + 1,
        );
      } else {
        // Narrow headers have no room, so the tabs stick right below them.
        expect(tabBox!.y).toBeGreaterThanOrEqual(headerBottom - 1);
        expect(tabBox!.y).toBeLessThan(headerBottom + 24);
      }
      const feedback = header.getByRole('button', {
        name: 'Feedback',
        exact: true,
      });
      await expect(feedback).toBeVisible();
      await expect(feedback).toBeInViewport();
      if (width > 1000) {
        const panels = await page
          .locator('.ucs-landing-ov-card > :last-child')
          .evaluateAll(nodes =>
            nodes.map(node => {
              const rect = node.getBoundingClientRect();
              return {top: rect.top, bottom: rect.bottom, height: rect.height};
            }),
          );
        for (const panel of panels.slice(1)) {
          expect(panel.top).toBeCloseTo(panels[0].top, 0);
          expect(panel.bottom).toBeCloseTo(panels[0].bottom, 0);
          expect(panel.height).toBeCloseTo(panels[0].height, 0);
        }
      }
      await captureViewport(page, {
        width,
        height: 812,
        name: `landing-overview-${width}-${theme}.png`,
      });
      await tabs.getByRole('link', {name: 'FAQ'}).click();
      await expect(page.locator('#faq')).toBeInViewport();
      await expect(tabs).toBeInViewport();
      await page.locator('.ucs-page--home').evaluate(node => {
        node.scrollTop = 0;
      });
      await expect(
        header.getByRole('navigation', {name: 'Landing sections'}),
      ).toHaveCount(0);
      await expect(page.getByRole('textbox')).toBeInViewport();
    });
  }
}

test('phones get the composer and compact feedback without landing', async ({
  page,
}) => {
  await page.setViewportSize({width: 375, height: 812});
  await page.goto('/');
  await expect(page.getByRole('textbox')).toBeInViewport();
  await expect(page.locator('.ucs-landing')).toHaveCount(0);
  await expect(
    page.getByRole('button', {name: 'Scroll to see how Open Co-Scientist works'}),
  ).toHaveCount(0);
  await expect(
    page.getByRole('navigation', {name: 'Example chats'}),
  ).toHaveCount(0);
  const header = page.locator('.ucs-header-action-bar');
  await expect(
    header.getByRole('button', {name: 'Feedback', exact: true}),
  ).toBeInViewport();
  await expect(header.getByText('Open Co-Scientist', {exact: true})).toBeVisible();
});
