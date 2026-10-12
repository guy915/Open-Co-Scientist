import { expect, test } from "../support/fixtures";
import { tabTo } from "../support/keyboard";

for (const theme of ["light", "dark"]) {
  test(`@keyboard-menu Settings and model choices in ${theme}`, async ({
    page,
  }, info) => {
    await page.addInitScript(
      (mode) => localStorage.setItem("cosci-theme", mode),
      theme,
    );
    await page.goto("/");
    await expect(page.getByRole("textbox")).toBeVisible();
    const navigation = page.getByRole("button", {
      name: "Open navigation",
      exact: true,
    });
    if (await navigation.isVisible()) {
      await tabTo(page, navigation);
      await page.keyboard.press("Enter");
    }
    const trigger = page.getByRole("button", { name: "Settings", exact: true });
    await tabTo(page, trigger);
    await page.keyboard.press("Enter");
    const menu = page.getByRole("menu", { name: "Settings", exact: true });
    await expect(
      menu.getByRole("menuitem", { name: "Appearance" }),
    ).toBeFocused();
    const screenshot = info.outputPath("menu-focused.png");
    await page.screenshot({ path: screenshot });
    await info.attach("menu-focused", {
      path: screenshot,
      contentType: "image/png",
    });
    await page.keyboard.press("ArrowDown");
    await expect(menu.getByRole("menuitem", { name: "Model" })).toBeFocused();
    await page.keyboard.press("ArrowDown");
    await expect(
      menu.getByRole("menuitem", { name: "Appearance" }),
    ).toBeFocused();
    await page.keyboard.press("ArrowUp");
    await expect(menu.getByRole("menuitem", { name: "Model" })).toBeFocused();
    await page.keyboard.press("Escape");
    await expect(menu).not.toBeVisible();
    await expect(trigger).toBeFocused();
    await page.keyboard.press("Space");
    await expect(
      menu.getByRole("menuitem", { name: "Appearance" }),
    ).toBeFocused();
    await page.keyboard.press("End");
    await expect(menu.getByRole("menuitem", { name: "Model" })).toBeFocused();
    await page.keyboard.press("Home");
    await expect(
      menu.getByRole("menuitem", { name: "Appearance" }),
    ).toBeFocused();
    await page.keyboard.press("ArrowDown");
    await expect(menu.getByRole("menuitem", { name: "Model" })).toBeFocused();
    await page.keyboard.press("Enter");
    const dialog = page.getByRole("dialog", { name: "Settings", exact: true });
    await expect(dialog).toBeVisible();
    await tabTo(page, dialog.locator('button[aria-haspopup="menu"]').first());
    const select = dialog.locator('button[aria-haspopup="menu"]').first();
    await page.keyboard.press("Enter");
    const choices = dialog.getByRole("menu");
    const selected = choices.locator('[aria-checked="true"]');
    await expect(selected).toBeFocused();
    await page.keyboard.press("ArrowDown");
    await expect(choices.locator(":focus")).toHaveAttribute(
      "role",
      "menuitemradio",
    );
    await page.keyboard.press("Escape");
    await expect(choices).not.toBeVisible();
    await expect(dialog).toBeVisible();
    await expect(select).toBeFocused();
    await page.keyboard.press("Enter");
    await page.keyboard.press("Tab");
    await expect(choices).not.toBeVisible();
    expect(
      await page
        .locator(":focus")
        .evaluate((node) => Boolean(node.closest('[role="dialog"]'))),
    ).toBe(true);
    await tabTo(page, select);
    await expect(select).toBeFocused();
    await page.keyboard.press("Escape");
    await expect(dialog).not.toBeVisible();
    await expect(
      (await navigation.isVisible()) ? navigation : trigger,
    ).toBeFocused();
    await info.attach("keyboard-menu-after", {
      body: await page.screenshot(),
      contentType: "image/png",
    });
  });
}
