import assert from 'node:assert/strict';
import {test} from 'node:test';
import {planFiles} from './shards';

test('weighted shards preserve every spec once and include newly added files', () => {
  const files = [
    'slow.spec.ts',
    'medium.spec.ts',
    'fast.spec.ts',
    'new.spec.ts',
  ];
  const weights = {
    'slow.spec.ts': 50,
    'medium.spec.ts': 30,
    'fast.spec.ts': 5,
  };
  const shards = planFiles(files, 2, weights);
  assert.deepEqual(shards, [
    ['fast.spec.ts', 'slow.spec.ts'],
    ['medium.spec.ts', 'new.spec.ts'],
  ]);
  assert.deepEqual(shards.flat().sort(), files.sort());
  assert.equal(new Set(shards.flat()).size, files.length);
  assert.deepEqual(planFiles([...files].reverse(), 2, weights), shards);
});

test('bad plans fail instead of running an empty or duplicate shard', () => {
  assert.throws(() => planFiles(['a.spec.ts'], 2));
  assert.throws(() => planFiles(['a.spec.ts'], 0));
  assert.throws(() => planFiles(['a.spec.ts', 'a.spec.ts'], 2));
  assert.throws(() => planFiles(['a.spec.ts'], 1, {'a.spec.ts': -1}));
});
