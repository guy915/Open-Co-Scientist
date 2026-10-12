import { captureViewport, expect, test } from "../support/fixtures";
import { API_URL } from "../support/paths";

for (const theme of ["light", "dark"]) {
  for (const width of [390, 768, 1440]) {
    test(`interview connection failure explains retry at ${width}px in ${theme}`, async ({
      page,
    }) => {
      const viewport = { width, height: 844 };
      await page.setViewportSize(viewport);
      await page.addInitScript(
        (mode) => localStorage.setItem("cosci-theme", mode),
        theme,
      );
      const endpoint = `${API_URL}/api/interviews`;
      await page.route(endpoint, (route) =>
        route.request().method() === "POST"
          ? route.abort("connectionfailed")
          : route.continue(),
      );
      await page.goto("/");
      const composer = page.getByRole("textbox").last();
      const goal = "Investigate immune mechanisms in human disease";
      await composer.fill(goal);
      await page.getByRole("button", { name: "Send", exact: true }).click();
      const message = page.getByText(
        "Cannot connect to the server. Check your connection and try again.",
        { exact: true },
      );
      await expect(message).toBeInViewport();
      await expect(composer).toBeEditable();
      await captureViewport(page, {
        ...viewport,
        name: `network-failure-${width}-${theme}.png`,
      });

      await page.unroute(endpoint);
      await composer.fill(goal);
      await page.getByRole("button", { name: "Send", exact: true }).click();
      await expect(page).toHaveURL(/\/chats\/[\w-]+$/);
      await expect(message).toHaveCount(0);
      await expect(
        page
          .locator(".reference-model-bubble")
          .getByText(
            "Which scientific mechanisms or focus areas should this research prioritize?",
            { exact: true },
          ),
      ).toBeVisible();
      await captureViewport(page, {
        ...viewport,
        name: `network-recovered-${width}-${theme}.png`,
      });
    });
  }
}
