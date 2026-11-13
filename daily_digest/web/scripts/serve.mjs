import { createServer } from 'node:http';
import { copyFile } from 'node:fs/promises';
import { openPreviewDatabase } from './sqlite-storage.mjs';
import worker from '../dist/server/index.js';

// 每个发布镜像携带一致性快照；公网没有写接口或发布凭据。
await copyFile('/app/editions.sqlite', '/tmp/editions.sqlite');
const DB = openPreviewDatabase('/tmp/editions.sqlite');
const server = createServer(async (incoming, outgoing) => {
  incoming.resume();
  if (!['GET', 'HEAD'].includes(incoming.method)) {
    outgoing.writeHead(405, { allow: 'GET, HEAD' });
    outgoing.end();
    return;
  }
  if (!incoming.url?.startsWith('/') || incoming.url.startsWith('//')) {
    outgoing.writeHead(400);
    outgoing.end();
    return;
  }
  try {
    const response = await worker.fetch(
      new Request('http://localhost' + incoming.url, { method: 'GET' }),
      { DB },
    );
    outgoing.writeHead(response.status, Object.fromEntries(response.headers));
    outgoing.end(
      incoming.method === 'HEAD' ? undefined : Buffer.from(await response.arrayBuffer()),
    );
  } catch {
    outgoing.writeHead(503, { 'cache-control': 'no-store' });
    outgoing.end('Service unavailable');
  }
});
server.requestTimeout = 15000;
server.headersTimeout = 10000;
server.maxHeadersCount = 50;
server.on('close', () => DB.close());
for (const signal of ['SIGINT', 'SIGTERM']) process.once(signal, () => server.close());
server.listen(8080, '0.0.0.0');
