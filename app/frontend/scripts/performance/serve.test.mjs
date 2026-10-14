import assert from 'node:assert/strict';
import {spawn} from 'node:child_process';
import {once} from 'node:events';
import {mkdtemp, rm, writeFile} from 'node:fs/promises';
import {createServer, request} from 'node:http';
import {tmpdir} from 'node:os';
import {join} from 'node:path';
import {test} from 'node:test';
import {gzipSync} from 'node:zlib';

const get = (port, path) =>
  new Promise((resolve, reject) => {
    const outgoing = request(
      {hostname: '127.0.0.1', port, path, headers: {'Accept-Encoding': 'gzip'}},
      async response => {
        const chunks = [];
        for await (const chunk of response) chunks.push(chunk);
        resolve({
          status: response.statusCode,
          headers: response.headers,
          body: Buffer.concat(chunks),
        });
      },
    );
    outgoing.on('error', reject);
    outgoing.end();
  });

test('preview fixes API destination, refuses redirect following and preserves compressed bytes', async () => {
  const root = await mkdtemp(join(tmpdir(), 'fp-preview-'));
  const compressed = gzipSync(Buffer.from('offline report '.repeat(200)), {
    level: 6,
  });
  const paths = [];
  const upstream = createServer((req, res) => {
    paths.push(req.url);
    if (req.url === '/api/redirect') {
      res.writeHead(302, {location: 'http://192.0.2.1/private'}).end();
      return;
    }
    res
      .writeHead(200, {
        'content-type': 'application/json',
        'content-encoding': 'gzip',
      })
      .end(compressed);
  });
  const reserve = createServer();
  let preview;
  try {
    await writeFile(join(root, 'index.html'), '<h1>Preview</h1>');
    await writeFile(join(root, '_headers'), '/*\n  Cache-Control: no-cache\n');
    upstream.listen(0, '127.0.0.1');
    await once(upstream, 'listening');
    reserve.listen(0, '127.0.0.1');
    await once(reserve, 'listening');
    const port = reserve.address().port;
    await new Promise(resolve => reserve.close(resolve));
    preview = spawn(
      process.execPath,
      [new URL('./serve.mjs', import.meta.url).pathname, root],
      {
        env: {
          ...process.env,
          FP_PORT: String(port),
          FP_API: `http://127.0.0.1:${upstream.address().port}`,
        },
        stdio: ['ignore', 'pipe', 'pipe'],
      },
    );
    await once(preview.stdout, 'data');
    const normal = await get(port, '/api/report?offline=1');
    assert.equal(normal.status, 200);
    assert.equal(normal.headers['content-encoding'], 'gzip');
    assert.deepEqual(normal.body, compressed);
    const hostile = await get(port, 'http://192.0.2.1/api/report');
    assert.deepEqual(hostile.body, compressed);
    const redirect = await get(port, '/api/redirect');
    assert.equal(redirect.status, 302);
    assert.deepEqual(paths, [
      '/api/report?offline=1',
      '/api/report',
      '/api/redirect',
    ]);
    const html = await get(port, '/runs/offline/overview');
    assert.equal(html.headers['cache-control'], 'no-cache');
  } finally {
    if (preview) {
      preview.kill();
      await once(preview, 'exit');
    }
    upstream.closeAllConnections();
    await new Promise(resolve => upstream.close(resolve));
    if (reserve.listening) await new Promise(resolve => reserve.close(resolve));
    await rm(root, {recursive: true, force: true});
  }
});
