/**
 * Private workbench routes stay out of the public build-time route list.
 */
import {mkdir, readFile, writeFile} from "node:fs/promises";
import path from "node:path";
import {fileURLToPath} from "node:url";

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const dist = path.resolve(root, process.env.COSCI_FRONTEND_DIST || "dist");
const baseHtml = await readFile(path.join(dist, "index.html"), "utf8");
const origin = "https://open-coscientist.com";

const routes = [
  {
    path: "/",
    title: "Open Co-Scientist",
    description:
      "Generate, evaluate, and rank research hypotheses with a team of agents.",
    heading: "Drive novel scientific discovery with Open Co-Scientist.",
    body: "Generate, evaluate, and rank hypotheses with agent collaboration.",
    jsonLd: [
      {
        "@context": "https://schema.org",
        "@type": "WebSite",
        name: "Open Co-Scientist",
        url: "https://open-coscientist.com",
        description: "A multi-agent research workspace.",
      },
      {
        "@context": "https://schema.org",
        "@type": "SoftwareApplication",
        name: "Open Co-Scientist",
        applicationCategory: "ResearchApplication",
        operatingSystem: "Web",
        url: "https://open-coscientist.com",
        description:
          "A multi-agent workspace that generates, reviews, ranks, and synthesizes research hypotheses.",
        isAccessibleForFree: true,
      },
    ],
  },
  {
    path: "/privacy",
    title: "Privacy notice - Open Co-Scientist",
    description: "Privacy notice for Open Co-Scientist.",
    heading: "Privacy notice",
    body: "Sentry uses the free Developer plan, with retention of 30 days for new error events. Events ingested during the earlier free Business trial retain 90 days. A downgrade does not shorten the retention assigned to earlier events at ingestion.",
  },
];

function escapeHtml(value) {
  return value
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;");
}

function renderRoute(route) {
  const canonical = `${origin}${route.path === "/" ? "" : route.path.replace(/\/$/, "")}`;
  const jsonLd = route.jsonLd
    ? JSON.stringify(route.jsonLd).replaceAll("</", "<\\/")
    : "";
  return baseHtml
    .replace(/<title>.*?<\/title>/, `<title>${escapeHtml(route.title)}</title>`)
    .replace(
      /<meta name="description" content=".*?" \/>/,
      `<meta name="description" content="${escapeHtml(route.description)}" />`,
    )
    .replace(
      /<link rel="canonical" href=".*?" \/>/,
      `<link rel="canonical" href="${canonical}" />`,
    )
    .replace(
      /<meta property="og:title" content=".*?" \/>/,
      `<meta property="og:title" content="${escapeHtml(route.title)}" />`,
    )
    .replace(
      /<meta property="og:description" content=".*?" \/>/,
      `<meta property="og:description" content="${escapeHtml(route.description)}" />`,
    )
    .replace(
      /<meta property="og:url" content=".*?" \/>/,
      `<meta property="og:url" content="${canonical}" />`,
    )
    .replace(
      /<meta name="twitter:title" content=".*?" \/>/,
      `<meta name="twitter:title" content="${escapeHtml(route.title)}" />`,
    )
    .replace(
      /<meta name="twitter:description" content=".*?" \/>/,
      `<meta name="twitter:description" content="${escapeHtml(route.description)}" />`,
    )
    .replace(
      /<noscript>[\s\S]*?<\/noscript>/,
      `<noscript><main><h1>${escapeHtml(route.heading)}</h1><p>${escapeHtml(route.body)}</p></main></noscript>`,
    )
    .replace(
      "</head>",
      route.jsonLd ? `<script type="application/ld+json">${jsonLd}</script></head>` : "</head>",
    );
}

for (const route of routes) {
  const outputDirectory =
    route.path === "/" ? dist : path.join(dist, route.path.replace(/^\//, ""));
  await mkdir(outputDirectory, {recursive: true});
  await writeFile(path.join(outputDirectory, "index.html"), renderRoute(route));
}

const notFoundHtml = baseHtml
  .replace(
    /<title>.*?<\/title>/,
    "<title>Page not found - Open Co-Scientist</title>",
  )
  .replace(
    /<meta name="description" content=".*?" \/>/,
    '<meta name="description" content="The page you requested does not exist." />',
  )
  .replace(
    /<meta name="robots" content=".*?" \/>/,
    '<meta name="robots" content="noindex, nofollow" />',
  )
  .replace(
    /<meta name="googlebot" content=".*?" \/>/,
    '<meta name="googlebot" content="noindex, nofollow" />',
  )
  .replace(
    /<meta property="og:title" content=".*?" \/>/,
    '<meta property="og:title" content="Page not found - Open Co-Scientist" />',
  )
  .replace(
    /<meta property="og:description" content=".*?" \/>/,
    '<meta property="og:description" content="The page you requested does not exist." />',
  )
  .replace(
    /<meta name="twitter:title" content=".*?" \/>/,
    '<meta name="twitter:title" content="Page not found - Open Co-Scientist" />',
  )
  .replace(
    /<meta name="twitter:description" content=".*?" \/>/,
    '<meta name="twitter:description" content="The page you requested does not exist." />',
  )
  .replace(/<link rel="canonical" href=".*?" \/>/, "")
  .replace(/<meta property="og:url" content=".*?" \/>/, "")
  .replace(
    /<noscript>[\s\S]*?<\/noscript>/,
    '<noscript><main><h1>Page not found</h1><p>The page you requested does not exist.</p><p><a href="/">Return home</a></p></main></noscript>',
  );

await writeFile(path.join(dist, "404.html"), notFoundHtml);

console.log(`Pre-rendered ${routes.length} public routes and 404.html.`);
