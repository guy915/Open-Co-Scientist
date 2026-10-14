import {writeFile} from 'node:fs/promises';
import AxeBuilder from '@axe-core/playwright';
import type {Locator, Page} from '@playwright/test';
import {test, expect, createCompletedRun} from '../support/fixtures';
import {tabTo} from '../support/keyboard';

async function scrollWithKey(page: Page, pane: Locator, key: string) {
  // A position poll can pass while the previous native key animation still runs.
  // Observe real completion before sending the next scrolling key.
  await pane.evaluate(node => {
    node.setAttribute('data-test-scroll-ended', 'false');
    node.addEventListener(
      'scrollend',
      () => node.setAttribute('data-test-scroll-ended', 'true'),
      {once: true},
    );
  });
  await page.keyboard.press(key);
  await expect(pane).toHaveAttribute('data-test-scroll-ended', 'true');
}

for (const theme of ['light', 'dark']) {
  test(`@keyboard-progress report section landmarks are named ${theme}`, async ({page, api}, info) => {
    await page.addInitScript(mode => localStorage.setItem('cosci-theme', mode), theme);
    const id = await createCompletedRun(api, {research_goal: 'Offline report section keyboard navigation', tier: 'express'});
    await page.goto(`/runs/${id}/ideas`);
    await expect(page.getByRole('list', {name: 'Ranked hypothesis list'})).toBeVisible();
    const results = await new AxeBuilder({page}).withRules(['landmark-unique', 'landmark-complementary-is-top-level']).analyze();
    const snapshot = info.outputPath('report-sections.yml');
    await writeFile(snapshot, await page.locator('body').ariaSnapshot());
    await info.attach('report-sections-tree', {path: snapshot, contentType: 'text/yaml'});
    const screenshot = info.outputPath('report-sections.png');
    await page.screenshot({path: screenshot});
    await info.attach('report-sections', {path: screenshot, contentType: 'image/png'});
    expect(results.violations).toEqual([]);
    const navigation = page.getByRole('navigation', {name: 'Hypothesis sections'});
    if (info.project.name !== 'webkit-iphone') {
      await expect(navigation).toBeVisible();
      const link = navigation.getByRole('link').first();
      await tabTo(page, link);
      await expect(link).toBeFocused();
      await page.keyboard.press('Enter');
    }
  });

  test(`@keyboard-progress active activity pane scrolls with arrows ${theme}`, async ({page, api}, info) => {
    await page.addInitScript(mode => localStorage.setItem('cosci-theme', mode), theme);
    const id = await createCompletedRun(api, {research_goal: 'Offline keyboard progress history', tier: 'express'});
    await page.route(`**/api/runs/${id}`, async route => {
      const response = await route.fetch();
      await route.fulfill({json: {...await response.json(), status: 'running'}});
    });
    // Keep ten distinct committed activity groups in the production pane.
    const body = Array.from({length: 10}, (_, index) => 'data: ' + JSON.stringify({
      seq: index + 1,
      type: 'scientific_task',
      payload: {activity: index % 2 ? 'drafting' : 'review', message: `Committed activity ${index + 1}: matched controls and reproducible scientific evidence.`},
      created_at: Date.now() / 1000,
    }) + '\n\n').join('');
    await page.route(`**/api/runs/${id}/events**`, route => route.fulfill({contentType: 'text/event-stream', body}));
    await page.goto(`/runs/${id}/details`);
    await expect(page.getByText('Committed activity 10:', {exact: false})).toBeVisible();
    const pane = page.locator('div.min-h-0.overflow-auto.px-8');
    await expect.poll(() => pane.evaluate(node => node.scrollHeight > node.clientHeight)).toBe(true);
    const snapshot = info.outputPath('progress-scroll.yml');
    await writeFile(snapshot, await page.locator('body').ariaSnapshot());
    await info.attach('progress-scroll-tree', {path: snapshot, contentType: 'text/yaml'});
    const screenshot = info.outputPath('progress-scroll.png');
    await page.screenshot({path: screenshot});
    await info.attach('progress-scroll', {path: screenshot, contentType: 'image/png'});
    await expect(page.getByText('Reconnecting...', {exact: true})).toBeVisible();
    await page.locator('.ui-motion-enter-items > li').evaluateAll(nodes => {
      for (const node of nodes) {
        // Restart the actual entry animation and inspect a fixed entry frame.
        const style = (node as HTMLElement).style;
        style.animation = 'none';
        void (node as HTMLElement).offsetWidth;
        style.animation = '';
        for (const animation of node.getAnimations()) {
          animation.pause();
          const timing = animation.effect!.getTiming();
          animation.currentTime = (timing.delay ?? 0) + Number(timing.duration) * 0.2;
        }
      }
    });
    const entryImage = info.outputPath('progress-entry-contrast.png');
    await page.screenshot({path: entryImage});
    await info.attach('progress-entry-contrast', {path: entryImage, contentType: 'image/png'});
    const results = await new AxeBuilder({page}).withRules(['scrollable-region-focusable', 'color-contrast']).analyze();
    const contrast = info.outputPath('progress-contrast.json');
    await writeFile(contrast, JSON.stringify(results.violations, null, 2));
    await info.attach('progress-contrast', {path: contrast, contentType: 'application/json'});
    expect.soft(results.violations).toEqual([]);
    await page.locator('.ui-motion-enter-items > li').evaluateAll(nodes => {
      for (const node of nodes) {
        for (const animation of node.getAnimations()) animation.finish();
      }
    });
    await expect(pane).toHaveAttribute('tabindex', '0');
    await expect(pane).toHaveAttribute('role', 'region');
    await expect(pane).toHaveAttribute('aria-label', 'Research progress and activity');
    await tabTo(page, pane);
    await expect(pane).toBeFocused();
    const focusImage = info.outputPath('progress-scroll-focused.png');
    await page.screenshot({path: focusImage});
    await info.attach('progress-scroll-focused', {path: focusImage, contentType: 'image/png'});
    const initial = await pane.evaluate(node => node.scrollTop);
    await scrollWithKey(page, pane, 'ArrowDown');
    await expect.poll(() => pane.evaluate(node => node.scrollTop)).toBeGreaterThan(initial);
    await scrollWithKey(page, pane, 'End');
    await expect.poll(() => pane.evaluate(node => node.scrollTop + node.clientHeight)).toBeGreaterThanOrEqual(await pane.evaluate(node => node.scrollHeight - 1));
    await scrollWithKey(page, pane, 'Home');
    await expect.poll(() => pane.evaluate(node => node.scrollTop)).toBe(0);
  });
}
