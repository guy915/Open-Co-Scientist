import { captureViewport, expect, test } from "../support/fixtures";

const BUNDLE = /\/assets\/.*\.js$/;
const ROUTES = [
  ["/", "home"],
  ["/chats/boot-check", "chat"],
  ["/runs/boot-check/details", "report"],
] as const;
const WORKSPACE = { light: "rgb(255, 255, 255)", dark: "rgb(19, 19, 20)" };

for (const theme of ["light", "dark"] as const) {
  for (const viewport of [
    { width: 390, height: 844 },
    { width: 1440, height: 900 },
  ]) {
    test(`the boot skeleton paints the stored ${theme} theme and the route's layout at ${viewport.width}px`, async ({
      page,
    }) => {
      const violations: string[] = [];
      await page.exposeFunction("recordCspViolation", (value: string) => {
        violations.push(value);
      });
      // The operating system prefers the other theme; the stored choice wins.
      await page.emulateMedia({
        colorScheme: theme === "dark" ? "light" : "dark",
      });
      await page.setViewportSize(viewport);
      await page.addInitScript((mode) => {
        localStorage.setItem("cosci-theme", mode);
        document.addEventListener("securitypolicyviolation", (event) => {
          void (
            window as unknown as {
              recordCspViolation: (value: string) => Promise<void>;
            }
          ).recordCspViolation(event.effectiveDirective);
        });
      }, theme);
      await page.route(BUNDLE, (route) => route.abort());

      for (const [path, view] of ROUTES) {
        await page.goto(path);
        await expect(page.locator("html")).toHaveAttribute("data-theme", theme);
        const shown = page.locator(".boot-view:visible");
        await expect(shown).toHaveCount(1);
        await expect(shown).toHaveClass(new RegExp(`boot-${view}`));
        await expect(page.locator(".boot-workspace")).toHaveCSS(
          "background-color",
          WORKSPACE[theme],
        );
        await expect(page.locator(".boot-rail")).toBeVisible({
          visible: viewport.width > 700,
        });
        if (view === "home") {
          await expect(page.locator(".boot-recents")).toBeVisible({
            visible: viewport.width > 1180,
          });
        }
        await captureViewport(page, {
          ...viewport,
          name: `boot-skeleton-${view}-${viewport.width}-${theme}.png`,
        });
      }

      await page.unroute(BUNDLE);
      await page.goto("/");
      await expect(page.locator("textarea").last()).toBeVisible();
      await expect(page.locator(".boot")).toHaveCount(0);
      await expect(page.locator('[aria-busy="true"]')).toHaveCount(0);
      expect(violations).toEqual([]);
    });
  }
}
