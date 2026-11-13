import test from 'node:test';
import assert from 'node:assert/strict';
import { mkdtempSync, readFileSync, writeFileSync, rmSync, existsSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { execFileSync } from 'node:child_process';
import { createPreviewServer } from '../scripts/preview.mjs';
import { api, contentHash } from '../worker/api.js';
const token = 'preview-only-test-credential-'.repeat(2);
const apiWorker = { fetch: (request, env) => api(request, env) };
async function start(databasePath, secret = token, worker = apiWorker) {
  const instance = createPreviewServer({ databasePath, token: secret, worker });
  await new Promise((done, reject) => {
    instance.server.once('error', reject);
    instance.server.listen(0, '127.0.0.1', done);
  });
  return { ...instance, url: 'http://127.0.0.1:' + instance.server.address().port };
}
async function get(url) {
  return fetch(url, { headers: { connection: 'close' } });
}
async function put(instance, edition, secret = token) {
  return fetch(instance.url + '/api/editions/' + edition.date, {
    method: 'PUT',
    headers: {
      'content-type': 'application/json',
      authorization: 'Bearer ' + secret,
      connection: 'close',
    },
    body: JSON.stringify(edition),
  });
}
async function edition() {
  const value = JSON.parse(
    readFileSync(new URL('./fixtures/edition.preview.json', import.meta.url), 'utf8').replaceAll(
      '2030-01-02',
      '2026-01-02',
    ),
  );
  value.simulation = false;
  value.content_hash = await contentHash(value);
  return value;
}

test('loopback HTTP supports empty history and persistent idempotent edition readback after restart', async () => {
  const directory = mkdtempSync(join(tmpdir(), 'digest-preview-'));
  const db = join(directory, 'preview.sqlite');
  let instance;
  try {
    instance = await start(db);
    assert.deepEqual(await (await get(instance.url + '/api/editions')).json(), {
      items: [],
      next_cursor: null,
    });
    assert.equal((await get(instance.url + '/api/editions/latest')).status, 404);
    const e = await edition();
    assert.equal((await put(instance, e)).status, 200);
    assert.deepEqual(await (await get(instance.url + '/api/editions/' + e.date)).json(), e);
    await instance.close();
    instance = await start(db);
    assert.deepEqual(await (await get(instance.url + '/api/editions/latest')).json(), e);
    assert.equal((await put(instance, e)).status, 200);
    const changed = { ...e, title: '冲突版本' };
    changed.content_hash = await contentHash(changed);
    assert.equal((await put(instance, changed)).status, 409);
    const listing = await (await get(instance.url + '/api/editions')).json();
    assert.equal(listing.items.length, 1);
    assert.equal(listing.items[0].date, e.date);
  } finally {
    if (instance?.server.listening) await instance.close();
    rmSync(directory, { recursive: true, force: true });
  }
});

test('preview is read-only without a configured secret and never auto-inserts simulation samples', async () => {
  const directory = mkdtempSync(join(tmpdir(), 'digest-readonly-'));
  let instance;
  try {
    instance = await start(join(directory, 'preview.sqlite'), null);
    const e = await edition();
    assert.equal((await put(instance, e)).status, 401);
    assert.deepEqual(await (await get(instance.url + '/api/editions')).json(), {
      items: [],
      next_cursor: null,
    });
    await instance.close();
    instance = await start(join(directory, 'preview.sqlite'));
    const simulated = { ...e, simulation: true };
    simulated.content_hash = await contentHash(simulated);
    assert.equal((await put(instance, simulated)).status, 400);
    assert.deepEqual(await (await get(instance.url + '/api/editions')).json(), {
      items: [],
      next_cursor: null,
    });
  } finally {
    if (instance?.server.listening) await instance.close();
    rmSync(directory, { recursive: true, force: true });
  }
});

test('local HTTP bridge rejects unexpected Origin and Host without changing stored data', async () => {
  const directory = mkdtempSync(join(tmpdir(), 'digest-origin-'));
  let instance;
  try {
    instance = await start(join(directory, 'preview.sqlite'));
    const response = await fetch(instance.url + '/api/editions', {
      headers: { origin: 'https://external.example', connection: 'close' },
    });
    assert.equal(response.status, 403);
    assert.deepEqual(await (await get(instance.url + '/api/editions')).json(), {
      items: [],
      next_cursor: null,
    });
  } finally {
    if (instance?.server.listening) await instance.close();
    rmSync(directory, { recursive: true, force: true });
  }
});

test('explicit local import validates hash and conflicts while HTTP remains read-only', async () => {
  const instance = await start(':memory:', null);
  try {
    const e = await edition();
    await instance.importEdition(e);
    await instance.importEdition(e);
    assert.deepEqual(await (await get(instance.url + '/api/editions/' + e.date)).json(), e);
    assert.equal((await put(instance, e)).status, 401);
    await assert.rejects(instance.importEdition({ ...e, title: '摘要不符' }), /400/);
    const changed = { ...e, title: '不同版本' };
    changed.content_hash = await contentHash(changed);
    await assert.rejects(instance.importEdition(changed), /409/);
    const simulated = { ...e, simulation: true };
    simulated.content_hash = await contentHash(simulated);
    await assert.rejects(instance.importEdition(simulated), /400/);
    assert.deepEqual(await (await get(instance.url + '/api/editions/' + e.date)).json(), e);
  } finally {
    await instance.close();
  }
});

test(
  'built React preview and empty formal homepage are served by the standalone local Worker',
  { skip: !existsSync(new URL('../dist/server/index.js', import.meta.url)) },
  async () => {
    const directory = mkdtempSync(join(tmpdir(), 'digest-ui-'));
    let instance;
    try {
      const { default: worker } = await import('../dist/server/index.js');
      instance = await start(join(directory, 'preview.sqlite'), null, worker);
      for (const path of ['/', '/preview', '/archive']) {
        const response = await get(instance.url + path);
        assert.equal(response.status, 200);
        assert.match(response.headers.get('content-type'), /text\/html/);
        assert.match(await response.text(), /每日观察/);
      }
      assert.deepEqual(await (await get(instance.url + '/api/editions')).json(), {
        items: [],
        next_cursor: null,
      });
    } finally {
      if (instance?.server.listening) await instance.close();
      rmSync(directory, { recursive: true, force: true });
    }
  },
);

test('root dotenv defaults resolve DB paths at repository root while process aliases retain precedence', () => {
  const directory = mkdtempSync(join(tmpdir(), 'digest-config-'));
  try {
    writeFileSync(
      join(directory, '.env'),
      'DAILY_DIGEST_PREVIEW_PORT=4173\nDAILY_DIGEST_PREVIEW_DB=state/from-dotenv.sqlite\n',
    );
    const environment = {
      ...process.env,
      PORT: '45123',
      DIGEST_PREVIEW_DB: 'state/from-process.sqlite',
    };
    for (const key of ['DAILY_DIGEST_PREVIEW_PORT', 'DAILY_DIGEST_PREVIEW_DB'])
      delete environment[key];
    const output = execFileSync(
      process.execPath,
      [
        '--input-type=module',
        '-e',
        "import {loadPreviewConfig} from './scripts/preview.mjs';const c=loadPreviewConfig({root:process.argv[1]});console.log(JSON.stringify({port:c.port,databasePath:c.databasePath}));",
        directory,
      ],
      { cwd: new URL('../', import.meta.url), env: environment, encoding: 'utf8' },
    );
    assert.deepEqual(JSON.parse(output), {
      port: 45123,
      databasePath: join(directory, 'state/from-process.sqlite'),
    });
    delete environment.PORT;
    delete environment.DIGEST_PREVIEW_DB;
    const defaults = JSON.parse(
      execFileSync(
        process.execPath,
        [
          '--input-type=module',
          '-e',
          "import {loadPreviewConfig} from './scripts/preview.mjs';const c=loadPreviewConfig({root:process.argv[1]});console.log(JSON.stringify({port:c.port,databasePath:c.databasePath}));",
          directory,
        ],
        { cwd: new URL('../', import.meta.url), env: environment, encoding: 'utf8' },
      ),
    );
    assert.deepEqual(defaults, {
      port: 4173,
      databasePath: join(directory, 'state/from-dotenv.sqlite'),
    });
  } finally {
    rmSync(directory, { recursive: true, force: true });
  }
});
