import { writeFile } from "node:fs/promises";
import AxeBuilder from "@axe-core/playwright";
import { test, expect, createCompletedRun } from "../support/fixtures";
import { tabTo } from "../support/keyboard";

for (const theme of ["light", "dark"]) {
  test(`@keyboard-semantics header landmark and conversation heading ${theme}`, async ({
    page,
  }, info) => {
    await page.addInitScript(
      (mode) => localStorage.setItem("cosci-theme", mode),
      theme,
    );
    await page.goto("/");
    const banner = page.getByRole("banner", { name: "Workspace header" });
    await expect.soft(banner).toHaveCount(1);
    if (await banner.count()) {
      await expect(
        banner.getByRole("button", { name: "Feedback", exact: true }),
      ).toBeVisible();
      await expect(banner.getByRole("link", { name: /home/ })).toBeVisible();
    }
    const composer = page.getByRole("textbox").last();
    await tabTo(page, composer);
    await page.keyboard.insertText("Compare mechanisms of enzyme stability.");
    await page.keyboard.press("Enter");
    await expect(
      page.getByRole("button", { name: "Copy response" }).first(),
    ).toBeVisible();
    const heading = page.getByRole("heading", { level: 1 });
    await expect.soft(heading).toHaveCount(1);
    if (await heading.count()) await expect(heading).not.toBeEmpty();
    const results = await new AxeBuilder({ page })
      .withRules(["region", "page-has-heading-one"])
      .analyze();
    expect.soft(results.violations).toEqual([]);
    const tree = info.outputPath("semantics.yml");
    await writeFile(tree, await page.locator("body").ariaSnapshot());
    await info.attach("semantics-tree", {
      path: tree,
      contentType: "text/yaml",
    });
    const screenshot = info.outputPath("semantics.png");
    await page.screenshot({ path: screenshot });
    await info.attach("semantics", {
      path: screenshot,
      contentType: "image/png",
    });
  });
}

for (const theme of ["light", "dark"]) {
  test(`@keyboard-semantics specification headings follow the document title ${theme}`, async ({page, api}, info) => {
    await page.addInitScript(mode => localStorage.setItem("cosci-theme", mode), theme);
    const id = await createCompletedRun(api, {
      research_goal: "Offline specification heading navigation",
      tier: "express",
      attributes: ["Human evidence and causal mechanisms"],
      requirements: ["Matched controls and reproducible experiments"],
    });
    await page.goto(`/runs/${id}/details`);
    await expect(page.getByRole("heading", {name: "Run Specifications", exact: true})).toBeVisible();
    const tree = info.outputPath("specification-headings.yml");
    await writeFile(tree, await page.locator("body").ariaSnapshot());
    await info.attach("specification-headings-tree", {path: tree, contentType: "text/yaml"});
    const image = info.outputPath("specification-headings.png");
    await page.screenshot({path: image});
    await info.attach("specification-headings", {path: image, contentType: "image/png"});
    const results = await new AxeBuilder({page}).withRules(["heading-order"]).analyze();
    expect.soft(results.violations).toEqual([]);
    await expect(page.getByRole("heading", {name: "Focus Area:", level: 3})).toBeVisible();
    await expect(page.getByRole("heading", {name: "Preferences:", level: 3})).toBeVisible();
    await page.reload();
    await expect(page.getByRole("heading", {name: "Focus Area:", level: 3})).toBeVisible();
  });
}
