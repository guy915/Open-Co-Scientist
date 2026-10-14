import { expect, test } from "../support/fixtures";

interface PublicExample {
  id: string;
  title: string;
  description: string;
  heading: string;
  goal: string;
  lastmod: string;
  summary: string;
  ideas: { title: string; statement: string }[];
}

function expectPublicDocument(
  html: string,
  metadata: {
    title: string;
    description: string;
    canonical: string;
    heading: string;
    summary?: string;
  },
) {
  expect(html).toContain(`<title>${metadata.title}</title>`);
  expect(html).toContain(
    `<meta name="description" content="${metadata.description}" />`,
  );
  expect(html).toContain('<meta name="robots" content="index, follow" />');
  expect(html).toContain('<meta name="googlebot" content="index, follow" />');
  expect(html).toContain(
    `<link rel="canonical" href="${metadata.canonical}" />`,
  );
  expect(html).toContain(
    `<meta property="og:url" content="${metadata.canonical}" />`,
  );
  expect(html).toContain(`<h1>${metadata.heading}</h1>`);
  if (metadata.summary) expect(html).toContain(`<p>${metadata.summary}</p>`);
}

test("built home, privacy and curated examples are indexable before JavaScript", async ({
  request,
}) => {
  const manifestResponse = await request.get("/examples.json");
  expect(manifestResponse.ok()).toBe(true);
  const examples = (await manifestResponse.json()) as PublicExample[];
  expect(examples).toHaveLength(3);
  for (const example of examples) {
    expect(Object.keys(example).sort()).toEqual(
      [
        "description",
        "goal",
        "heading",
        "id",
        "ideas",
        "lastmod",
        "summary",
        "title",
      ].sort(),
    );
  }

  const homeResponse = await request.get("/index.html");
  const privacyResponse = await request.get("/privacy/index.html");
  expect(homeResponse.ok()).toBe(true);
  expect(privacyResponse.ok()).toBe(true);
  const home = await homeResponse.text();
  const privacy = await privacyResponse.text();
  expectPublicDocument(home, {
    title: "Open Co-Scientist",
    description:
      "Generate, evaluate, and rank research hypotheses with a team of agents.",
    canonical: "https://open-coscientist.com",
    heading: "Open Co-Scientist",
  });
  expect(home).toContain('href="/privacy"');
  expectPublicDocument(privacy, {
    title: "Privacy notice - Open Co-Scientist",
    description: "Privacy notice for Open Co-Scientist.",
    canonical: "https://open-coscientist.com/privacy",
    heading: "Privacy notice",
  });

  const socialImage = home.match(/<meta property="og:image" content="([^"]+)"/);
  expect(socialImage).not.toBeNull();
  const icons = [
    '<link rel="icon" href="/favicon.ico" sizes="48x48" />',
    '<link rel="icon" type="image/svg+xml" href="/favicon.svg" sizes="any" />',
    '<link rel="apple-touch-icon" href="/apple-touch-icon.png" />',
  ];
  for (const html of [home, privacy]) {
    expect(html).toContain(socialImage![0]);
    for (const icon of icons) expect(html).toContain(icon);
  }

  for (const example of examples) {
    const response = await request.get(`/examples/${example.id}/index.html`);
    expect(response.ok()).toBe(true);
    const html = await response.text();
    expectPublicDocument(html, {
      title: example.title,
      description: example.description,
      canonical: `https://open-coscientist.com/examples/${example.id}`,
      heading: example.heading,
      summary: example.goal,
    });
    expect(html).toContain(`<p>${example.summary}</p>`);
    expect(html).toContain("<h2>Leading hypotheses</h2>");
    expect(example.ideas).toHaveLength(3);
    for (const idea of example.ideas) {
      expect(html).toContain(`<h3>${idea.title}</h3>`);
      expect(html).toContain(`<p>${idea.statement}</p>`);
    }
    expect(html).toContain('href="/"');
    expect(html).not.toContain("application/ld+json");
    expect(html).toContain(socialImage![0]);
    for (const icon of icons) expect(html).toContain(icon);
  }

  const sitemapResponse = await request.get("/sitemap.xml");
  const llmsResponse = await request.get("/llms.txt");
  expect(sitemapResponse.ok()).toBe(true);
  expect(llmsResponse.ok()).toBe(true);
  const sitemap = await sitemapResponse.text();
  const llms = await llmsResponse.text();
  for (const example of examples) {
    const url = `https://open-coscientist.com/examples/${example.id}`;
    expect(sitemap).toContain(
      `<loc>${url}</loc>\n    <lastmod>${example.lastmod}</lastmod>`,
    );
    expect(llms).toContain(`](${url})`);
    expect(llms).toContain(example.heading);
  }
  expect(sitemap).toContain("<loc>https://open-coscientist.com</loc>");
  expect(sitemap).toContain("<loc>https://open-coscientist.com/privacy</loc>");
  expect(llms).toContain(
    "[Privacy notice](https://open-coscientist.com/privacy)",
  );
  expect(llms).not.toContain("/runs/");
  expect(llms).not.toContain("/chats/");
});

test("built public HTML aliases retain indexable pages with clean canonicals", async ({
  page,
  request,
}) => {
  const manifest = await request.get("/examples.json");
  expect(manifest.ok()).toBe(true);
  const [example] = (await manifest.json()) as PublicExample[];
  expect(example).toBeDefined();

  const aliases = [
    {
      path: "/index.html",
      finalPath: "/",
      canonical: "https://open-coscientist.com",
      heading: "What breakthrough should we make today?",
    },
    {
      path: "/privacy/index.html",
      finalPath: "/privacy",
      canonical: "https://open-coscientist.com/privacy",
      heading: "Privacy notice",
    },
    {
      path: `/examples/${example.id}/index.html`,
      finalPath: `/examples/${example.id}/index.html`,
      canonical: `https://open-coscientist.com/examples/${example.id}`,
      heading: example.heading,
    },
  ];
  for (const { path, finalPath, canonical, heading } of aliases) {
    await page.goto(path);
    await expect(page).toHaveURL(finalPath);
    await expect(
      page.getByRole("heading", { name: heading, level: 1 }),
    ).toBeVisible();
    await expect(page.locator('meta[name="robots"]')).toHaveAttribute(
      "content",
      "index, follow",
    );
    await expect(page.locator('link[rel="canonical"]')).toHaveAttribute(
      "href",
      canonical,
    );
    await expect(page.locator('meta[property="og:url"]')).toHaveAttribute(
      "content",
      canonical,
    );
  }
});

test("built example stays indexable while its public manifest is pending", async ({
  page,
  request,
}) => {
  const manifest = await request.get("/examples.json");
  expect(manifest.ok()).toBe(true);
  const [example] = (await manifest.json()) as PublicExample[];
  expect(example).toBeDefined();

  await page.route("**/examples.json", async (route) => {
    await new Promise((resolve) => setTimeout(resolve, 750));
    await route.continue();
  });
  await page.goto(`/examples/${example.id}/index.html`);
  await expect(page.locator('meta[name="robots"]')).toHaveAttribute(
    "content",
    "index, follow",
  );
  await expect(
    page.getByRole("heading", { name: example.heading, level: 1 }),
  ).toBeVisible();
  await page.unroute("**/examples.json");
  await expect(
    page.getByRole("button", { name: "Open example", exact: true }),
  ).toBeVisible();
});
