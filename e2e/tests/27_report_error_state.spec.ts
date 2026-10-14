import { captureViewport, expect, test } from "../support/fixtures";

for (const theme of ["light", "dark"]) {
  for (const viewport of [
    { width: 390, height: 844 },
    { width: 1440, height: 900 },
  ]) {
    test(`an unknown run shows a compact error and no empty report at ${viewport.width}px in ${theme}`, async ({
      page,
    }) => {
      await page.setViewportSize(viewport);
      await page.addInitScript(
        (mode) => localStorage.setItem("cosci-theme", mode),
        theme,
      );
      await page.goto("/runs/no-such-run/details");
      const alert = page.locator(".cosci-report-alert");
      await expect(alert).toBeVisible();
      const box = (await alert.boundingBox())!;
      expect(box.height).toBeLessThan(120);
      await expect(
        page.getByRole("link", { name: /Goal Details|Details/ }),
      ).toHaveCount(0);
      await expect(page.getByText("Run Specifications")).toHaveCount(0);
      await expect(page.getByText("Loading…")).toHaveCount(0);
      await captureViewport(page, {
        ...viewport,
        name: `report-error-${viewport.width}-${theme}.png`,
      });
    });
  }
}
