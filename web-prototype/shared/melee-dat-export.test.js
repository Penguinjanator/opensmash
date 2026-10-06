import test from 'node:test';
import assert from 'node:assert/strict';
import { prepareNativeCostume } from '../../engines/melee/web/lib/native-fit.ts';

test('DAT export requires a standard-format worker response and supports cancellation', async t => {
  const previous = Object.fromEntries(['window', 'Worker', 'fetch'].map(key => [key, Object.getOwnPropertyDescriptor(globalThis, key)]));
  t.after(() => {
    for (const [key, descriptor] of Object.entries(previous)) {
      if (descriptor) Object.defineProperty(globalThis, key, descriptor);
      else delete globalThis[key];
    }
  });
  globalThis.window = {};
  globalThis.fetch = async () => ({ ok: true, json: async () => ({ base: '/engine/source/test' }) });
  let worker;
  globalThis.Worker = class {
    constructor() { worker = this; }
    postMessage(message) { this.request = message; }
    terminate() { this.terminated = true; }
  };
  const entry = { character: 'test', target: 'mario', color: 0, format: 'dat' };
  async function start() {
    const controller = new AbortController();
    const promise = prepareNativeCostume(entry, controller.signal);
    await new Promise(resolve => setImmediate(resolve));
    return { promise, controller };
  }
  const stale = await start();
  assert.equal(worker.request.format, 'dat');
  worker.onmessage({ data: { filename: 'PlMrNr.dat', bytes: new ArrayBuffer(32) } });
  await assert.rejects(stale.promise, /updated game assets/);
  assert.equal(worker.terminated, true);

  const valid = await start();
  worker.onmessage({ data: { format: 'dat', filename: 'PlMrNr.dat', bytes: new Uint8Array([1, 2, 3]).buffer } });
  const file = await valid.promise;
  assert.equal(file.filename, 'PlMrNr.dat');
  assert.deepEqual(new Uint8Array(await file.blob.arrayBuffer()), new Uint8Array([1, 2, 3]));
  assert.equal(worker.terminated, true);

  const cancelled = await start();
  cancelled.controller.abort();
  await assert.rejects(cancelled.promise, { name: 'AbortError' });
  assert.equal(worker.terminated, true);
});
