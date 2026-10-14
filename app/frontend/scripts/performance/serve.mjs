import {createServer, request} from 'node:http';
import {createSecureServer} from 'node:http2';
import {readFile, stat} from 'node:fs/promises';
import {resolve, extname, sep} from 'node:path';
import {gzipSync} from 'node:zlib';

const root = resolve(process.argv[2] || 'dist');
const port = Number(process.env.FP_PORT || 5273);
const api = new URL(process.env.FP_API || 'http://127.0.0.1:8108');
if (
  api.protocol !== 'http:' ||
  !['localhost', '127.0.0.1'].includes(api.hostname)
) {
  throw new Error('Performance preview requires a loopback API');
}
const rules = [];
let rule;
for (const line of (await readFile(resolve(root, '_headers'), 'utf8')).split(
  '\n',
)) {
  if (!line.trim() || line.trim().startsWith('#')) continue;
  if (!/^\s/.test(line)) {
    rule = {pattern: line.trim(), headers: {}};
    rules.push(rule);
  } else {
    const colon = line.indexOf(':');
    if (!rule || colon < 0) throw new Error('Invalid _headers');
    rule.headers[line.slice(0, colon).trim().toLowerCase()] = line
      .slice(colon + 1)
      .trim();
  }
}
const types = {
  '.html': 'text/html',
  '.js': 'text/javascript',
  '.css': 'text/css',
  '.svg': 'image/svg+xml',
  '.png': 'image/png',
  '.webp': 'image/webp',
  '.woff2': 'font/woff2',
  '.json': 'application/json',
};
const handle = async (req, res) => {
  try {
    const url = new URL(req.url, `http://127.0.0.1:${port}`);
    if (url.pathname.startsWith('/fp-sentry/')) {
      req.resume();
      res.writeHead(204).end();
      return;
    }
    if (/^\/(api(?:\/|$)|status$|health$)/.test(url.pathname)) {
      // Keep the destination fixed and do not follow upstream redirects.
      const upstream = await new Promise((resolve, reject) => {
        const outgoing = request(
          {
            hostname: '127.0.0.1',
            port: api.port || 80,
            path: url.pathname + url.search,
            method: 'GET',
            headers: {
              'X-Client-ID': req.headers['x-client-id'] || 'fp-performance',
              'Accept-Encoding': req.headers['accept-encoding'] || 'identity',
            },
          },
          resolve,
        );
        outgoing.on('error', reject);
        outgoing.end();
      });
      const headers = {
        'content-type': upstream.headers['content-type'] || 'application/json',
        vary: 'Accept-Encoding',
      };
      const chunks = [];
      for await (const chunk of upstream) chunks.push(chunk);
      const body = Buffer.concat(chunks);
      if (upstream.headers['content-encoding'])
        headers['content-encoding'] = upstream.headers['content-encoding'];
      headers['content-length'] = body.length;
      res.writeHead(upstream.statusCode, headers);
      res.end(body);
      return;
    }
    let assetPath = url.pathname;
    let file = resolve(root, '.' + decodeURIComponent(url.pathname));
    if (file !== root && !file.startsWith(root + sep)) {
      res.writeHead(403).end();
      return;
    }
    if (file === root) file = resolve(root, 'index.html');
    try {
      if (!(await stat(file)).isFile()) throw new Error('not a file');
    } catch {
      if (url.pathname.startsWith('/assets/')) {
        res.writeHead(404).end();
        return;
      }
      file = resolve(root, 'index.html');
      assetPath = '/index.html';
    }
    const headers = {
      'content-type': types[extname(file)] || 'application/octet-stream',
      vary: 'Accept-Encoding',
    };
    for (const {pattern, headers: values} of rules) {
      const regex = new RegExp(
        '^' +
          pattern
            .split('*')
            .map(part => part.replace(/[.*+?^${}()|[\]\\]/g, '\\$&'))
            .join('.*') +
          '$',
      );
      if (regex.test(assetPath)) Object.assign(headers, values);
    }
    let body = await readFile(file);
    if (
      /\b gzip\b|\bgzip\b/.test(req.headers['accept-encoding'] || '') &&
      /\.(html|js|css|svg|json)$/.test(file)
    ) {
      body = gzipSync(body, {level: 9});
      headers['content-encoding'] = 'gzip';
    }
    headers['content-length'] = body.length;
    res.writeHead(200, headers);
    res.end(req.method === 'HEAD' ? undefined : body);
  } catch (error) {
    console.error(error.message);
    res.writeHead(500).end();
  }
};
const tlsCert = process.env.FP_TLS_CERT;
const tlsKey = process.env.FP_TLS_KEY;
if (Boolean(tlsCert) !== Boolean(tlsKey))
  throw new Error('Provide both FP_TLS_CERT and FP_TLS_KEY');
const server = tlsCert
  ? createSecureServer(
      {cert: await readFile(tlsCert), key: await readFile(tlsKey)},
      handle,
    )
  : createServer(handle);
server.listen(port, '127.0.0.1', () =>
  console.log(
    `Static performance preview: ${tlsCert ? 'https' : 'http'}://127.0.0.1:${port}`,
  ),
);
