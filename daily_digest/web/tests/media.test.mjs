import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync, mkdtempSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { DatabaseSync } from 'node:sqlite';
import { api, contentHash } from '../worker/api.js';
import { openPreviewDatabase } from '../scripts/sqlite-storage.mjs';
const token = 'media-test-token-with-at-least-32-characters';
const data = readFileSync(new URL('../src/assets/news-cosmos.webp', import.meta.url));
const hash = async (bytes) =>
  Buffer.from(await crypto.subtle.digest('SHA-256', bytes)).toString('hex');
const request = (digest, bytes, authorized = true) =>
  new Request('http://localhost/api/media/' + digest, {
    method: 'PUT',
    headers: {
      'content-type': 'image/webp',
      ...(authorized ? { authorization: 'Bearer ' + token } : {}),
    },
    body: bytes,
  });
test('media authentication, digest, size, format, idempotence and public bytes', async () => {
  const DB = openPreviewDatabase(':memory:');
  const env = { DB, DIGEST_PUBLISH_TOKEN: token };
  const digest = await hash(data);
  try {
    assert.equal((await api(request(digest, data, false), env)).status, 401);
    assert.equal((await api(request('a'.repeat(64), data), env)).status, 400);
    assert.equal((await api(request(digest, new Uint8Array(512 * 1024 + 1)), env)).status, 413);
    assert.equal((await api(request(digest, new Uint8Array(50)), env)).status, 400);
    for (let i = 0; i < 2; i++) assert.equal((await api(request(digest, data), env)).status, 200);
    const saved = await api(new Request('http://localhost/media/' + digest), env);
    assert.equal(saved.headers.get('content-type'), 'image/webp');
    assert.deepEqual(Buffer.from(await saved.arrayBuffer()), data);
    assert.equal((await DB.prepare('SELECT count(*) AS n FROM media').first()).n, 1);
  } finally {
    DB.close();
  }
});
test('version-one database upgrades without losing editions', async () => {
  const filename = join(mkdtempSync(join(tmpdir(), 'digest-media-')), 'old.sqlite');
  const old = new DatabaseSync(filename);
  old.exec(readFileSync(new URL('../migrations/0001_editions.sql', import.meta.url), 'utf8'));
  old.exec(
    "INSERT INTO editions(date,content_hash,title,cutoff_at,body) VALUES('2026-11-06','oldhash','oldtitle','oldcutoff','{}'); PRAGMA user_version=1",
  );
  old.close();
  const db = openPreviewDatabase(filename);
  try {
    assert.equal((await db.prepare('SELECT title FROM editions').first()).title, 'oldtitle');
  } finally {
    db.close();
  }
});

test('edition requires cached images first and accepts controlled illustration or legacy absence', async () => {
  const DB = openPreviewDatabase(':memory:');
  const env = { DB, DIGEST_PUBLISH_TOKEN: token };
  const edition = JSON.parse(
    readFileSync(new URL('./fixtures/edition.preview.json', import.meta.url), 'utf8').replaceAll(
      '2030-01-02',
      '2026-01-02',
    ),
  );
  edition.simulation = false;
  const digest = await hash(data);
  const put = async () => {
    edition.content_hash = await contentHash(edition);
    return api(
      new Request('http://localhost/api/editions/' + edition.date, {
        method: 'PUT',
        headers: { 'content-type': 'application/json', authorization: 'Bearer ' + token },
        body: JSON.stringify(edition),
      }),
      env,
    );
  };
  try {
    edition.sources[0].image = {
      url: 'https://example.org/image.webp',
      caption: '主题配图',
      credit: 'Example',
      kind: 'theme',
      asset_id: digest,
      page_url: 'https://example.org/page',
    };
    assert.equal((await put()).status, 400);
    assert.equal((await api(request(digest, data), env)).status, 200);
    edition.sources[1].image = {
      kind: 'illustration',
      url: '',
      caption: '主题插画',
      credit: '本地插画',
      fallback_key: 'technology',
    };
    assert.equal((await put()).status, 200);
    assert.equal((await put()).status, 200);
    edition.sources[1].image.fallback_key = '../escape';
    assert.equal((await put()).status, 400);
  } finally {
    DB.close();
  }
});
