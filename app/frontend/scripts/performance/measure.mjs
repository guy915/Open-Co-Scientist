import {createRequire} from 'node:module';
import {mkdir, readFile, writeFile} from 'node:fs/promises';
import {resolve} from 'node:path';
import {createHash, X509Certificate} from 'node:crypto';

const requireTools = createRequire(
  resolve(process.env.FP_TOOLS || '/tmp/fp-tools', 'package.json'),
);
if (requireTools('lighthouse/package.json').version !== '12.8.2') {
  throw new Error('Measurements require Lighthouse 12.8.2');
}
const {default: lighthouse} = await import(requireTools.resolve('lighthouse'));
const {default: desktopConfig} = await import(
  requireTools.resolve('lighthouse/core/config/desktop-config.js')
);
const {launch} = await import(requireTools.resolve('chrome-launcher'));
const {chromium} = requireTools('playwright');
const origin = process.env.FP_ORIGIN || 'http://127.0.0.1:5273';
if (!['localhost', '127.0.0.1'].includes(new URL(origin).hostname))
  throw new Error('Loopback preview required');
const trustedKey = process.env.FP_TLS_CERT
  ? createHash('sha256')
      .update(
        new X509Certificate(
          await readFile(process.env.FP_TLS_CERT),
        ).publicKey.export({type: 'spki', format: 'der'}),
      )
      .digest('base64')
  : undefined;
const output = resolve(process.argv[2] || '/tmp/fp-before');
const routes = JSON.parse(
  await readFile(process.env.FP_ROUTES || '/tmp/fp-state/routes.json', 'utf8'),
);
const pages = {
  home: '/',
  example: `/runs/${routes.example}/overview`,
  run: `/runs/${routes.run}/overview`,
};
await mkdir(output, {recursive: true});
const rows = [];
for (const theme of ['light', 'dark']) {
  const chrome = await launch({
    chromePath: process.env.FP_CHROME || '/usr/bin/chromium',
    chromeFlags: [
      '--headless',
      '--no-sandbox',
      '--disable-dev-shm-usage',
      ...(trustedKey
        ? [`--ignore-certificate-errors-spki-list=${trustedKey}`]
        : []),
      ...(theme === 'dark' ? ['--force-dark-mode'] : []),
    ],
  });
  const browser = await chromium.connectOverCDP(
    `http://127.0.0.1:${chrome.port}`,
  );
  try {
    const context = browser.contexts()[0];
    const page = context.pages()[0];
    const session = await context.newCDPSession(page);
    await page.goto(origin);
    await page.evaluate(
      ({theme}) => {
        localStorage.clear();
        localStorage.setItem('cosci-theme', theme);
        localStorage.setItem('co_scientist_client_id', 'fp-performance');
      },
      {theme},
    );
    for (const device of process.env.FP_SKIP_AUDITS
      ? []
      : (process.env.FP_DEVICES || 'mobile,desktop').split(',')) {
      for (const [name, route] of Object.entries(pages)) {
        // Stop delayed imports before clearing the previous page's cache.
        await page.goto('about:blank');
        await session.send('Network.clearBrowserCache');
        const options = {
          port: chrome.port,
          output: 'json',
          onlyCategories: ['performance'],
          logLevel: 'error',
          disableStorageReset: true,
          extraHeaders: {'X-Client-ID': 'fp-performance'},
        };
        const result = await lighthouse(
          origin + route,
          options,
          device === 'desktop' ? desktopConfig : undefined,
        );
        if (result.lhr.runtimeError)
          throw new Error(JSON.stringify(result.lhr.runtimeError));
        const audits = result.lhr.audits;
        const cachedAsset = audits['network-requests'].details.items.find(
          item =>
            item.url.startsWith(origin + '/assets/') &&
            item.transferSize === 0 &&
            ['Script', 'Stylesheet', 'Font'].includes(item.resourceType),
        );
        if (cachedAsset)
          throw new Error(`Cold audit reused an asset: ${cachedAsset.url}`);
        const resources = audits['resource-summary'].details.items;
        const row = {
          page: name,
          device,
          theme,
          lcp_ms: audits['largest-contentful-paint'].numericValue,
          cls: audits['cumulative-layout-shift'].numericValue,
          tbt_ms: audits['total-blocking-time'].numericValue,
          js_bytes:
            resources.find(item => item.resourceType === 'script')
              ?.transferSize || 0,
          css_bytes:
            resources.find(item => item.resourceType === 'stylesheet')
              ?.transferSize || 0,
          score: result.lhr.categories.performance.score,
          warnings: result.lhr.runWarnings,
        };
        rows.push(row);
        await writeFile(
          resolve(output, `${name}-${device}-${theme}.json`),
          result.report,
        );
        await writeFile(
          resolve(output, 'summary.json'),
          JSON.stringify(rows, null, 2),
        );
        console.log(JSON.stringify(row));
      }
    }
    for (const width of [390, 1440]) {
      await page.setViewportSize({width, height: 900});
      await page.emulateMedia({colorScheme: theme, reducedMotion: 'reduce'});
      for (const [name, route] of Object.entries(pages)) {
        await page.goto(origin + route);
        await page.waitForFunction(
          () => document.documentElement.dataset.theme !== undefined,
        );
        if (name === 'home')
          await page.locator('.reference-composer textarea').waitFor();
        else
          await page.getByRole('heading', {name: /agent insights/i}).waitFor();
        await page.evaluate(() => document.fonts.ready);
        await page.waitForLoadState('networkidle');
        await page.evaluate(
          () =>
            new Promise(resolve =>
              requestAnimationFrame(() => requestAnimationFrame(resolve)),
            ),
        );
        await page.screenshot({
          animations: 'disabled',
          path: resolve(output, `${name}-${width}-${theme}.png`),
        });
        if (name === 'home' && width === 1440) {
          await page.locator('#landing').scrollIntoViewIfNeeded();
          await page.locator('#ucs-landing-word').waitFor();
          await page.evaluate(() => {
            const container = document.querySelector('.ucs-page--home');
            const landing = document.getElementById('landing');
            container.scrollTop +=
              landing.getBoundingClientRect().top -
              container.getBoundingClientRect().top;
          });
          await page.waitForFunction(() =>
            [...document.images]
              .filter(image => {
                const rect = image.getBoundingClientRect();
                return rect.bottom > 0 && rect.top < innerHeight;
              })
              .every(image => image.complete),
          );
          await page.screenshot({
            animations: 'disabled',
            mask: [page.locator('iframe')],
            path: resolve(output, `landing-${width}-${theme}.png`),
          });
          await page.evaluate(
            () => (document.querySelector('.ucs-page--home').scrollTop = 0),
          );
          await page.screenshot({
            animations: 'disabled',
            path: resolve(output, `home-loaded-${width}-${theme}.png`),
          });
        }
      }
    }
  } finally {
    await browser.close();
    await chrome.kill();
  }
}
