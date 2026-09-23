import assert from 'node:assert/strict';
import test from 'node:test';

import { installBridge } from './bridge.mjs';

function fixture() {
  const listeners = new Map();
  const replies = [];
  const parent = { postMessage: (...args) => replies.push(args) };
  const frame = {
    parent,
    document: { referrer: 'http://127.0.0.1:7779/' },
    addEventListener: (type, callback) => listeners.set(type, callback),
    removeEventListener: (type) => listeners.delete(type),
  };
  const calls = [];
  const stop = installBridge({
    frame,
    allowedOrigins: ['http://127.0.0.1:7779'],
    runAction: async (...args) => { calls.push(args); return { ok: true }; },
  });
  return { listeners, replies, parent, frame, calls, stop };
}

test('Bridge akzeptiert nur den Mantis-Parent und erlaubten Origin', async () => {
  const f = fixture();
  const command = { source: 'mantis', id: 'a'.repeat(32), action: 'zoom_to_globe', args: {} };
  await f.listeners.get('message')({ source: f.parent, origin: 'https://evil.example', data: command });
  await f.listeners.get('message')({ source: {}, origin: 'http://127.0.0.1:7779', data: command });
  assert.equal(f.calls.length, 0);
  f.stop();
});

test('Bridge führt freigegebene Aktion aus und quittiert sie', async () => {
  const f = fixture();
  const command = { source: 'mantis', id: 'b'.repeat(32), action: 'set_visual_style', args: { style: 'thermal' } };
  await f.listeners.get('message')({ source: f.parent, origin: 'http://127.0.0.1:7779', data: command });
  assert.deepEqual(f.calls, [['set_visual_style', { style: 'thermal' }]]);
  assert.deepEqual(f.replies.at(-1), [{ source: 'gev', id: command.id, result: { ok: true } }, 'http://127.0.0.1:7779']);
  f.stop();
});
