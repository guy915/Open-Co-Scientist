import { captureViewport, expect, test } from "../support/fixtures";

const DESKTOP = { width: 1440, height: 900 };

for (const theme of ["light", "dark"]) {
  test(`the header status tag matches the header buttons and recents sit close together in ${theme}`, async ({
    page,
  }) => {
    await page.setViewportSize(DESKTOP);
    await page.addInitScript(
      (mode) => localStorage.setItem("cosci-theme", mode),
      theme,
    );
    await page.goto("/");
    const header = page.locator(".ucs-header-action-bar");
    const status = header
      .getByRole("status")
      .filter({ hasText: "Offline mode" });
    const feedback = header.getByRole("button", { name: /Feedback/ });
    await expect(status).toBeVisible();
    await expect(feedback).toBeVisible();
    const measure = (locator: typeof status) =>
      locator.evaluate((el) => {
        const box = el.getBoundingClientRect();
        const style = getComputedStyle(el);
        return {
          top: box.top,
          height: box.height,
          fontSize: style.fontSize,
          fontWeight: style.fontWeight,
        };
      });
    const [tag, button] = await Promise.all([
      measure(status),
      measure(feedback),
    ]);
    expect(Math.abs(tag.height - button.height)).toBeLessThanOrEqual(1);
    expect(Math.abs(tag.top - button.top)).toBeLessThanOrEqual(1);
    expect(tag.fontSize).toBe(button.fontSize);
    expect(tag.fontWeight).toBe(button.fontWeight);

    const cards = page.locator(".reference-recent-card");
    await expect(cards.nth(1)).toBeVisible();
    // The list rises in; measure once it has settled.
    await expect(async () => {
      const [first, second] = await Promise.all([
        cards.nth(0).boundingBox(),
        cards.nth(1).boundingBox(),
      ]);
      expect(second!.y - (first!.y + first!.height)).toBeCloseTo(28, 0);
    }).toPass();
    await captureViewport(page, {
      ...DESKTOP,
      name: `header-status-recents-${theme}.png`,
    });
  });
}

test("on tablets the status tag keeps the button height and shows only its icon", async ({
  page,
}) => {
  await page.setViewportSize({ width: 768, height: 844 });
  await page.goto("/");
  const header = page.locator(".ucs-header-action-bar");
  const status = header.getByRole("status").filter({ hasText: "Offline mode" });
  const feedback = header.getByRole("button", { name: /Feedback/ });
  await expect(status).toBeVisible();
  const [tag, button] = await Promise.all([
    status.boundingBox(),
    feedback.boundingBox(),
  ]);
  expect(Math.abs(tag!.height - button!.height)).toBeLessThanOrEqual(1);
  expect(tag!.width).toBeLessThan(tag!.height * 1.25);
});
