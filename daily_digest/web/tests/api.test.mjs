import test from 'node:test';
import assert from 'node:assert/strict';
import { DatabaseSync } from 'node:sqlite';
import { readFileSync, existsSync } from 'node:fs';
import { execFileSync } from 'node:child_process';
import { api, canonical, contentHash, validateEdition } from '../worker/api.js';
const token = 'test-publish-token-'.repeat(3);
test('source images accept public originals and reject private or executable URLs', () => {
  const edition = fixture();
  edition.sources[0].image = {
    url: 'https://images.example.com/news.jpg',
    caption: '新闻配图',
    credit: '原文来源',
  };
  assert.doesNotThrow(() => validateEdition(edition, edition.date));
  for (const url of ['http://127.0.0.1/private.jpg', 'javascript:alert(1)']) {
    edition.sources[0].image.url = url;
    assert.throws(() => validateEdition(edition, edition.date));
  }
});
function fixture(date = '2026-01-01') {
  const source = (i) => ({
    id: 's' + i,
    title: '原始来源 ' + i,
    url: 'https://example.com/news/' + i,
    published_at: date + 'T12:00:00+08:00',
    captured_at: date + 'T16:00:00+08:00',
  });
  const article = (i) => ({
    id: 'n' + i,
    title: '新闻事件 ' + i,
    summary: '经过公开来源核实的事件。',
    background: '背景',
    impact: '影响',
    uncertainty: '待核实',
    source_ids: ['s' + i],
  });
  const missing = {
    status: 'unavailable',
    trade_date: null,
    as_of: null,
    source: '公开来源',
    source_url: 'https://example.com/market',
    classification: '行业分类',
    indices: [],
    gainers: [],
    losers: [],
    missing: ['当日数据未取得'],
  };
  return {
    schema_version: 1,
    date,
    cutoff_at: date + 'T16:30:00+08:00',
    title: '每日观察',
    intro: '今日导读',
    teaser: '一'.repeat(100),
    highlights: ['第一项重点', '第二项重点', '第三项重点'],
    articles: { ai: [1, 2, 3].map(article), society: [4, 5, 6].map(article) },
    analysis: [{ title: '深度分析', text: '事实与分析分开。', source_ids: ['s1'] }],
    sources: [1, 2, 3, 4, 5, 6].map(source),
    markets: { a: structuredClone(missing), hk: structuredClone(missing) },
    warnings: ['行情数据暂缺'],
    simulation: false,
    content_hash: '0'.repeat(64),
  };
}
async function signed(date) {
  const e = fixture(date);
  e.content_hash = await contentHash(e);
  return e;
}
function database() {
  const db = new DatabaseSync(':memory:');
  db.exec(readFileSync(new URL('../migrations/0001_editions.sql', import.meta.url), 'utf8'));
  return {
    raw: db,
    prepare(sql) {
      const statement = db.prepare(sql);
      let values = [];
      return {
        bind(...v) {
          values = v;
          return this;
        },
        async first() {
          return statement.get(...values) || null;
        },
        async all() {
          return { results: statement.all(...values) };
        },
        async run() {
          return statement.run(...values);
        },
      };
    },
  };
}
const req = (path, method = 'GET', body, auth = token) =>
  new Request('https://digest.example' + path, {
    method,
    headers: {
      'content-type': 'application/json',
      ...(auth ? { authorization: 'Bearer ' + auth } : {}),
    },
    ...(body ? { body: JSON.stringify(body) } : {}),
  });
test('upload, anonymous readback, same-body retry and immutable conflict', async () => {
  const DB = database(),
    env = { DB, DIGEST_PUBLISH_TOKEN: token };
  try {
    const e = await signed();
    assert.equal((await api(req('/api/editions/' + e.date, 'PUT', e), env)).status, 200);
    assert.equal((await api(req('/api/editions/' + e.date, 'PUT', e), env)).status, 200);
    const fetched = await api(req('/api/editions/' + e.date), env);
    assert.deepEqual(await fetched.json(), e);
    const changed = { ...e, title: '改变内容' };
    changed.content_hash = await contentHash(changed);
    assert.equal((await api(req('/api/editions/' + e.date, 'PUT', changed), env)).status, 409);
    assert.equal(DB.raw.prepare('SELECT count(*) AS n FROM editions').get().n, 1);
  } finally {
    DB.raw.close();
  }
});
test('parallel uploads cannot overwrite each other', async () => {
  const DB = database(),
    env = { DB, DIGEST_PUBLISH_TOKEN: token };
  try {
    const a = await signed();
    const b = { ...a, title: '另一期正文' };
    b.content_hash = await contentHash(b);
    const responses = await Promise.all([
      api(req('/api/editions/' + a.date, 'PUT', a), env),
      api(req('/api/editions/' + a.date, 'PUT', b), env),
    ]);
    assert.deepEqual(responses.map((r) => r.status).sort(), [200, 409]);
  } finally {
    DB.raw.close();
  }
});
test('latest points to largest completed date and archive pages have no overlap', async () => {
  const DB = database(),
    env = { DB, DIGEST_PUBLISH_TOKEN: token };
  try {
    for (const d of ['2026-01-03', '2026-01-01', '2026-01-02'])
      await api(req('/api/editions/' + d, 'PUT', await signed(d)), env);
    assert.equal((await (await api(req('/api/editions/latest'), env)).json()).date, '2026-01-03');
    const page = await (await api(req('/api/editions?limit=2'), env)).json();
    assert.deepEqual(
      page.items.map((x) => x.date),
      ['2026-01-03', '2026-01-02'],
    );
    const tail = await (
      await api(req('/api/editions?limit=2&cursor=' + page.next_cursor), env)
    ).json();
    assert.deepEqual(
      tail.items.map((x) => x.date),
      ['2026-01-01'],
    );
    assert.equal(tail.next_cursor, null);
  } finally {
    DB.raw.close();
  }
});
test('authentication, simulation, digest and body size are enforced', async () => {
  const DB = database(),
    env = { DB, DIGEST_PUBLISH_TOKEN: token };
  try {
    const e = await signed();
    assert.equal((await api(req('/api/editions/' + e.date, 'PUT', e, null), env)).status, 401);
    assert.equal(
      (await api(req('/api/editions/' + e.date, 'PUT', { ...e, simulation: true }), env)).status,
      400,
    );
    assert.equal(
      (
        await api(
          req('/api/editions/' + e.date, 'PUT', { ...e, content_hash: '0'.repeat(64) }),
          env,
        )
      ).status,
      400,
    );
    assert.equal(
      (
        await api(
          req('/api/editions/' + e.date, 'PUT', { ...e, intro: 'x'.repeat(1024 * 1024) }),
          env,
        )
      ).status,
      413,
    );
    assert.equal(DB.raw.prepare('SELECT count(*) AS n FROM editions').get().n, 0);
  } finally {
    DB.raw.close();
  }
});
test('missing editions, unavailable storage and invalid archive cursor are explicit', async () => {
  const DB = database();
  try {
    assert.equal((await api(req('/api/editions/latest'), { DB })).status, 404);
    assert.equal((await api(req('/api/editions/latest'), {})).status, 503);
    assert.equal((await api(req('/api/editions?limit=1.2'), { DB })).status, 400);
    assert.equal((await api(req('/api/editions/2026-02-30'), { DB })).status, 400);
  } finally {
    DB.raw.close();
  }
});
test('source references and minimum independent news are required', () => {
  for (const change of [
    (e) => e.articles.ai.pop(),
    (e) => (e.articles.ai[0].source_ids = ['missing']),
    (e) => (e.sources[0].url = 'javascript:alert(1)'),
    (e) => (e.articles.society[0].source_ids = ['s1']),
    (e) => (e.articles.ai[1].title = e.articles.ai[0].title),
  ]) {
    const e = fixture();
    change(e);
    assert.throws(() => validateEdition(e, e.date));
  }
});
test('stale market dates, nonfinite quotes, inverted sector rankings and wrong signs are refused', () => {
  const base = fixture();
  base.markets.a = {
    status: 'open',
    trade_date: base.date,
    as_of: base.cutoff_at,
    source: '公开来源',
    source_url: 'https://example.com/market',
    classification: '行业分类',
    indices: [{ code: '000001', name: '上证', close: 3000, change_pct: 1.25 }],
    gainers: [
      { code: '01', name: '行业一', change_pct: 2 },
      { code: '02', name: '行业二', change_pct: 1 },
    ],
    losers: [{ code: '03', name: '行业三', change_pct: -2 }],
    missing: [],
  };
  assert.doesNotThrow(() => validateEdition(base, base.date));
  for (const change of [
    (e) => (e.markets.a.trade_date = '2025-12-31'),
    (e) => (e.markets.a.indices[0].close = NaN),
    (e) => e.markets.a.gainers.reverse(),
    (e) => (e.markets.a.losers[0].change_pct = 1),
    (e) => (e.markets.a.status = 'closed'),
  ]) {
    const e = structuredClone(base);
    change(e);
    assert.throws(() => validateEdition(e, e.date));
  }
});
test('canonical JSON and preview fixture hash match Python content implementation', async () => {
  const raw = JSON.parse(
    readFileSync(new URL('./fixtures/edition.preview.json', import.meta.url), 'utf8'),
  );
  assert.equal(await contentHash(raw), raw.content_hash);
  const value = { z: 1.0, a: [-0, 1e-6, 1e-7, 12.345678, '中文与 emoji 😀'] };
  const python = existsSync(new URL('../../content.py', import.meta.url))
    ? execFileSync(
        'python',
        [
          '-B',
          '-X',
          'utf8',
          '-c',
          "import sys,json;sys.path.insert(0,sys.argv[1]);from daily_digest.content import canonical;print(canonical(json.load(sys.stdin)),end='')",
          new URL('../../../', import.meta.url).pathname.replace(/^\/([A-Za-z]:)/, '$1'),
        ],
        { input: JSON.stringify(value), encoding: 'utf8' },
      )
    : '{"a":[0,0.000001,1e-7,12.345678,"中文与 emoji 😀"],"z":1}';
  assert.equal(canonical(value), python);
});

test('duplicate source URLs and sources older than 24 hours cannot enter the durable edition', async () => {
  for (const mutate of [
    (e) => (e.sources[1].url = e.sources[0].url),
    (e) => (e.sources[0].published_at = '2025-12-31T16:29:59+08:00'),
  ]) {
    const DB = database(),
      env = { DB, DIGEST_PUBLISH_TOKEN: token };
    try {
      const e = fixture();
      mutate(e);
      e.content_hash = await contentHash(e);
      assert.equal((await api(req('/api/editions/' + e.date, 'PUT', e), env)).status, 400);
      assert.equal(DB.raw.prepare('SELECT count(*) AS n FROM editions').get().n, 0);
    } finally {
      DB.raw.close();
    }
  }
  const boundary = fixture();
  boundary.sources[0].published_at = '2025-12-31T16:30:00+08:00';
  assert.doesNotThrow(() => validateEdition(boundary, boundary.date));
});
test('source capture must follow publication and cannot exceed the content cutoff', () => {
  for (const mutate of [
    (e) => (e.sources[0].captured_at = '2026-01-01T11:59:59+08:00'),
    (e) => (e.sources[0].captured_at = '2026-01-01T16:30:01+08:00'),
    (e) => (e.sources[0].published_at = '2026-01-01T16:31:00+08:00'),
  ]) {
    const e = fixture();
    mutate(e);
    assert.throws(() => validateEdition(e, e.date));
  }
});
test('partial current indices and explicitly dated unavailable snapshots remain readable', () => {
  const partial = fixture();
  partial.markets.a = {
    ...partial.markets.a,
    status: 'open',
    trade_date: partial.date,
    as_of: partial.cutoff_at,
    indices: [{ code: '000001', name: '上证指数', close: 3500, change_pct: 0.1 }],
    missing: ['其他主要指数暂缺'],
  };
  assert.doesNotThrow(() => validateEdition(partial, partial.date));
  const uncertain = structuredClone(partial);
  uncertain.markets.a.status = 'unavailable';
  uncertain.markets.a.trade_date = '2025-12-31';
  uncertain.markets.a.missing = ['当天休市安排未核实，仅显示最近交易日'];
  assert.doesNotThrow(() => validateEdition(uncertain, uncertain.date));
  uncertain.markets.a.trade_date = null;
  assert.throws(() => validateEdition(uncertain, uncertain.date));
});
