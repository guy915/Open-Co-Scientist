import {readdirSync, readFileSync} from 'node:fs';
import {dirname, join, resolve} from 'node:path';
import {fileURLToPath} from 'node:url';

const weights: Record<string, number> = JSON.parse(
  readFileSync(new URL('./shard_weights.json', import.meta.url), 'utf8'),
);

export function planFiles(
  files: string[],
  count: number,
  timings: Record<string, number> = weights,
): string[][] {
  if (!Number.isInteger(count) || count < 1 || count > files.length) {
    throw new Error('Shard count must fit the collected spec files');
  }
  if (new Set(files).size !== files.length)
    throw new Error('Duplicate spec file');
  const duration = (file: string) => timings[file] ?? 20;
  if (
    files.some(file => !Number.isFinite(duration(file)) || duration(file) <= 0)
  ) {
    throw new Error('Spec durations must be positive');
  }
  const bins = Array.from({length: count}, () => ({
    files: [] as string[],
    seconds: 0,
  }));
  const ordered = [...files].sort(
    (a, b) => duration(b) - duration(a) || a.localeCompare(b),
  );
  for (const file of ordered) {
    const bin = bins.reduce((best, candidate) =>
      candidate.seconds < best.seconds ? candidate : best,
    );
    bin.files.push(file);
    bin.seconds += duration(file);
  }
  return bins.map(bin => bin.files.sort());
}

function specFiles(directory: string, prefix = ''): string[] {
  return readdirSync(directory, {withFileTypes: true}).flatMap(entry => {
    const relative = prefix + entry.name;
    if (entry.isDirectory())
      return specFiles(join(directory, entry.name), relative + '/');
    return entry.name.endsWith('.spec.ts') ? [relative] : [];
  });
}

export function browserShardFiles(
  shard: string | undefined,
): string[] | undefined {
  if (!shard) return undefined;
  const parsed = /^([1-9]\d*)\/([1-9]\d*)$/.exec(shard);
  if (!parsed || Number(parsed[1]) > Number(parsed[2]))
    throw new Error('Invalid browser shard');
  const directory = resolve(
    dirname(fileURLToPath(import.meta.url)),
    '../tests',
  );
  return planFiles(specFiles(directory), Number(parsed[2]))[
    Number(parsed[1]) - 1
  ].map(file => join(directory, file));
}
