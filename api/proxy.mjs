// Vercel-only gateway. The worker credential never reaches the browser.
import { Readable } from 'node:stream';
import { pipeline } from 'node:stream/promises';

const prefixes = ['/api/', '/files/', '/gallery-files/', '/demo/', '/ldraw/', '/ldraw-id/', '/viewer/', '/xr/'];

export default async function handler(req, res) {
  const base = process.env.LEGO_WORKER_URL;
  const key = process.env.LEGO_WORKER_KEY;
  if (!base || !key) return res.status(503).json({ detail: 'The model builder is not configured.' });
  const incoming = new URL(req.url, 'https://app.invalid');
  const route = incoming.searchParams.get('__route') || '';
  const path = '/' + route;
  incoming.searchParams.delete('__route');
  if (!prefixes.some(prefix => path.startsWith(prefix)) || path.includes('\\') || path.includes('..') || path.startsWith('//')) {
    return res.status(404).json({ detail: 'Not found' });
  }
  // Vercel authenticates the visitor before this function. Keep CSRF checks
  // at this boundary, before translating Origin for the private worker.
  const origin = req.headers.origin;
  const host = req.headers.host;
  let foreignOrigin = false;
  try { foreignOrigin = Boolean(origin && new URL(origin).host !== host); }
  catch { foreignOrigin = true; }
  if (req.headers['sec-fetch-site'] === 'cross-site' || foreignOrigin) {
    return res.status(403).json({ detail: 'Cross-origin requests are not allowed' });
  }
  const target = new URL(path, base);
  target.search = incoming.searchParams.toString();
  const headers = { 'x-lego-worker-key': key };
  for (const name of ['accept', 'content-type', 'range', 'if-none-match', 'if-modified-since']) {
    if (typeof req.headers[name] === 'string') headers[name] = req.headers[name];
  }
  if (origin) headers.origin = target.origin;
  const controller = new AbortController();
  res.on('close', () => controller.abort());
  try {
    const init = { method: req.method, headers, redirect: 'manual', signal: controller.signal };
    if (!['GET', 'HEAD'].includes(req.method)) { init.body = req; init.duplex = 'half'; }
    const upstream = await fetch(target, init);
    res.statusCode = upstream.status;
    for (const name of ['content-type', 'content-disposition', 'etag', 'last-modified', 'content-range', 'accept-ranges', 'x-ldraw-folder']) {
      const value = upstream.headers.get(name);
      if (value) res.setHeader(name, value);
    }
    res.setHeader('Cache-Control', 'private, no-store');
    res.setHeader('X-Content-Type-Options', 'nosniff');
    if (upstream.body) await pipeline(Readable.fromWeb(upstream.body), res);
    else res.end();
  } catch (error) {
    if (controller.signal.aborted) return;
    console.error('Model builder request failed:', error.name);
    if (!res.headersSent) res.status(502).json({ detail: 'The model builder is starting. Please try again shortly.' });
    else res.end();
  }
}

export const config = { api: { bodyParser: false } };
