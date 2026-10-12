import type { Page } from "@playwright/test";
import { expect, test } from "../support/fixtures";

test("operator applies the loaded revision and receives a conflict without storing the token", async ({
  page,
}) => {
  let updates = 0;
  const control = {
    paused: false,
    drain: false,
    message: "Research is temporarily paused.",
    resumes_at: null,
    revision: 7,
    backup: {
      enabled: true,
      verification: { status: "failed", verified_at: 1700000000 },
    },
  };
  await page.route("**/api/launch-control", async (route) => {
    expect(route.request().headers()["x-logs-token"]).toBe(
      "synthetic-page-operator",
    );
    if (route.request().method() === "GET") {
      await route.fulfill({ json: control });
    } else {
      expect(route.request().postDataJSON()).toMatchObject({
        paused: true,
        drain: false,
        expected_revision: 7,
        resumes_at: null,
      });
      updates++;
      await route.fulfill({
        status: 409,
        json: { detail: "Control changed; reload before updating" },
      });
    }
  });
  await page.goto("/operations");
  await page.getByLabel("Operator token").fill("synthetic-page-operator");
  await page.getByRole("button", { name: "Load control" }).click();
  await expect(
    page.getByText("Loaded revision 7.", { exact: true }),
  ).toBeVisible();
  await expect(page.getByText(/^Backups: failed\./)).toContainText(
    "Verify recovery before resuming new work.",
  );
  await expect(page.getByText(/^Backups: failed\./)).not.toContainText(
    "Last verified: unknown",
  );
  await page
    .getByLabel("Pause new work; let current work finish", { exact: true })
    .check();
  await page.getByRole("button", { name: "Apply control" }).click();
  await expect(
    page.getByText("Control changed; reload before updating", { exact: true }),
  ).toBeVisible();
  expect(updates).toBe(1);
  expect(
    await page.evaluate(
      () => JSON.stringify(localStorage) + JSON.stringify(sessionStorage),
    ),
  ).not.toContain("synthetic-page-operator");
});

// Public presentation fixtures never mutate the shared browser server's
// admission state. Real token authority, persistence and refusal races are
// covered by test_launch_control.py against isolated stores.
test("pause appears within a poll, blocks new replies and clears on resume", async ({
  page,
}) => {
  let paused = false;
  await page.route("**/api/launch-status", (route) =>
    route.fulfill({
      json: {
        reason: paused ? "paused" : null,
        message: paused
          ? "Research is paused while we restore capacity."
          : null,
        resumes_at: paused ? Math.floor(Date.now() / 1000) + 3600 : null,
        paused,
        free_runs_allowed: !paused,
        byok_runs_allowed: !paused,
      },
    }),
  );
  await page.goto("/");
  const input = page.getByRole("textbox").first();
  await input.fill("Synthetic launch check");
  await expect(page.getByRole("button", { name: "Send" })).toBeEnabled();
  paused = true;
  const notice = page.locator("#launch-availability-notice");
  await expect(notice).toContainText("Research is paused", { timeout: 6000 });
  await expect(notice.locator("time")).toBeVisible();
  await expect(page.getByRole("button", { name: "Send" })).toBeDisabled();
  let replies = 0;
  page.on("request", (request) => {
    if (
      request.method() === "POST" &&
      request.url().includes("/api/interviews")
    )
      replies++;
  });
  await input.press("Enter");
  expect(replies).toBe(0);
  paused = false;
  await expect(notice).toBeEmpty({ timeout: 6000 });
  await expect(page.getByRole("button", { name: "Send" })).toBeEnabled();
});

async function checkFreeCapacityNotice(
  page: Page,
  width: number,
  colorScheme: "light" | "dark",
) {
  let exhausted = false;
  await page.route("**/api/launch-status", (route) =>
    route.fulfill({
      json: {
        reason: exhausted ? "free_capacity" : null,
        message: exhausted
          ? "Today's free research capacity is used up. You can use your own key in Settings."
          : null,
        resumes_at: exhausted
          ? Math.floor(Date.now() / 86400000 + 1) * 86400
          : null,
        paused: false,
        free_runs_allowed: !exhausted,
        byok_runs_allowed: true,
      },
    }),
  );
  await page.goto("/");
  await page
    .getByRole("textbox")
    .first()
    .fill("Which cellular mechanisms repair damaged mitochondria?");
  await page.getByRole("button", { name: "Send" }).click();
  const start = page.getByRole("button", { name: "Start research" });
  const turns = page.getByRole("button", { name: "Copy response" });
  await expect(turns.first()).toBeVisible();
  for (let turn = 0; turn < 5; turn++) {
    if (await start.isVisible()) break;
    const count = await turns.count();
    await page
      .getByRole("textbox")
      .last()
      .fill(
        "Use cell culture controls and translational relevance. No further constraints.",
      );
    await page.getByRole("button", { name: "Send" }).click();
    await expect(async () => {
      expect((await start.isVisible()) || (await turns.count()) > count).toBe(
        true,
      );
    }).toPass();
  }
  await expect(start).toBeVisible();
  exhausted = true;
  await expect(start).toBeDisabled({ timeout: 6000 });
  await expect(start).toHaveAttribute(
    "aria-describedby",
    "launch-availability-notice",
  );
  await page.setViewportSize({ width, height: 960 });
  await page.emulateMedia({ colorScheme, reducedMotion: "reduce" });
  await page.evaluate(() => localStorage.removeItem("cosci-theme"));
  await page.reload();
  await expect(page.locator("#launch-availability-notice")).toContainText(
    "capacity is used up",
  );
  await expect(page.locator("html")).toHaveAttribute("data-theme", colorScheme);
  expect(
    await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth),
  ).toBe(true);
}

for (const width of [390, 1440]) {
  for (const colorScheme of ["light", "dark"] as const) {
    test(`free-capacity notice explains a disabled start at ${width}px in ${colorScheme}`, async ({
      page,
    }) => {
      await checkFreeCapacityNotice(page, width, colorScheme);
    });
  }
}

test("credit notice has no invented reset and the operations page rejects a visitor", async ({
  page,
}) => {
  await page.route("**/api/launch-status", (route) =>
    route.fulfill({
      json: {
        reason: "credit_exhausted",
        message:
          "Azure credit is unavailable. You can use your own key in Settings.",
        resumes_at: null,
        paused: false,
        free_runs_allowed: false,
        byok_runs_allowed: true,
      },
    }),
  );
  await page.goto("/operations");
  const notice = page.locator("#launch-availability-notice");
  await expect(notice).toContainText("Azure credit is unavailable");
  await expect(notice.locator("time")).toHaveCount(0);
  await page.getByLabel("Operator token").fill("synthetic-invalid-token");
  await page.getByRole("button", { name: "Load control" }).click();
  await expect(
    page.getByText("Operator token required", { exact: true }),
  ).toBeVisible();
  await expect(page.getByRole("button", { name: "Apply control" })).toHaveCount(
    0,
  );
  expect(
    await page.evaluate(
      () => JSON.stringify(localStorage) + JSON.stringify(sessionStorage),
    ),
  ).not.toContain("synthetic-invalid-token");
});
