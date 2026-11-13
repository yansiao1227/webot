import test from 'node:test';
import assert from 'node:assert/strict';
import { mkdtempSync, readFileSync, writeFileSync, mkdirSync, rmSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { resolve, join } from 'node:path';
import { spawnSync } from 'node:child_process';
import { fileURLToPath } from 'node:url';
import { api, contentHash } from '../worker/api.js';
import { openPreviewDatabase } from '../scripts/sqlite-storage.mjs';
import { createPreviewServer } from '../scripts/preview.mjs';
import { importEditionIntoDatabase } from '../scripts/edition-import.mjs';
import { parseImportArguments } from '../scripts/import-edition.mjs';

const script = fileURLToPath(new URL('../scripts/import-edition.mjs', import.meta.url));
const apiWorker = { fetch: (request, environment) => api(request, environment) };
const imageBytes = readFileSync(new URL('../src/assets/news-cosmos.webp', import.meta.url));
const digest = Buffer.from(await crypto.subtle.digest('SHA-256', imageBytes)).toString('hex');

async function edition() {
  const value = JSON.parse(
    readFileSync(new URL('./fixtures/edition.preview.json', import.meta.url), 'utf8').replaceAll(
      '2030-01-02',
      '2026-01-02',
    ),
  );
  value.simulation = false;
  value.sources[0].image = {
    url: 'https://example.org/image.webp',
    caption: '主题配图',
    credit: 'Example',
    kind: 'theme',
    asset_id: digest,
    page_url: 'https://example.org/page',
  };
  value.content_hash = await contentHash(value);
  return value;
}

function workspace() {
  const root = mkdtempSync(join(tmpdir(), 'digest-import-'));
  const media = join(root, 'media');
  mkdirSync(media);
  writeFileSync(join(media, digest + '.webp'), imageBytes);
  return { root, media, db: join(root, 'editions.sqlite'), json: join(root, 'edition.json') };
}

function cli(args, cwd = tmpdir()) {
  return spawnSync(process.execPath, [script, ...args], {
    cwd,
    encoding: 'utf8',
    timeout: 10000,
  });
}

test('standalone import exits after validation, media readback and durable SQLite save', async () => {
  const paths = workspace();
  try {
    const value = await edition();
    writeFileSync(paths.json, JSON.stringify(value));
    const result = cli(['--db', paths.db, '--edition', paths.json, '--media', paths.media]);
    assert.equal(result.status, 0, result.stderr);
    assert.equal(result.error, undefined);
    assert.deepEqual(JSON.parse(result.stdout), {
      date: value.date,
      content_hash: value.content_hash,
      databasePath: paths.db,
    });
    assert.equal(result.stdout.includes(value.intro), false);
    const DB = openPreviewDatabase(paths.db);
    try {
      const stored = await api(new Request('http://localhost/api/editions/' + value.date), { DB });
      assert.deepEqual(await stored.json(), value);
      const media = await api(new Request('http://localhost/media/' + digest), { DB });
      assert.deepEqual(Buffer.from(await media.arrayBuffer()), imageBytes);
    } finally {
      DB.close();
    }
    // Stored media is verified again; identical repeat does not require the local files.
    rmSync(join(paths.media, digest + '.webp'));
    const repeated = cli(['--db', paths.db, '--edition', paths.json]);
    assert.equal(repeated.status, 0, repeated.stderr);
    assert.deepEqual(JSON.parse(repeated.stdout), JSON.parse(result.stdout));
  } finally {
    rmSync(paths.root, { recursive: true, force: true });
  }
});

test('CLI conflict exits 3 and preserves the frozen edition and all existing history', async () => {
  const paths = workspace();
  try {
    const value = await edition();
    writeFileSync(paths.json, JSON.stringify(value));
    assert.equal(
      cli(['--db', paths.db, '--edition', paths.json, '--media', paths.media]).status,
      0,
    );
    const changed = { ...value, title: '同日不同版本不能覆盖' };
    changed.content_hash = await contentHash(changed);
    writeFileSync(paths.json, JSON.stringify(changed));
    const conflict = cli(['--db', paths.db, '--edition', paths.json]);
    assert.equal(conflict.status, 3);
    assert.equal(conflict.stdout, '');
    assert.match(conflict.stderr, /"error":"edition_conflict","status":409/);
    const DB = openPreviewDatabase(paths.db);
    try {
      assert.equal((await DB.prepare('SELECT count(*) AS n FROM editions').first()).n, 1);
      assert.equal((await DB.prepare('SELECT count(*) AS n FROM media').first()).n, 1);
      assert.equal(
        (await DB.prepare('SELECT content_hash FROM editions').first()).content_hash,
        value.content_hash,
      );
    } finally {
      DB.close();
    }
  } finally {
    rmSync(paths.root, { recursive: true, force: true });
  }
});

test('simulation and incorrect frozen hashes exit 2 without inserting media or editions', async () => {
  const paths = workspace();
  try {
    const value = await edition();
    for (const invalid of [
      { ...value, simulation: true },
      { ...value, content_hash: '0'.repeat(64) },
    ]) {
      writeFileSync(paths.json, JSON.stringify(invalid));
      const result = cli(['--db', paths.db, '--edition', paths.json, '--media', paths.media]);
      assert.equal(result.status, 2);
      assert.equal(result.stdout, '');
    }
    const DB = openPreviewDatabase(paths.db);
    try {
      assert.equal((await DB.prepare('SELECT count(*) AS n FROM editions').first()).n, 0);
      assert.equal((await DB.prepare('SELECT count(*) AS n FROM media').first()).n, 0);
    } finally {
      DB.close();
    }
  } finally {
    rmSync(paths.root, { recursive: true, force: true });
  }
});

test('formal import verifies actual image bytes before saving the edition', async () => {
  const paths = workspace();
  try {
    const value = await edition();
    writeFileSync(paths.json, JSON.stringify(value));
    const missingMedia = cli(['--db', paths.db, '--edition', paths.json]);
    assert.equal(missingMedia.status, 2);
    assert.match(missingMedia.stderr, /media_directory_required/);
    writeFileSync(join(paths.media, digest + '.webp'), Buffer.from('not an image'));
    const result = cli(['--db', paths.db, '--edition', paths.json, '--media', paths.media]);
    assert.equal(result.status, 2);
    assert.match(result.stderr, /media_import_failed/);
    const DB = openPreviewDatabase(paths.db);
    try {
      assert.equal((await DB.prepare('SELECT count(*) AS n FROM editions').first()).n, 0);
      assert.equal((await DB.prepare('SELECT count(*) AS n FROM media').first()).n, 0);
    } finally {
      DB.close();
    }
  } finally {
    rmSync(paths.root, { recursive: true, force: true });
  }
});

test('database readback must match the frozen body, not merely its hash label', async () => {
  const paths = workspace();
  const DB = openPreviewDatabase(':memory:');
  try {
    const value = await edition();
    const worker = {
      async fetch(request, env) {
        const response = await api(request, env);
        if (request.method === 'GET' && request.url.includes('/api/editions/') && response.ok) {
          const altered = await response.json();
          altered.intro = '存储读回被改变';
          return Response.json(altered);
        }
        return response;
      },
    };
    await assert.rejects(
      importEditionIntoDatabase({ worker, DB, edition: value, mediaDirectory: paths.media }),
      /edition_readback_failed/,
    );
  } finally {
    DB.close();
    rmSync(paths.root, { recursive: true, force: true });
  }
});

test('paths resolve against repository root, with strict argument and failure exit codes', () => {
  const root = resolve(tmpdir(), 'digest-repository');
  assert.deepEqual(
    parseImportArguments(
      ['--db', 'state/edition.sqlite', '--edition', 'edition.json', '--media', 'media'],
      root,
    ),
    {
      databasePath: resolve(root, 'state/edition.sqlite'),
      editionPath: resolve(root, 'edition.json'),
      mediaDirectory: resolve(root, 'media'),
    },
  );
  assert.equal(cli([]).status, 2);
  assert.equal(cli(['--db', ':memory:', '--edition', 'edition.json']).status, 2);
  assert.equal(cli(['--db', 'one', '--db', 'two', '--edition', 'edition.json']).status, 2);
  assert.equal(
    cli(['--db', 'state/missing.sqlite', '--edition', 'missing/edition.json']).status,
    1,
  );
});

test('preview imports before listen and can close repeatedly without leaking its database handle', async () => {
  const instance = createPreviewServer({ worker: apiWorker, databasePath: ':memory:' });
  assert.equal(instance.server.listening, false);
  await instance.close();
  await instance.close();
  assert.equal(instance.server.listening, false);
});
