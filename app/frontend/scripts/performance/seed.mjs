import {mkdir, writeFile} from 'node:fs/promises';
import {resolve} from 'node:path';
import {setTimeout} from 'node:timers/promises';

const api = process.env.FP_API || 'http://127.0.0.1:8108';
if (!['localhost', '127.0.0.1'].includes(new URL(api).hostname))
  throw new Error('Loopback API required');
const state = resolve(process.argv[2] || '/tmp/fp-state');
await mkdir(state, {recursive: true});
async function request(path, data) {
  const response = await fetch(new URL(path, api), {
    method: data === undefined ? 'GET' : 'POST',
    headers: {
      'X-Client-ID': 'fp-performance',
      'Content-Type': 'application/json',
    },
    ...(data === undefined ? {} : {body: JSON.stringify(data)}),
  });
  if (!response.ok) throw new Error(`${path}: ${response.status}`);
  return response.json();
}
const {id} = await request('/api/runs', {
  research_goal:
    'Identify testable enzyme stability hypotheses for an offline performance report',
  tier: 'express',
});
await request(`/api/runs/${id}/start`, {});
let status;
for (let attempt = 0; attempt < 120; attempt++) {
  ({status} = await request(`/api/runs/${id}`));
  if (['completed', 'failed', 'cancelled'].includes(status)) break;
  await setTimeout(500);
}
if (status !== 'completed')
  throw new Error(`Offline report did not complete: ${status}`);
const demos = await request('/api/runs/demo');
if (!demos.runs.length) throw new Error('No curated report seeded');
await writeFile(
  resolve(state, 'routes.json'),
  JSON.stringify({run: id, example: demos.runs[0].id}, null, 2),
);
console.log(`Local report completed: ${id}`);
