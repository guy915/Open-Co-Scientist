import { captureViewport, expect, test } from "../support/fixtures";

const BUNDLE = /\/assets\/.*\.js$/;
const ROUTES = [
  ["/", "home"],
  ["/chats/boot-check", "chat"],
  ["/runs/boot-check/details", "report"],
] as const;
const PRIVATE_PATHS = new Set<string>(
  ROUTES.flatMap(([path]) => (path === "/" ? [] : [path])),
);
const WORKSPACE = { light: "rgb(255, 255, 255)", dark: "rgb(19, 19, 20)" };

for (const theme of ["light", "dark"] as const) {
  for (const viewport of [
    { width: 390, height: 844 },
    { width: 1440, height: 900 },
  ]) {
    test(`the stored ${theme} theme paints the routes at ${viewport.width}px`, async ({
      page,
    }) => {
      const violations: string[] = [];
      let minimumHomeContrast = Number.POSITIVE_INFINITY;
      let homeContrastFailure = "";
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
      await page.route(
        (url) => PRIVATE_PATHS.has(url.pathname),
        async (route) => {
          if (!route.request().isNavigationRequest()) return route.continue();
          const response = await route.fetch({
            url: new URL("/private.html", route.request().url()).href,
          });
          await route.fulfill({ response });
        },
      );

      for (const [path, view] of ROUTES) {
        const response = await page.goto(path);
        expect(new URL(page.url()).pathname).toBe(path);
        await expect(page.locator("html")).toHaveAttribute("data-theme", theme);
        if (view === "home") {
          const main = page.locator("#root > main");
          await expect(main).toBeVisible();
          await expect(
            main.getByRole("heading", {
              name: "Open Co-Scientist",
              exact: true,
            }),
          ).toBeVisible();
          await expect(
            main.getByRole("heading", {
              name: "Curated examples",
              exact: true,
            }),
          ).toBeVisible();
          await expect(page.locator(".boot")).toHaveCount(0);
          await expect(page.locator("body")).toHaveCSS(
            "background-color",
            WORKSPACE[theme],
          );

          const examples = main.locator('a[href^="/examples/"]');
          await expect(examples).not.toHaveCount(0);
          const exampleCount = await examples.count();
          for (let index = 0; index < exampleCount; index++) {
            const link = examples.nth(index);
            await expect(link).toBeVisible();
            await expect(link).not.toBeEmpty();
            await expect(link).toHaveAttribute(
              "href",
              /^\/examples\/[A-Za-z0-9_-]+$/,
            );
          }

          const contrastDetails = await main.evaluate((content) => {
            const luminance = (color: string) => {
              const channels = color
                .match(/[\d.]+/g)
                ?.slice(0, 3)
                .map(Number);
              if (!channels || channels.length !== 3) {
                throw new Error(`Cannot read computed color: ${color}`);
              }
              const [red, green, blue] = channels.map((channel) => {
                const normalized = channel / 255;
                return normalized <= 0.04045
                  ? normalized / 12.92
                  : ((normalized + 0.055) / 1.055) ** 2.4;
              });
              return 0.2126 * red + 0.7152 * green + 0.0722 * blue;
            };
            const backgroundColor = getComputedStyle(
              document.body,
            ).backgroundColor;
            const background = luminance(backgroundColor);
            return Array.from(
              content.querySelectorAll("h1, h2, p, a"),
              (item) => {
                const foregroundColor = getComputedStyle(item).color;
                const foreground = luminance(foregroundColor);
                const lighter = Math.max(foreground, background);
                const darker = Math.min(foreground, background);
                return {
                  selector: `#root > main ${item.tagName.toLowerCase()}`,
                  element: item.outerHTML,
                  foregroundColor,
                  backgroundColor,
                  ratio: (lighter + 0.05) / (darker + 0.05),
                };
              },
            );
          });
          expect(contrastDetails.length).toBeGreaterThan(0);
          const lowestContrast = contrastDetails.reduce((lowest, item) =>
            item.ratio < lowest.ratio ? item : lowest,
          );
          minimumHomeContrast = lowestContrast.ratio;
          homeContrastFailure = `${lowestContrast.selector}: ${lowestContrast.foregroundColor} on ${lowestContrast.backgroundColor} (${lowestContrast.ratio.toFixed(2)}:1); ${lowestContrast.element}`;
        } else {
          expect(response?.headers()["content-security-policy"]).toContain(
            "script-src",
          );
          await expect(page.locator('meta[name="robots"]')).toHaveAttribute(
            "content",
            "noindex, nofollow",
          );
          await expect(page.locator('meta[name="googlebot"]')).toHaveAttribute(
            "content",
            "noindex, nofollow",
          );
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
        }
        await captureViewport(page, {
          ...viewport,
          name: `${view === "home" ? "prerendered-home" : `boot-skeleton-${view}`}-${viewport.width}-${theme}.png`,
        });
      }

      await page.unroute(BUNDLE);
      await page.goto("/");
      await expect(page.locator("textarea").last()).toBeVisible();
      await expect(page.locator(".boot")).toHaveCount(0);
      await expect(page.locator('[aria-busy="true"]')).toHaveCount(0);
      expect(violations).toEqual([]);
      expect(minimumHomeContrast, homeContrastFailure).toBeGreaterThanOrEqual(
        4.5,
      );
    });
  }
}
