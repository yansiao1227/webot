import { createServer } from 'node:http';
import { Readable } from 'node:stream';
import { fileURLToPath, pathToFileURL } from 'node:url';
import { resolve, dirname } from 'node:path';
import { readFile } from 'node:fs/promises';
import { openPreviewDatabase } from './sqlite-storage.mjs';
import { importEditionIntoDatabase } from './edition-import.mjs';

/** HTTP bridge only: all edition/auth/CRUD behavior remains in the shared Worker. */
export function createPreviewServer({ worker, databasePath, token } = {}) {
  if (!worker || typeof worker.fetch !== 'function')
    throw new Error('Build the Worker before starting preview');
  const DB = openPreviewDatabase(
    databasePath || fileURLToPath(new URL('../state/preview.sqlite', import.meta.url)),
  );
  const environment = { DB, ...(token ? { DIGEST_PUBLISH_TOKEN: token } : {}) };
  const server = createServer(async (incoming, outgoing) => {
    const port = server.address()?.port;
    const host = incoming.headers.host;
    const allowedHosts = new Set(['127.0.0.1:' + port, 'localhost:' + port]);
    const origin = incoming.headers.origin;
    if (
      !allowedHosts.has(host) ||
      (origin && !['http://127.0.0.1:' + port, 'http://localhost:' + port].includes(origin)) ||
      !incoming.url?.startsWith('/') ||
      incoming.url.startsWith('//')
    ) {
      incoming.resume();
      outgoing.writeHead(403, {
        'content-type': 'application/json; charset=utf-8',
        'cache-control': 'no-store',
      });
      outgoing.end(JSON.stringify({ error: 'invalid_host_or_origin' }));
      return;
    }
    try {
      const hasBody = !['GET', 'HEAD'].includes(incoming.method);
      const request = new Request('http://127.0.0.1:' + port + incoming.url, {
        method: incoming.method,
        headers: incoming.headers,
        ...(hasBody ? { body: Readable.toWeb(incoming), duplex: 'half' } : {}),
      });
      const response = await worker.fetch(request, environment);
      outgoing.writeHead(response.status, Object.fromEntries(response.headers));
      outgoing.end(Buffer.from(await response.arrayBuffer()));
      if (!incoming.complete) incoming.resume();
    } catch {
      incoming.resume();
      if (!outgoing.headersSent)
        outgoing.writeHead(500, {
          'content-type': 'application/json; charset=utf-8',
          'cache-control': 'no-store',
        });
      outgoing.end(JSON.stringify({ error: 'preview_request_failed' }));
    }
  });
  server.requestTimeout = 30000;
  server.headersTimeout = 10000;
  server.on('close', () => DB.close());
  server.on('error', () => DB.close());
  let closing;
  return {
    server,
    databasePath: DB.path,
    async importEdition(edition, mediaDirectory) {
      return importEditionIntoDatabase({ worker, DB, edition, mediaDirectory });
    },
    close: () => {
      if (closing) return closing;
      if (!server.listening) {
        DB.close();
        closing = Promise.resolve();
        return closing;
      }
      closing = new Promise((done, reject) =>
        server.close((error) => (error ? reject(error) : done())),
      );
      return closing;
    },
  };
}

export function loadPreviewConfig({
  root = fileURLToPath(new URL('../../../', import.meta.url)),
} = {}) {
  const before = { ...process.env };
  try {
    process.loadEnvFile(resolve(root, '.env'));
  } catch (error) {
    if (error.code !== 'ENOENT') throw error;
  }
  const choose = (...values) =>
    values.find((value) => typeof value === 'string' && value.trim())?.trim();
  // An explicit process alias wins over a namespaced default loaded from .env.
  const port = Number(
    choose(
      before.DAILY_DIGEST_PREVIEW_PORT,
      before.PORT,
      process.env.DAILY_DIGEST_PREVIEW_PORT,
      process.env.PORT,
    ) || 4173,
  );
  if (!Number.isInteger(port) || port < 1 || port > 65535)
    throw new Error('Preview port must be between 1 and 65535');
  const selectedPath = choose(
    before.DAILY_DIGEST_PREVIEW_DB,
    before.DIGEST_PREVIEW_DB,
    process.env.DAILY_DIGEST_PREVIEW_DB,
    process.env.DIGEST_PREVIEW_DB,
  );
  const databasePath = selectedPath
    ? selectedPath === ':memory:'
      ? selectedPath
      : resolve(root, selectedPath)
    : fileURLToPath(new URL('../state/preview.sqlite', import.meta.url));
  return { port, databasePath, token: process.env.DIGEST_PUBLISH_TOKEN };
}

async function main() {
  const { port, databasePath, token } = loadPreviewConfig();
  const { default: worker } = await import('../dist/server/index.js');
  const preview = createPreviewServer({ worker, databasePath, token });
  const args = process.argv.slice(2);
  if (args.length) {
    try {
      if (args.length !== 2 || args[0] !== '--edition')
        throw new Error('Usage: npm run preview -- --edition <edition.json>');
      await preview.importEdition(
        JSON.parse(await readFile(resolve(args[1]), 'utf8')),
        resolve(dirname(args[1]), 'media'),
      );
    } catch (error) {
      await preview.close().catch(() => {});
      throw error;
    }
  }
  preview.server.on('error', () => {
    console.error('Preview server could not start; check the port and state directory.');
    process.exitCode = 1;
  });
  preview.server.listen(port, '127.0.0.1', () => {
    console.log('Local: http://127.0.0.1:' + port + '/');
    console.log('Layout sample: http://127.0.0.1:' + port + '/preview');
    console.log('Only loopback access is available. Writes require DIGEST_PUBLISH_TOKEN.');
  });
  for (const signal of ['SIGINT', 'SIGTERM']) process.once(signal, () => preview.server.close());
}
if (process.argv[1] && pathToFileURL(resolve(process.argv[1])).href === import.meta.url) {
  main().catch((error) => {
    console.error('Preview setup failed:', error.message);
    process.exitCode = 1;
  });
}
