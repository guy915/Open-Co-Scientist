import { type Locator, type Page, expect } from "@playwright/test";

export async function tabTo(page: Page, target: Locator): Promise<void> {
  await expect(target).toBeVisible();
  for (let step = 0; step < 80; step++) {
    if (await target.evaluate((node) => node === document.activeElement))
      return;
    const backwards = await target.evaluate(
      (node) =>
        !document.hasFocus() ||
        Boolean(
          node.compareDocumentPosition(document.activeElement!) &
          Node.DOCUMENT_POSITION_FOLLOWING,
        ),
    );
    await page.keyboard.press(backwards ? "Shift+Tab" : "Tab");
  }
  throw new Error(
    `Tab did not reach ${(await target.getAttribute("aria-label")) ?? (await target.textContent())}`,
  );
}
