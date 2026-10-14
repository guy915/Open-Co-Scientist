import { createCompletedRun, expect, test, CLIENT_ID } from "../support/fixtures";
import { API_URL } from "../support/paths";

const FOREIGN_CLIENT_ID = "e2e-foreign-client";
const GOAL = "Offline production check of enzyme stability hypotheses";

test("deep links serve built assets and retain private-page metadata", async ({
  page,
  api,
}) => {
  const errors: string[] = [];
  page.on("pageerror", (error) => errors.push(error.message));
  const id = await createCompletedRun(api, {
    research_goal: GOAL,
    tier: "express",
  });
  await page.goto(`/runs/${id}/details`);
  await expect(
    page.getByRole("heading", { name: /run specifications/i }),
  ).toBeVisible();
  const scripts = await page
    .locator("script[src]")
    .evaluateAll((elements) =>
      elements.map((element) => element.getAttribute("src") ?? ""),
    );
  expect(scripts.some((src) => /^\/assets\/.*\.js$/.test(src))).toBe(true);
  expect(
    scripts.some(
      (src) => src.includes("@vite/client") || src.includes("/src/"),
    ),
  ).toBe(false);
  await expect(page.locator('meta[name="robots"]')).toHaveAttribute(
    "content",
    /noindex/,
  );
  await page.reload();
  await expect(
    page.getByRole("heading", { name: /run specifications/i }),
  ).toBeVisible();
  await expect(page.locator('meta[name="robots"]')).toHaveAttribute(
    "content",
    /noindex/,
  );
  expect(errors).toEqual([]);
});

test("anonymous ownership refuses headerless creation and foreign client IDs", async ({
  request,
  api,
}) => {
  const created = await request.post(`${API_URL}/api/runs`, {
    data: {
      research_goal: "An anonymous research goal",
      tier: "express",
    },
  });
  expect(created.status()).toBe(400);
  expect(await created.text()).toContain("X-Client-ID");

  const { id } = await api.createRun({
    research_goal: GOAL,
    tier: "express",
  });
  for (const host of [undefined, "example.com/#", "example.com/?"]) {
    const response = await request.get(`${API_URL}/api/runs/${id}`, {
      headers: {
        "X-Client-ID": FOREIGN_CLIENT_ID,
        ...(host ? { Host: host } : {}),
      },
    });
    expect(response.status()).toBe(404);
  }
  const owner = await request.get(`${API_URL}/api/runs/${id}`, {
    headers: { "X-Client-ID": CLIENT_ID },
  });
  expect(owner.status()).toBe(200);
});

test("the browser owns its research by client ID and keeps it after a reload", async ({
  page,
  api,
}) => {
  const id = await createCompletedRun(api, {
    research_goal: GOAL,
    tier: "express",
  });
  const apiRequests: { url: string; headers: Record<string, string> }[] = [];
  page.on("request", (req) => {
    if (req.url().startsWith(`${API_URL}/api/`)) {
      apiRequests.push({ url: req.url(), headers: req.headers() });
    }
  });
  const loaded = page.waitForResponse(
    (response) =>
      response.url() === `${API_URL}/api/runs/${id}` &&
      response.status() === 200,
  );
  await page.goto(`/runs/${id}/details`);
  const read = await loaded;
  expect(read.request().headers()["x-client-id"]).toBe(CLIENT_ID);
  expect(read.request().headers().authorization).toBeUndefined();
  await expect(
    page.getByRole("heading", { name: /run specifications/i }),
  ).toBeVisible();
  await expect(page.getByText(GOAL).first()).toBeVisible();
  await page.reload();
  await expect(
    page.getByRole("heading", { name: /run specifications/i }),
  ).toBeVisible();
  await page
    .getByRole("link", { name: "Research Overview", exact: true })
    .click();
  await expect(
    page.getByRole("heading", { name: /agent insights/i }),
  ).toBeVisible();

  const runRequests = apiRequests.filter(({ url }) =>
    url.startsWith(`${API_URL}/api/runs/${id}`),
  );
  expect(runRequests.length).toBeGreaterThan(0);
  for (const { url, headers } of apiRequests) {
    expect(headers.authorization, url).toBeUndefined();
  }
  for (const { url, headers } of runRequests) {
    expect(headers["x-client-id"], url).toBe(CLIENT_ID);
  }

  const foreignRead = await page.request.get(`${API_URL}/api/runs/${id}`, {
    headers: { "X-Client-ID": FOREIGN_CLIENT_ID },
  });
  expect(foreignRead.status()).toBe(404);
});
