import { expect, test } from "../support/fixtures";

test("privacy deep link and reload disclose ingestion-time error retention", async ({
  page,
  request,
}) => {
  const html = await request.get("/privacy/index.html");
  expect(html.ok()).toBe(true);
  const body = await html.text();
  expect(body).toContain("Sentry uses the free Developer plan");
  expect(body).toContain("30 days");
  expect(body).toContain("90 days");
  expect(body).toContain('href="https://open-coscientist.com/privacy"');
  expect(body).not.toContain('type="application/ld+json"');
  for (const theme of ["light", "dark"]) {
    await page.addInitScript(
      (value) => localStorage.setItem("cosci-theme", value),
      theme,
    );
    await page.goto("/privacy");
    await expect(
      page.getByRole("heading", { name: "Privacy notice", exact: true }),
    ).toBeVisible();
    await expect(page.getByText(/Business trial.*90 days/)).toBeVisible();
    await expect(
      page.getByText(/Developer plan.*30 days for new error events/),
    ).toBeVisible();
    await expect(page.getByText(/downgrade does not shorten/)).toBeVisible();
    await expect(page.locator('link[rel="canonical"]')).toHaveAttribute(
      "href",
      "https://open-coscientist.com/privacy",
    );
    await page.reload();
    await expect(
      page.getByRole("heading", { name: "Privacy notice", exact: true }),
    ).toBeVisible();
  }
});
