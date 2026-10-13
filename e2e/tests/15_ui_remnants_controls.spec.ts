import {type Page} from '@playwright/test';
import {captureViewport, expect, test} from '../support/fixtures';

const PHONE = {width: 390, height: 844};
const DESKTOP = {width: 1440, height: 900};

async function openSettings(page: Page, phone: boolean): Promise<void> {
  if (phone) {
    await page.getByRole('button', {name: 'Open navigation'}).click();
  }
  await page.getByRole('button', {name: 'Settings', exact: true}).click();
  await page.getByRole('menuitem', {name: /Model/}).click();
}

async function startResearchSession(page: Page): Promise<string> {
  await page.goto('/');
  const composer = page.getByRole('textbox').last();
  await composer.fill('Which checkpoints govern ferroptosis escape in glioma?');
  await composer.press('Enter');
  const start = page.getByRole('button', {name: 'Start research'});
  const turns = page.getByRole('button', {name: 'Copy response'});
  await expect(turns.first()).toBeVisible({timeout: 30_000});
  for (const answer of ['Lipid repair.', 'Organoids.', 'Patients.', 'None.']) {
    if (await start.isVisible().catch(() => false)) break;
    const prior = await turns.count();
    await page.getByRole('textbox').last().fill(answer);
    await page.getByRole('button', {name: 'Send', exact: true}).click();
    await expect(async () => {
      const ready = await start.isVisible().catch(() => false);
      expect(ready || (await turns.count()) > prior).toBeTruthy();
    }).toPass({timeout: 30_000});
  }
  await start.click();
  const card = page.locator('.reference-started-session-card');
  await expect(card).toBeVisible({timeout: 30_000});
  return (await card.getAttribute('href'))!.split('/')[2];
}

for (const theme of ['light', 'dark']) {
  test.describe(`shared controls in ${theme}`, () => {
    test.beforeEach(async ({page}) => {
      await page.addInitScript(
        mode => localStorage.setItem('cosci-theme', mode),
        theme,
      );
    });

    for (const [name, viewport] of [
      ['phone', PHONE],
      ['desktop', DESKTOP],
    ] as const) {
      test(`settings tabs and selects keep their edges at ${name} width`, async ({
        page,
      }) => {
        await page.setViewportSize(viewport);
        await page.goto('/');
        await openSettings(page, name === 'phone');
        const dialog = page.getByRole('dialog');
        const firstTab = dialog.getByRole('navigation').getByRole('button');
        const title = dialog.getByRole('heading', {name: 'Settings'});
        // The dialog scales in; measure once it has settled.
        await expect(async () => {
          const tabBox = (await firstTab.first().boundingBox())!;
          const titleBox = (await title.boundingBox())!;
          expect(Math.abs(tabBox.x - titleBox.x)).toBeLessThanOrEqual(1);
        }).toPass();

        const trigger = dialog.locator('button[aria-haspopup="menu"]').first();
        await trigger.click();
        const menu = page.getByRole('menu').last();
        await expect(menu).toBeVisible();
        // A model ID longer than the field must ellipsize, not widen the menu.
        const option = menu.getByRole('menuitemradio').last();
        await option.evaluate(el => {
          el.querySelector('span')!.textContent =
            'openrouter/provider/an-extremely-long-custom-model-identifier-preview';
        });
        const triggerBox = (await trigger.boundingBox())!;
        // The menu scales in; measure once it has settled.
        await expect(async () => {
          const menuBox = (await menu.boundingBox())!;
          expect(Math.abs(menuBox.width - triggerBox.width)).toBeLessThanOrEqual(
            1,
          );
          expect(Math.abs(menuBox.x - triggerBox.x)).toBeLessThanOrEqual(1);
        }).toPass();
        expect(
          await option
            .locator('span')
            .first()
            .evaluate(el => el.scrollWidth > el.clientWidth),
        ).toBe(true);
        expect(
          await menu.evaluate(el => el.scrollWidth <= el.clientWidth),
        ).toBe(true);
        await captureViewport(page, {
          ...viewport,
          name: `ui-remnants-select-${name}-${theme}.png`,
        });
      });
    }

    test('session actions share hover feedback and entry points choose their view', async ({
      page,
    }) => {
      await page.setViewportSize(DESKTOP);
      const runId = await startResearchSession(page);
      const chatUrl = page.url();
      const details = page.getByRole('link', {name: 'View session details'});
      const newTopic = page.getByRole('button', {
        name: 'Start a new research goal session on a new topic',
      });
      const hoverColour = await details.evaluate(el => {
        const probe = document.createElement('span');
        probe.style.backgroundColor = 'var(--button-accent-hover)';
        el.append(probe);
        const colour = getComputedStyle(probe).backgroundColor;
        probe.remove();
        return colour;
      });
      for (const action of [details, newTopic]) {
        await page.mouse.move(0, 0);
        await expect(action).toHaveCSS('background-color', 'rgba(0, 0, 0, 0)');
        await action.hover();
        await expect(action).toHaveCSS('background-color', hoverColour);
      }
      const send = page.getByRole('button', {name: 'Send', exact: true});
      await expect(send).toBeDisabled();
      await page.mouse.move(0, 0);
      const disabledColour = await send.evaluate(el => getComputedStyle(el).backgroundColor);
      await send.hover({force: true});
      await expect(send).toHaveCSS('background-color', disabledColour);

      // Old saved preferences must not override the entry point's destination.
      for (const side of ['chat', 'results']) {
        await page.evaluate(({runId, side}) => {
          localStorage.setItem('cosci:session-side', side);
          localStorage.setItem(`cosci:session-side:${runId}`, side);
        }, {runId, side});
        await page.getByRole('link', {name: 'Go to Open Co-Scientist home', exact: true}).click();
        const recentLink = page.locator(`.reference-recent-card[href="/runs/${runId}/details"]`);
        await recentLink.click();
        await expect(page).toHaveURL(new RegExp(`/runs/${runId}/details$`));
        await expect(page.getByRole('navigation', {name: 'Session view'})
          .getByRole('link', {name: 'Results'})).toHaveAttribute('aria-current', 'page');
        const menu = page.getByRole('button', {name: 'Menu', exact: true});
        if (await menu.getAttribute('aria-expanded') !== 'true') await menu.click();
        const sidebar = page.getByRole('complementary', {name: 'Primary navigation'});
        await sidebar.locator(`a[href="${new URL(chatUrl).pathname}"]`).click();
        await expect(page).toHaveURL(chatUrl);
        await expect(page.getByRole('navigation', {name: 'Session view'})
          .getByRole('link', {name: 'Chat', exact: true})).toHaveAttribute('aria-current', 'page');
      }
    });

    test('header tooltips open below and the session switch hover fills its slot', async ({
      page,
    }) => {
      await page.setViewportSize(DESKTOP);
      const runId = await startResearchSession(page);
      await page.goto(`/runs/${runId}/details`);
      const header = page.locator('.ucs-header-action-bar');
      const anchors = header.locator('[data-tooltip]');
      await expect(anchors.first()).toBeVisible();
      const placements = await anchors.evaluateAll(els =>
        els
          .filter(el => (el as HTMLElement).offsetParent !== null)
          .map(el => {
            const tip = getComputedStyle(el, '::after');
            return {
              tip: el.getAttribute('data-tooltip'),
              top: parseFloat(tip.top),
              height: el.getBoundingClientRect().height,
            };
          }),
      );
      expect(placements.map(p => p.tip)).toContain('Home');
      for (const placement of placements) {
        expect(placement.top, placement.tip ?? '').toBeGreaterThan(
          placement.height,
        );
      }

      const sessionSwitch = page.getByRole('navigation', {name: 'Session view'});
      const chat = sessionSwitch.getByRole('link', {name: 'Chat'});
      await chat.hover();
      // Pixel colour where the hovered segment meets the selected thumb: the
      // hover fill, not the darker track, must show there.
      await page.waitForTimeout(400);
      const box = (await chat.boundingBox())!;
      const shot = await page.screenshot({
        clip: {x: box.x, y: box.y, width: box.width, height: box.height},
      });
      const colours = await page.evaluate(
        async ({png, inner, seam}) => {
          const image = new Image();
          image.src = `data:image/png;base64,${png}`;
          await image.decode();
          const canvas = document.createElement('canvas');
          canvas.width = image.width;
          canvas.height = image.height;
          const context = canvas.getContext('2d')!;
          context.drawImage(image, 0, 0);
          const scale = image.width / inner.width;
          const at = (x: number, y: number) =>
            Array.from(
              context.getImageData(Math.round(x * scale), Math.round(y * scale), 1, 1)
                .data,
            ).slice(0, 3);
          return {inner: at(inner.x, inner.y), seam: at(seam.x, seam.y)};
        },
        {
          png: shot.toString('base64'),
          inner: {x: box.width / 2, y: 3, width: box.width},
          seam: {x: box.width - 1.5, y: 3},
        },
      );
      for (let channel = 0; channel < 3; channel++) {
        expect(
          Math.abs(colours.inner[channel] - colours.seam[channel]),
        ).toBeLessThanOrEqual(6);
      }
      await captureViewport(page, {
        ...DESKTOP,
        name: `ui-remnants-switch-hover-${theme}.png`,
      });
    });
  });
}
