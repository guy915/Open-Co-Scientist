import { captureViewport, expect, test } from "../support/fixtures";

for (const theme of ["light", "dark"]) {
  test(`examples open as full private chats and keep chatting on a phone in ${theme}`, async ({
    page,
    api,
    request,
  }) => {
    await page.setViewportSize({ width: 375, height: 812 });
    await page.addInitScript(
      (mode) => localStorage.setItem("cosci-theme", mode),
      theme,
    );
    const demos = await api.listDemoRuns();
    const publicExamplesResponse = await request.get("/examples.json");
    expect(publicExamplesResponse.ok()).toBe(true);
    const publicExamples = (await publicExamplesResponse.json()) as {
      id: string;
      title: string;
      description: string;
      heading: string;
      goal: string;
      lastmod: string;
      summary: string;
      ideas: { title: string; statement: string }[];
    }[];
    // Phones have no Recents; the sidebar's Chats list carries the examples.
    // Each theme opens its own example: both share one visitor, and an opened
    // example lists as its private copy instead.
    const example = demos[theme === "light" ? 0 : 1];
    const publicExample = publicExamples.find((item) => item.id === example.id);
    expect(publicExample).toBeDefined();
    const cloneRequests: string[] = [];
    page.on("request", (request) => {
      if (
        request.method() === "POST" &&
        request.url().endsWith(`/api/runs/${example.id}/example-chat`)
      )
        cloneRequests.push(request.url());
    });
    await page.goto("/");
    await page.getByRole("button", { name: "Open navigation" }).click();
    const sidebarExample = page.locator(`a[href="/examples/${example.id}"]`);
    await expect(sidebarExample).toBeVisible();
    await sidebarExample.click();
    await expect(page).toHaveURL(`/examples/${example.id}`);
    await expect(
      page.getByRole("heading", {
        name: publicExample!.heading,
        exact: true,
      }),
    ).toBeVisible();
    await expect(page).toHaveTitle(publicExample!.title);
    await expect(
      page.getByText(publicExample!.goal, { exact: true }),
    ).toBeVisible();
    await expect(
      page.getByRole("heading", { name: "Example overview", exact: true }),
    ).toBeVisible();
    await expect(
      page.getByText(publicExample!.summary, { exact: true }),
    ).toBeVisible();
    await expect(
      page.getByRole("heading", { name: "Leading hypotheses", exact: true }),
    ).toBeVisible();
    expect(publicExample!.ideas).toHaveLength(3);
    for (const idea of publicExample!.ideas) {
      await expect(
        page.getByRole("heading", { name: idea.title, level: 3, exact: true }),
      ).toBeVisible();
      await expect(
        page.getByText(idea.statement, { exact: true }),
      ).toBeVisible();
    }
    await expect(page.locator('meta[name="description"]')).toHaveAttribute(
      "content",
      publicExample!.description,
    );
    await expect(page.locator('meta[name="robots"]')).toHaveAttribute(
      "content",
      "index, follow",
    );
    await expect(page.locator('link[rel="canonical"]')).toHaveAttribute(
      "href",
      `https://open-coscientist.com/examples/${example.id}`,
    );
    expect(cloneRequests).toHaveLength(0);
    await page
      .getByRole("button", { name: "Open example", exact: true })
      .click();
    await expect(page).toHaveURL(/\/chats\//);
    const chatUrl = page.url();
    await expect(page.getByLabel("Inferred run setup")).toBeAttached();
    await expect(page.getByLabel("Started research session")).toBeAttached();
    await expect(
      page.getByRole("link", { name: "View session details" }),
    ).toBeAttached();
    const question = `Which controls need replication in this ${theme} example?`;
    const composer = page.getByRole("textbox", {
      name: "Ask Co-Scientist",
    });
    await expect(composer).toBeInViewport();
    await composer.fill(question);
    await page.getByRole("button", { name: "Send", exact: true }).click();
    await expect(
      page
        .getByText("Answering from this run's own artifacts", {
          exact: false,
        })
        .last(),
    ).toBeVisible();
    await expect(
      page.getByRole("button", { name: "Send", exact: true }),
    ).toBeVisible();
    await page.reload();
    await expect(page.getByText(question, { exact: true })).toBeAttached();
    await expect(composer).toBeInViewport();
    await page.goto(`/examples/${example.id}`);
    await expect(page).toHaveURL(`/examples/${example.id}`);
    await expect(
      page.getByRole("button", { name: "Open example", exact: true }),
    ).toBeVisible();
    expect(cloneRequests).toHaveLength(1);
    await page
      .getByRole("button", { name: "Open example", exact: true })
      .click();
    await expect(page).toHaveURL(chatUrl);
    await expect(page.getByText(question, { exact: true })).toBeAttached();
    await captureViewport(page, {
      width: 375,
      height: 812,
      name: `example-chat-${theme}.png`,
    });
  });
}

test("every curated example carries its conversation, saved plan and linked results", async ({
  page,
  api,
  request,
}) => {
  const demos = await api.listDemoRuns();
  const response = await request.get("/examples.json");
  expect(response.ok()).toBe(true);
  const publicExamples = (await response.json()) as {
    id: string;
    title: string;
    description: string;
    heading: string;
    goal: string;
    lastmod: string;
    summary: string;
    ideas: { title: string; statement: string }[];
  }[];
  for (const example of demos) {
    const publicExample = publicExamples.find((item) => item.id === example.id);
    expect(publicExample).toBeDefined();
    await page.goto(`/examples/${example.id}`);
    await expect(page).toHaveURL(`/examples/${example.id}`);
    await expect(
      page.getByRole("heading", {
        name: publicExample!.heading,
        exact: true,
      }),
    ).toBeVisible();
    await expect(page).toHaveTitle(publicExample!.title);
    await expect(
      page.getByText(publicExample!.goal, { exact: true }),
    ).toBeVisible();
    await expect(
      page.getByText(publicExample!.summary, { exact: true }),
    ).toBeVisible();
    for (const idea of publicExample!.ideas) {
      await expect(
        page.getByRole("heading", { name: idea.title, level: 3, exact: true }),
      ).toBeVisible();
      await expect(
        page.getByText(idea.statement, { exact: true }),
      ).toBeVisible();
    }
    await expect(page.locator('meta[name="description"]')).toHaveAttribute(
      "content",
      publicExample!.description,
    );
    await expect(page.locator('meta[name="robots"]')).toHaveAttribute(
      "content",
      "index, follow",
    );
    await expect(page.locator('link[rel="canonical"]')).toHaveAttribute(
      "href",
      `https://open-coscientist.com/examples/${example.id}`,
    );
    await page
      .getByRole("button", { name: "Open example", exact: true })
      .click();
    await expect(page).toHaveURL(/\/chats\//);
    const plan = page.getByLabel("Inferred run setup");
    await expect(plan).toBeAttached();
    await expect(plan.getByRole("radio", { name: /Standard/ })).toBeChecked();
    await expect(
      page.getByText(
        "This is a curated example conversation and research plan.",
        { exact: false },
      ),
    ).toBeAttached();
    await expect(
      page.getByRole("link", { name: "View session details" }),
    ).toBeAttached();
    await expect(
      page.getByRole("textbox", {
        name: "Ask Co-Scientist",
      }),
    ).toBeVisible();
    await page.getByRole("link", { name: "View session details" }).click();
    await expect(page).toHaveURL(/\/runs\/.*\/details/);
    await expect(
      page.getByRole("heading", { name: /^Example: /, level: 1 }),
    ).toBeVisible();
  }
});

test("an unknown example is noindex and has no canonical URL", async ({
  page,
}) => {
  await page.goto("/examples/not-a-curated-example");
  await expect(
    page.getByRole("heading", { name: "Page not found", exact: true }),
  ).toBeVisible();
  await expect(page.locator('meta[name="robots"]')).toHaveAttribute(
    "content",
    "noindex, nofollow",
  );
  await expect(page.locator('link[rel="canonical"]')).toHaveCount(0);
  await expect(page.locator('meta[property="og:url"]')).toHaveCount(0);
});

test("route metadata changes from private to public and back without stale canonicals", async ({
  page,
  api,
  request,
}) => {
  const privateId = await api.createRun({
    research_goal: "Route metadata transition check",
    tier: "express",
  });
  const publicExamplesResponse = await request.get("/examples.json");
  expect(publicExamplesResponse.ok()).toBe(true);
  const publicExamples = (await publicExamplesResponse.json()) as {
    id: string;
  }[];
  const example = publicExamples[0];
  expect(example).toBeDefined();

  await page.goto(`/runs/${privateId.id}/details`);
  await expect(page.locator('meta[name="robots"]')).toHaveAttribute(
    "content",
    "noindex, nofollow",
  );
  await expect(page.locator('link[rel="canonical"]')).toHaveCount(0);
  await expect(page.locator('meta[property="og:url"]')).toHaveCount(0);

  await page.getByRole("banner").getByRole("link", { name: /home/i }).click();
  await expect(page).toHaveURL("/");
  await expect(page.locator('meta[name="robots"]')).toHaveAttribute(
    "content",
    "index, follow",
  );
  await expect(page.locator('link[rel="canonical"]')).toHaveAttribute(
    "href",
    "https://open-coscientist.com",
  );
  await expect(page.locator('meta[property="og:url"]')).toHaveAttribute(
    "content",
    "https://open-coscientist.com",
  );

  await page
    .getByRole("button", { name: /Scroll to see how Open Co-Scientist works/ })
    .click();
  await expect(
    page.getByRole("link", { name: "Privacy", exact: true }),
  ).toBeVisible();
  await page.getByRole("link", { name: "Privacy", exact: true }).click();
  await expect(page).toHaveURL("/privacy");
  await expect(page.locator('meta[name="robots"]')).toHaveAttribute(
    "content",
    "index, follow",
  );
  await expect(page.locator('link[rel="canonical"]')).toHaveAttribute(
    "href",
    "https://open-coscientist.com/privacy",
  );
  await expect(page.locator('meta[property="og:url"]')).toHaveAttribute(
    "content",
    "https://open-coscientist.com/privacy",
  );

  await page
    .getByRole("link", { name: "Research workspace", exact: true })
    .click();
  await expect(page.locator('link[rel="canonical"]')).toHaveAttribute(
    "href",
    "https://open-coscientist.com",
  );
  await page.goto(`/examples/${example.id}`);
  await expect(page.locator('link[rel="canonical"]')).toHaveAttribute(
    "href",
    `https://open-coscientist.com/examples/${example.id}`,
  );
  await expect(page.locator('meta[property="og:url"]')).toHaveAttribute(
    "content",
    `https://open-coscientist.com/examples/${example.id}`,
  );
  await page.getByRole("button", { name: "Open example", exact: true }).click();
  await expect(page).toHaveURL(/\/chats\//);
  await expect(page.locator('meta[name="robots"]')).toHaveAttribute(
    "content",
    "noindex, nofollow",
  );
  await expect(page.locator('link[rel="canonical"]')).toHaveCount(0);
  await expect(page.locator('meta[property="og:url"]')).toHaveCount(0);
});
