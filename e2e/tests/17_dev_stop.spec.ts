import {execFile} from 'node:child_process';
import {mkdtemp, readFile, rm, writeFile} from 'node:fs/promises';
import {createServer} from 'node:net';
import {tmpdir} from 'node:os';
import {join} from 'node:path';
import {promisify} from 'node:util';
import {expect, test} from '../support/fixtures';
import {API_URL, REPO_ROOT} from '../support/paths';

const execute = promisify(execFile);

test('stopping empty development ports succeeds and leaves isolated browsing usable', async ({
  page,
}) => {
  const directory = await mkdtemp(join(tmpdir(), 'cosci-stop-'));
  const reservations = [createServer(), createServer(), createServer()];
  try {
    // Reserve three unused ports instead of stopping a developer's services.
    for (const server of reservations) {
      await new Promise<void>((resolve, reject) => {
        server.once('error', reject);
        server.listen(0, '127.0.0.1', resolve);
      });
    }
    let source = await readFile(join(REPO_ROOT, 'Makefile'), 'utf8');
    for (const [index, port] of [8008, 5173, 8888].entries()) {
      const address = reservations[index].address();
      if (!address || typeof address === 'string')
        throw new Error('No test port');
      source = source.replaceAll(String(port), String(address.port));
    }
    const makefile = join(directory, 'Makefile');
    await writeFile(makefile, source);
    for (const server of reservations) {
      await new Promise<void>(resolve => server.close(() => resolve()));
    }
    await execute('make', ['--file', makefile, 'stop'], {cwd: REPO_ROOT});
    expect((await page.request.get(`${API_URL}/health`)).ok()).toBe(true);
    await page.goto('/');
    const composer = page.getByRole('textbox').last();
    await expect(composer).toBeInViewport();
    await composer.fill('Investigate immune mechanisms');
    await expect(
      page.getByRole('button', {name: 'Send', exact: true}),
    ).toBeEnabled();
  } finally {
    for (const server of reservations) server.close();
    await rm(directory, {recursive: true, force: true});
  }
});
