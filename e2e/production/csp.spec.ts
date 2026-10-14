import { readFileSync } from "node:fs";
import { createCompletedRun, expect, test } from "../support/fixtures";
import { FRONTEND_DIR, API_URL } from "../support/paths";

const policy = readFileSync(`${FRONTEND_DIR}/public/_headers`, "utf8")
  .split("Content-Security-Policy: ")[1]
  .trim();

for (const theme of ["light", "dark"]) {
  test(`production pages load under the Cloudflare CSP in ${theme}`, async ({
    page,
    api,
  }) => {
    test.setTimeout(120_000);
    const violations: string[] = [];
    const errors: string[] = [];
    const imageRequests: string[] = [];
    page.on("request", (request) => {
      if (request.url().startsWith("https://tracker.example/")) {
        imageRequests.push(request.url());
      }
    });
    page.on("console", (message) => {
      if (
        /content.security.policy|violates.*directive|refused to/i.test(
          message.text(),
        )
      ) {
        errors.push(message.text());
      }
    });
    await page.exposeFunction("recordCspViolation", (value: string) => {
      violations.push(value);
    });
    await page.addInitScript((mode) => {
      localStorage.setItem("cosci-theme", mode);
      document.addEventListener("securitypolicyviolation", (event) => {
        void (
          window as unknown as {
            recordCspViolation: (value: string) => Promise<void>;
          }
        ).recordCspViolation(
          `${event.effectiveDirective}: ${event.blockedURI}`,
        );
      });
    }, theme);
    // Exercise the SDK without sending test events to a real Sentry project.
    await page.route(
      /^https:\/\/[^/]+\.ingest(?:\.us|\.de)?\.sentry\.io\//,
      (route) => route.fulfill({ status: 200, body: "{}" }),
    );
    await page.route(/\/api\/interviews\/[^/]+$/, async (route) => {
      const response = await route.fetch();
      const body = await response.json();
      const turn = body.turns?.find(
        (item: { role: string }) => item.role === "agent",
      );
      if (turn) {
        turn.content +=
          "\n\nH<sub>2</sub>O and \\(x_1^2\\).\n\n\\[\nx^2\n\ny^2\n\\]\n\n![S4 study diagram](https://tracker.example/pixel?private=research)";
      }
      await route.fulfill({ response, json: body });
    });
    const id = await createCompletedRun(api, {
      research_goal: "Offline CSP check of enzyme stability",
      tier: "express",
    });
    const [example] = await api.listDemoRuns();
    const routes = [
      "/",
      "/runs",
      "/runs/new",
      `/runs/${id}`,
      ...["details", "learning", "overview", "ideas"].map(
        (tab) => `/runs/${id}/${tab}`,
      ),
      `/examples/${example.id}`,
      "/operations",
      "/operations/spend",
      "/missing-page",
    ];
    for (const path of routes) {
      const response = await page.goto(path);
      expect(response?.headers()["content-security-policy"], path).toBe(
        policy.replace("connect-src", `connect-src ${API_URL}`),
      );
      await expect(page.locator("html")).toHaveAttribute("data-theme", theme);
      await expect(page.locator("main")).toBeVisible();
      await page.evaluate(() => document.fonts.ready);
      await expect(page.locator('[aria-busy="true"]')).toHaveCount(0);
      if (path === "/") {
        const png = await page.evaluate(() => {
          const canvas = document.createElement("canvas");
          canvas.width = 1;
          canvas.height = 1;
          return canvas.toDataURL("image/png").split(",")[1];
        });
        const name = "S4 local study image.png";
        await page.getByLabel("Upload files").setInputFiles({
          name,
          mimeType: "image/png",
          buffer: Buffer.from(png, "base64"),
        });
        const preview = page.getByRole("img", { name, exact: true });
        await expect(preview).toHaveAttribute("src", /^blob:/);
        await expect
          .poll(() =>
            preview.evaluate((image: HTMLImageElement) => image.naturalWidth),
          )
          .toBe(1);
        await page
          .getByRole("button", { name: `Remove ${name}`, exact: true })
          .click();
        await expect(preview).toHaveCount(0);
        await page.getByRole("button", {
          name: "Scroll to see how Open Co-Scientist works",
        }).click();
        const trailer = page.getByTitle("Open Co-Scientist trailer");
        await trailer.scrollIntoViewIfNeeded();
        await expect(page.frameLocator('iframe[title="Open Co-Scientist trailer"]')
          .getByText("Trailer player")).toBeVisible();
      }
      if (path === "/operations") {
        await expect(
          page.getByRole("heading", { name: "Launch operations", exact: true }),
        ).toBeVisible();
        await expect(page.getByLabel("Operator token")).toBeVisible();
      }
      if (path === "/operations/spend") {
        await expect(
          page.getByRole("heading", { name: "Model spend", exact: true }),
        ).toBeVisible();
        const token = "synthetic-csp-operator-token";
        let spendRequests = 0;
        await page.route(`${API_URL}/api/spend`, async (route) => {
          spendRequests++;
          expect(route.request().headers()["x-logs-token"]).toBe(token);
          await route.fulfill({
            status: 200,
            contentType: "application/json",
            headers: { "Cache-Control": "no-store" },
            json: {
              azure: {
                available: true,
                today_eur: 1,
                week_eur: 2,
                total_spent_eur: 3,
                reserved_eur: 4,
                run_forecasts_eur: 5,
                total_budget_eur: 100,
                remaining_eur: 88,
                burn_eur_per_day: 1,
                days_at_current_rate: 88,
              },
              anthropic: {
                available: true,
                cycle_spent_usd: 0,
                reserved_usd: 0,
                grant_usd: 100,
                remaining_credit_usd: 100,
                usable_allowance_usd: 95,
                reset_at: null,
              },
              cache_by_role: [],
            },
          });
        });
        const load = page.getByRole("button", { name: "Load spend" });
        await expect(load).toBeDisabled();
        await page.getByLabel("Operator token").fill(token);
        expect(spendRequests).toBe(0);
        await load.click();
        await expect(
          page.getByRole("heading", { name: "Azure", exact: true }),
        ).toBeVisible();
        expect(spendRequests).toBe(1);
        expect(
          await page.evaluate(() => [
            ...Object.values(localStorage),
            ...Object.values(sessionStorage),
          ]),
        ).not.toContain(token);
        await page.getByLabel("Operator token").fill("");
        await expect(
          page.getByRole("heading", { name: "Azure", exact: true }),
        ).toHaveCount(0);
      }
      if (path.startsWith("/examples/")) {
        await expect(page).toHaveURL(path);
        await expect(
          page.getByRole("button", { name: "Open example", exact: true }),
        ).toBeVisible();
        await page
          .getByRole("button", { name: "Open example", exact: true })
          .click();
        await expect(page).toHaveURL(/\/chats\//);
        await page.reload();
        await expect(
          page.getByLabel("Started research session"),
        ).toBeAttached();
        await expect(
          page.getByText(
            "S4 study diagram (https://tracker.example/pixel?private=research)",
          ),
        ).toBeAttached();
      }
      if (path.startsWith("/examples/")) {
        await expect(page.locator(".katex-html").first()).toBeAttached();
      }
      expect(violations, path).toEqual([]);
      expect(errors, path).toEqual([]);
      expect(imageRequests, path).toEqual([]);
    }
    for (const region of [
      "ingest.sentry.io",
      "ingest.us.sentry.io",
      "ingest.de.sentry.io",
    ]) {
      const url = `https://s4.${region}/api/0/envelope/`;
      await page.route(url, (route) =>
        route.fulfill({
          status: 200,
          body: "{}",
          headers: { "Access-Control-Allow-Origin": "*" },
        }),
      );
      const status = await page.evaluate(
        async (endpoint) =>
          (await fetch(endpoint, { method: "POST", body: "{}" })).status,
        url,
      );
      expect(status, region).toBe(200);
    }
    expect(violations).toEqual([]);
    expect(errors).toEqual([]);
  });
}
