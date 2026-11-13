// Read-only verifier, usable inside a candidate image and against a loopback origin.
import { readFileSync } from 'node:fs';
import { createHash } from 'node:crypto';
import { DatabaseSync } from 'node:sqlite';
import { resolve } from 'node:path';
import { pathToFileURL } from 'node:url';
const sha = value => createHash('sha256').update(value).digest('hex');
const canonical = value => Array.isArray(value) ? '[' + value.map(canonical).join(',') + ']'
  : value && typeof value === 'object' ? '{' + Object.keys(value).sort().map(key => JSON.stringify(key) + ':' + canonical(value[key])).join(',') + '}'
  : JSON.stringify(value);
const check = (condition, message) => { if (!condition) throw new Error(message); };
function edition(raw, date, expected) {
  const value = JSON.parse(raw);
  const copy = { ...value }; delete copy.content_hash;
  check(value.date === date && value.content_hash === expected && sha(canonical(copy)) === expected && value.simulation === false, 'edition_mismatch');
  return value;
}
function snapshot(manifest) {
  const raw = readFileSync('/app/editions.sqlite');
  check(raw.length === manifest.snapshot_size && raw.length <= manifest.max_snapshot_bytes && sha(raw) === manifest.snapshot_sha256, 'snapshot_bytes_mismatch');
  for (const [path, expected] of Object.entries(manifest.files)) check(sha(readFileSync('/app/' + path.replace(/^web\//, ''))) === expected, 'artifact_mismatch');
  const db = new DatabaseSync('/app/editions.sqlite', { readOnly: true });
  try {
    check(Object.values(db.prepare('PRAGMA integrity_check').get())[0] === 'ok', 'sqlite_integrity_failed');
    const rows = db.prepare('SELECT date,content_hash,body FROM editions ORDER BY date').all();
    check(rows.length === Object.keys(manifest.editions).length, 'history_count_mismatch');
    const images = new Set(manifest.media.map(item => item.sha256));
    for (const row of rows) {
      const value = edition(row.body, row.date, manifest.editions[row.date]);
      check(row.content_hash === value.content_hash, 'stored_hash_mismatch');
      for (const source of value.sources) if (source.image?.asset_id) check(images.has(source.image.asset_id), 'bound_media_missing');
    }
    check(db.prepare('SELECT count(*) AS n FROM media').get().n === manifest.media.length, 'media_count_mismatch');
    for (const image of manifest.media) {
      const row = db.prepare('SELECT body,content_type FROM media WHERE sha256=?').get(image.sha256);
      check(row && row.body.length === image.size && row.content_type === 'image/webp' && sha(row.body) === image.sha256, 'media_mismatch');
    }
  } finally { db.close(); }
}
async function http(origin, manifest) {
  check(/^http:\/\/127\.0\.0\.1:\d+$/.test(origin), 'unexpected_check_origin');
  const started = Date.now();
  async function read(path) {
    check(Date.now() - started < 90000, 'check_deadline_exceeded');
    const response = await fetch(origin + path, { redirect: 'error', signal: AbortSignal.timeout(5000) });
    check(response.status === 200, 'http_status_failed');
    const body = new Uint8Array(await response.arrayBuffer());
    check(body.length <= 1048576, 'http_body_limit'); return body;
  }
  const latest = JSON.parse(new TextDecoder().decode(await read('/api/editions/latest')));
  check(latest.date === manifest.latest_date && latest.content_hash === manifest.latest_hash, 'latest_mismatch');
  for (const [date, digest] of Object.entries(manifest.editions)) edition(new TextDecoder().decode(await read('/api/editions/' + date)), date, digest);
  for (const media of manifest.media) { const body = await read('/media/' + media.sha256); check(body.length === media.size && sha(body) === media.sha256, 'http_media_mismatch'); }
  for (const path of ['/', '/archive', '/daily/' + manifest.latest_date]) check((await read(path)).length > 0, 'empty_page');
}
export async function verifyHistory(manifest, readPage) {
  let cursor = null;
  const cursors = new Set();
  for (let pageIndex = 0; pageIndex <= 200; pageIndex++) {
    const page = await readPage('/api/editions?limit=50' + (cursor ? '&cursor=' + cursor : ''));
    check(page && Array.isArray(page.items), 'history_response_invalid');
    for (const item of page.items) check(manifest.editions[item.date] === item.content_hash, 'current_history_missing_or_conflicting');
    cursor = page.next_cursor;
    if (cursor === null) return;
    check(typeof cursor === 'string' && /^\d{4}-\d{2}-\d{2}$/.test(cursor) && !cursors.has(cursor), 'history_cursor_invalid');
    cursors.add(cursor);
  }
  throw new Error('history_page_limit');
}
async function history(origin, manifest) {
  check(/^http:\/\/127\.0\.0\.1:\d+$/.test(origin), 'unexpected_check_origin');
  const started = Date.now();
  await verifyHistory(manifest, async path => {
    check(Date.now() - started < 60000, 'history_deadline_exceeded');
    const response = await fetch(origin + path, { redirect: 'error', signal: AbortSignal.timeout(5000) });
    check(response.status === 200, 'history_http_failed');
    const body = await response.text();
    check(body.length <= 1048576, 'history_body_limit');
    return JSON.parse(body);
  });
}
if (process.argv[1] && pathToFileURL(resolve(process.argv[1])).href === import.meta.url) {
  try {
    const manifest = JSON.parse(readFileSync('/app/manifest.json', 'utf8'));
    if (process.argv[2] === 'snapshot') snapshot(manifest);
    else if (process.argv[2] === 'http') await http(process.argv[3], manifest);
    else if (process.argv[2] === 'history') await history(process.argv[3], manifest);
    else throw new Error('invalid_check_mode');
    console.log('{"verified":true}');
  } catch (error) {
    console.error(error instanceof Error ? error.message : 'release_verification_failed'); process.exitCode = 1;
  }
}
