import {execFileSync} from 'node:child_process';
import {gunzipSync} from 'node:zlib';
import {readFileSync} from 'node:fs';
import {mkdir, readFile, writeFile} from 'node:fs/promises';
import path from 'node:path';
import {fileURLToPath} from 'node:url';

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
const repository = path.resolve(root, '../..');
const dist = path.resolve(root, process.env.COSCI_FRONTEND_DIST || 'dist');
const origin = 'https://open-coscientist.com';
const fixturePath = path.join(
  repository,
  'engine/src/co_scientist/domains/chat/data/demo_runs.json.gz',
);
const baseHtml = await readFile(path.join(dist, 'index.html'), 'utf8');

function escapeHtml(value) {
  return String(value)
    .replaceAll('&', '&amp;')
    .replaceAll('<', '&lt;')
    .replaceAll('>', '&gt;')
    .replaceAll('"', '&quot;')
    .replaceAll("'", '&#39;');
}

function escapeXml(value) {
  return String(value)
    .replaceAll('&', '&amp;')
    .replaceAll('<', '&lt;')
    .replaceAll('>', '&gt;')
    .replaceAll('"', '&quot;')
    .replaceAll("'", '&apos;');
}

function exampleManifest() {
  const fixture = JSON.parse(
    gunzipSync(readFileSync(fixturePath)).toString('utf8'),
  );
  const runs = fixture.tables?.runs;
  if (!Array.isArray(runs) || runs.length === 0) {
    throw new Error('Expected curated runs in demo_runs.json.gz.');
  }
  const reports = fixture.tables?.reports;
  const hypotheses = fixture.tables?.hypotheses;
  if (!Array.isArray(reports) || !Array.isArray(hypotheses)) {
    throw new Error(
      'Expected linked reports and hypotheses in demo_runs.json.gz.',
    );
  }
  const reportsByRun = new Map(reports.map(report => [report.run_id, report]));

  return runs.map(({id, title, research_goal, updated_at}) => {
    const heading = String(title)
      .replace(/^Example:\s*/i, '')
      .trim();
    const goal = String(research_goal).replace(/\s+/g, ' ').trim();
    const timestamp = Number(updated_at);
    if (
      !/^[A-Za-z0-9_-]+$/.test(String(id)) ||
      !heading ||
      !goal ||
      !Number.isFinite(timestamp)
    ) {
      throw new Error('A curated run is missing public example metadata.');
    }
    const report = reportsByRun.get(String(id));
    if (!report || typeof report.payload_json !== 'string') {
      throw new Error(`Curated run ${id} is missing its report payload.`);
    }
    const payload = JSON.parse(report.payload_json);
    const summary = payload.research_overview?.overview?.summary;
    const leaderboard = payload.leaderboard;
    if (typeof summary !== 'string' || !summary.trim()) {
      throw new Error(`Curated run ${id} is missing its overview summary.`);
    }
    if (!Array.isArray(leaderboard) || leaderboard.length < 3) {
      throw new Error(
        `Curated run ${id} has fewer than three leaderboard ideas.`,
      );
    }
    const ideas = leaderboard.slice(0, 3).map(item => {
      const hypothesis = hypotheses.find(
        row => row.id === item.id && row.run_id === String(id),
      );
      const ideaTitle = String(item.title ?? '')
        .replace(/\s+/g, ' ')
        .trim();
      const statement = String(hypothesis?.statement ?? '')
        .replace(/\s+/g, ' ')
        .trim();
      if (!ideaTitle || !statement) {
        throw new Error(
          `Curated run ${id} has an incomplete leaderboard idea.`,
        );
      }
      return {title: ideaTitle, statement};
    });
    return {
      id: String(id),
      title: `${heading} - Open Co-Scientist`,
      description: goal,
      heading,
      goal,
      lastmod: new Date(timestamp * 1000).toISOString().slice(0, 10),
      summary: summary.replace(/\s+/g, ' ').trim(),
      ideas,
    };
  });
}

function sourceLastmod(files, route) {
  try {
    const output = execFileSync(
      'git',
      ['log', '-1', '--format=%cs', '--', ...files],
      {cwd: repository, encoding: 'utf8', stdio: ['ignore', 'pipe', 'ignore']},
    ).trim();
    if (/^\d{4}-\d{2}-\d{2}$/.test(output)) return output;
  } catch {
    // Archive builds use the checked-in source value, never the build clock.
  }

  const location = route === '/' ? origin : `${origin}${route}`;
  const sourceSitemap = readFileSync(
    path.join(root, 'public/sitemap.xml'),
    'utf8',
  );
  for (const [, entryLocation, lastmod] of sourceSitemap.matchAll(
    /<url>\s*<loc>([^<]+)<\/loc>\s*(?:<lastmod>(\d{4}-\d{2}-\d{2})<\/lastmod>)?\s*<\/url>/g,
  )) {
    if (entryLocation === location) return lastmod;
  }
  return undefined;
}

function replaceRoot(html, content) {
  const root = /(<div id="root">)[\s\S]*?(<\/div>\s*<noscript>)/;
  const result = html.replace(
    root,
    (_match, opening, closing) => `${opening}${content}${closing}`,
  );
  if (result === html)
    throw new Error('Could not locate the #root prerender boundary.');
  return result.replace(/<noscript>[\s\S]*?<\/noscript>/, '');
}

function renderRoute(route) {
  let html = baseHtml
    .replace(/<title>.*?<\/title>/, `<title>${escapeHtml(route.title)}</title>`)
    .replace(
      /<meta name="description" content=".*?" \/>/,
      `<meta name="description" content="${escapeHtml(route.description)}" />`,
    )
    .replace(
      /<meta name="robots" content=".*?" \/>/,
      `<meta name="robots" content="${route.robots}" />`,
    )
    .replace(
      /<meta name="googlebot" content=".*?" \/>/,
      `<meta name="googlebot" content="${route.robots}" />`,
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
      /<meta name="twitter:title" content=".*?" \/>/,
      `<meta name="twitter:title" content="${escapeHtml(route.title)}" />`,
    )
    .replace(
      /<meta name="twitter:description" content=".*?" \/>/,
      `<meta name="twitter:description" content="${escapeHtml(route.description)}" />`,
    );

  if (route.canonical) {
    html = html
      .replace(
        /<link rel="canonical" href=".*?" \/>/,
        `<link rel="canonical" href="${route.canonical}" />`,
      )
      .replace(
        /<meta property="og:url" content=".*?" \/>/,
        `<meta property="og:url" content="${route.canonical}" />`,
      );
  } else {
    html = html
      .replace(/\s*<link rel="canonical" href=".*?" \/>/, '')
      .replace(/\s*<meta property="og:url" content=".*?" \/>/, '');
  }

  if (route.jsonLd) {
    const jsonLd = JSON.stringify(route.jsonLd).replaceAll('</', '<\\/');
    html = html.replace(
      '</head>',
      `<script type="application/ld+json">${jsonLd}</script></head>`,
    );
  }
  return replaceRoot(html, route.content);
}

function xmlUrl(route) {
  const lastmod = route.lastmod
    ? `\n    <lastmod>${route.lastmod}</lastmod>`
    : '';
  const location = route.path === '/' ? origin : `${origin}${route.path}`;
  return `  <url>\n    <loc>${escapeXml(location)}</loc>${lastmod}\n  </url>`;
}

async function writeRoute(route) {
  const directory =
    route.path === '/' ? dist : path.join(dist, route.path.replace(/^\//, ''));
  await mkdir(directory, {recursive: true});
  await writeFile(path.join(directory, 'index.html'), renderRoute(route));
}

const examples = exampleManifest();
const homeLastmod = sourceLastmod(
  [
    'app/frontend/src/features/chat/home_landing.tsx',
    'app/frontend/src/features/chat/home_landing_content.ts',
  ],
  '/',
);
const privacyLastmod = sourceLastmod(
  [
    'app/frontend/src/features/legal/privacy_page.tsx',
    'app/frontend/src/features/legal/privacy_content.tsx',
  ],
  '/privacy',
);
const pages = [
  {
    path: '/',
    title: 'Open Co-Scientist',
    description:
      'Generate, evaluate, and rank research hypotheses with a team of agents.',
    canonical: origin,
    robots: 'index, follow',
    lastmod: homeLastmod,
    content: `<main><h1>Open Co-Scientist</h1><p>A multi-agent partner for scientific discovery.</p><p>Generate, review, and rank research hypotheses with a team of agents.</p><section><h2>Curated examples</h2><ul>${examples
      .map(
        example =>
          `<li><a href="/examples/${escapeHtml(example.id)}">${escapeHtml(example.heading)}</a></li>`,
      )
      .join(
        '',
      )}</ul></section><p><a href="/privacy">Privacy notice</a></p></main>`,
    jsonLd: [
      {
        '@context': 'https://schema.org',
        '@type': 'WebSite',
        name: 'Open Co-Scientist',
        url: origin,
        description: 'A multi-agent research workspace.',
      },
      {
        '@context': 'https://schema.org',
        '@type': 'SoftwareApplication',
        name: 'Open Co-Scientist',
        applicationCategory: 'ResearchApplication',
        operatingSystem: 'Web',
        url: origin,
        description:
          'A multi-agent workspace that generates, reviews, ranks, and synthesizes research hypotheses.',
        isAccessibleForFree: true,
      },
    ],
  },
  {
    path: '/privacy',
    title: 'Privacy notice - Open Co-Scientist',
    description: 'Privacy notice for Open Co-Scientist.',
    canonical: `${origin}/privacy`,
    robots: 'index, follow',
    lastmod: privacyLastmod,
    content:
      '<main><article><h1>Privacy notice</h1><p>Open Co-Scientist is operated by Guy Barel, its individual owner in the Netherlands. The privacy notice explains what information the service stores, how model and hosting providers process it, how long it stays, and how to request access or deletion.</p><p>Contact <a href="mailto:guy.barel@open-coscientist.com">guy.barel@open-coscientist.com</a> for privacy questions or a data-rights request.</p><p>Sentry uses the free Developer plan, with retention of 30 days for new error events. Events ingested during the earlier free Business trial retain 90 days. A downgrade does not shorten the retention assigned to earlier events at ingestion.</p><p><a href="/">Research workspace</a></p></article></main>',
  },
  ...examples.map(example => ({
    path: `/examples/${example.id}`,
    title: example.title,
    description: example.description,
    canonical: `${origin}/examples/${example.id}`,
    robots: 'index, follow',
    lastmod: example.lastmod,
    content: `<main><article><h1>${escapeHtml(example.heading)}</h1><h2>Research goal</h2><p>${escapeHtml(example.goal)}</p><section><h2>Example overview</h2><p>${escapeHtml(example.summary)}</p></section><section><h2>Leading hypotheses</h2><ol>${example.ideas
      .map(
        idea =>
          `<li><h3>${escapeHtml(idea.title)}</h3><p>${escapeHtml(idea.statement)}</p></li>`,
      )
      .join(
        '',
      )}</ol></section><p><a href="/">Research workspace</a></p></article></main>`,
  })),
];

for (const route of pages) await writeRoute(route);
await writeFile(
  path.join(dist, 'examples.json'),
  `${JSON.stringify(examples, null, 2)}\n`,
);
await writeFile(
  path.join(dist, 'sitemap.xml'),
  `<?xml version="1.0" encoding="UTF-8"?>\n<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">\n${pages
    .map(xmlUrl)
    .join('\n')}\n</urlset>\n`,
);
await writeFile(
  path.join(dist, 'llms.txt'),
  `# Open Co-Scientist\n\nOpen Co-Scientist is a multi-agent research workspace for creating goals, generating hypotheses, reviewing evidence, ranking ideas, and synthesizing reports. These public examples show research goals, overviews, and leading hypotheses. Full run outputs can be opened in the research workspace.\n\n## Public pages\n\n- [Home](${origin}): Workspace entry point.\n- [Privacy notice](${origin}/privacy): Data use, retention, and privacy rights.\n${examples
    .map(
      example =>
        `- [${example.heading}](${origin}/examples/${example.id}): ${example.goal}
  - Overview: ${example.summary}
  - Leading hypotheses:
${example.ideas
  .map(idea => `    - ${idea.title}: ${idea.statement}`)
  .join('\n')}`,
    )
    .join(
      '\n',
    )}\n\n## Notes\n\n- Private chat, run, and operations routes are not indexable.\n- Generated research output is not medical or clinical guidance.\n`,
);

const privateHtml = renderRoute({
  title: 'Private workspace - Open Co-Scientist',
  description: 'Private workspace pages are not indexed.',
  robots: 'noindex, nofollow',
  content:
    '<main><h1>Private workspace</h1><p>Continue in the same browser that owns this chat or run.</p><p><a href="/">Open the research workspace</a></p></main>',
});
await writeFile(path.join(dist, 'private.html'), privateHtml);

const notFoundHtml = renderRoute({
  title: 'Page not found - Open Co-Scientist',
  description: 'The page you requested does not exist.',
  robots: 'noindex, nofollow',
  content:
    '<main><h1>Page not found</h1><p>The page you requested does not exist.</p><p><a href="/">Return home</a></p></main>',
});
await writeFile(path.join(dist, '404.html'), notFoundHtml);

console.log(
  `Pre-rendered ${pages.length} public routes, a private shell, and 404.html.`,
);
