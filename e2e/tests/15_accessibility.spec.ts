import AxeBuilder from "@axe-core/playwright";
import { type Page, type TestInfo } from "@playwright/test";
import { writeFile } from "node:fs/promises";
import { createCompletedRun, expect, test } from "../support/fixtures";

type AxeResults = Awaited<ReturnType<AxeBuilder["analyze"]>>;

function blockingViolations(results: AxeResults) {
  return results.violations.filter(
    (violation) =>
      violation.impact === "serious" || violation.impact === "critical",
  );
}

async function scanAccessibility(page: Page, info: TestInfo) {
  const results = await new AxeBuilder({ page }).analyze();
  const path = info.outputPath("accessibility-results.json");
  await writeFile(path, JSON.stringify(results, null, 2));
  await info.attach("accessibility-results", {
    path,
    contentType: "application/json",
  });
  expect(blockingViolations(results)).toEqual([]);
}

async function checkAccessibility(page: Page, info: TestInfo) {
  await page.evaluate(async () => {
    await document.fonts.ready;
    await Promise.all(
      document
        .getAnimations()
        .filter(
          (animation) =>
            animation.effect?.getComputedTiming().iterations !== Infinity,
        )
        .map((animation) => animation.finished.catch(() => undefined)),
    );
  });
  await scanAccessibility(page, info);
}

for (const theme of ["light", "dark"]) {
  test.describe(`accessibility in ${theme}`, () => {
    test.beforeEach(async ({ page }) => {
      await page.setViewportSize({ width: 1440, height: 1050 });
      await page.addInitScript(
        (mode) => localStorage.setItem("cosci-theme", mode),
        theme,
      );
      await page.route("https://www.youtube-nocookie.com/**", (route) =>
        route.fulfill({
          body: '<html lang="en"><head><title>Trailer</title></head><body><main>Trailer</main></body></html>',
          contentType: "text/html",
        }),
      );
    });

    test("home", async ({ page }, info) => {
      await page.goto("/");
      await expect(page.getByRole("textbox")).toBeVisible();
      await expect(page.locator("html")).toHaveAttribute("data-theme", theme);
      await expect(page.locator(".ucs-system-status")).toHaveText("Offline mode");
      await expect(page.locator(".ucs-system-status")).toBeVisible();
      await checkAccessibility(page, info);
    });

    test("status label during entry animation", async ({ page }, info) => {
      await page.goto("/");
      await page.addStyleTag({
        content:
          ".ucs-system-status { animation-duration: 10s !important; " +
          "animation-delay: -2s !important; animation-play-state: paused !important; }",
      });
      await expect(page.locator(".ucs-system-status")).toHaveText("Offline mode");
      await expect(page.locator(".ucs-system-status")).toBeVisible();
      await page.evaluate(async () => {
        await document.fonts.ready;
      });
      await scanAccessibility(page, info);
    });

    test("landing", async ({ page }, info) => {
      await page.goto("/");
      await page.getByRole("button", { name: "Scroll to see how Open Co-Scientist works" }).click();
      await page.locator("#landing-overview").scrollIntoViewIfNeeded();
      await expect(page.locator("#landing-overview")).toBeInViewport();
      await expect(page.locator("html")).toHaveAttribute("data-theme", theme);
      await checkAccessibility(page, info);
    });

    test("completed run report", async ({ page, api }, info) => {
      const id = await createCompletedRun(api, {
        research_goal: "Compare mechanisms of mitochondrial quality control",
        tier: "standard",
      });
      await page.goto(`/runs/${id}/overview`);
      await expect(
        page.getByRole("heading", { name: /agent insights/i }),
      ).toBeVisible();
      await expect(page.locator("html")).toHaveAttribute("data-theme", theme);
      await checkAccessibility(page, info);
    });

    test("interview with answer options", async ({ page }, info) => {
      const question = "Which mechanism should the research investigate?";
      await page.route("**/api/interviews/accessibility-question", (route) =>
        route.fulfill({
          json: {
            id: "accessibility-question",
            client_id: "e2e-client",
            status: "active",
            fields: {
              research_challenge: "Investigate immune mechanisms",
              focus_area: [],
              preferences: [],
              title: null,
            },
            current_question: question,
            documents: [],
            created_at: 1,
            updated_at: 2,
            completed_at: null,
            turns: [
              {
                id: 1,
                role: "agent",
                content: question,
                created_at: 2,
                questions: [
                  {
                    header: "Mechanism",
                    question,
                    multi_select: false,
                    options: [
                      {
                        label: "Immune relay",
                        description: "Compare immune interactions.",
                      },
                      {
                        label: "Direct effect",
                        description: "Compare direct tissue effects.",
                      },
                    ],
                  },
                ],
              },
            ],
          },
        }),
      );
      await page.goto("/chats/accessibility-question");
      await expect(
        page.getByRole("region", { name: "Answer options" }),
      ).toBeVisible();
      await expect(page.locator("html")).toHaveAttribute("data-theme", theme);
      await checkAccessibility(page, info);
    });
  });
}

test("the accessibility guard detects an unlabelled input", async ({
  page,
}) => {
  await page.setContent(
    '<html lang="en"><head><title>Failure proof</title></head><body><main><input></main></body></html>',
  );
  const results = await new AxeBuilder({ page }).analyze();
  expect(
    blockingViolations(results).map((violation) => violation.id),
  ).toContain("label");
});
