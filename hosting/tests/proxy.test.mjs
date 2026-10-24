import assert from 'node:assert/strict';
import { createServer } from 'node:http';
import { after, before, test } from 'node:test';
import handler from '../../api/proxy.mjs';

let gateway, worker, base, last;
const listen = server => new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
before(async () => {
  worker = createServer(async (req, res) => {
    let body = '';
    for await (const chunk of req) body += chunk;
    last = { headers: req.headers, path: req.url, body, method: req.method };
    res.setHeader('Content-Type', 'application/json');
    res.end(JSON.stringify({ ok: true }));
  });
  await listen(worker);
  process.env.LEGO_WORKER_URL = `http://127.0.0.1:${worker.address().port}`;
  process.env.LEGO_WORKER_KEY = 'test-worker-key';
  gateway = createServer((req, res) => {
    res.status = code => { res.statusCode = code; return res; };
    res.json = value => { res.setHeader('Content-Type', 'application/json'); res.end(JSON.stringify(value)); };
    handler(req, res);
  });
  await listen(gateway);
  base = `http://127.0.0.1:${gateway.address().port}`;
});
after(() => { gateway.close(); worker.close(); });

test('forwards bodies and queries while keeping visitor credentials out of the worker', async () => {
  const response = await fetch(base + '/api/proxy?__route=api/assembly-guides&n=1', {
    method: 'POST', headers: { 'content-type': 'application/json', origin: base, cookie: 'private=visitor', authorization: 'Bearer visitor' },
    body: JSON.stringify({ url: '/files/generated/car.mpd' }),
  });
  assert.equal(response.status, 200);
  assert.deepEqual(await response.json(), { ok: true });
  assert.equal(last.path, '/api/assembly-guides?n=1');
  assert.equal(last.headers['x-lego-worker-key'], 'test-worker-key');
  assert.equal(last.headers.cookie, undefined);
  assert.equal(last.headers.authorization, undefined);
  assert.equal(JSON.parse(last.body).url, '/files/generated/car.mpd');
  assert.equal(response.headers.get('cache-control'), 'private, no-store');
});

test('rejects foreign and malformed origins', async () => {
  for (const origin of ['https://attacker.invalid', 'malformed']) {
    const response = await fetch(base + '/api/proxy?__route=api/chats', { headers: { origin } });
    assert.equal(response.status, 403);
  }
});

test('rejects paths outside the fixed worker routes', async () => {
  for (const route of ['https://attacker.invalid', '/attacker.invalid', 'config/models.json', 'files/../config/models.json']) {
    const response = await fetch(base + '/api/proxy?__route=' + encodeURIComponent(route));
    assert.equal(response.status, 404);
  }
});
