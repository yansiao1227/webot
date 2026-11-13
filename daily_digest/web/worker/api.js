const MAX_BODY = 1024 * 1024;
const DATE = /^\d{4}-\d{2}-\d{2}$/;
const HASH = /^[a-f0-9]{64}$/;
export class InputError extends Error {
  constructor(message, status = 400) {
    super(message);
    this.status = status;
  }
}
export function canonical(value) {
  if (Array.isArray(value)) return '[' + value.map(canonical).join(',') + ']';
  if (value && typeof value === 'object')
    return (
      '{' +
      Object.keys(value)
        .sort()
        .map((k) => JSON.stringify(k) + ':' + canonical(value[k]))
        .join(',') +
      '}'
    );
  return JSON.stringify(value);
}
export async function contentHash(value) {
  const copy = { ...value };
  delete copy.content_hash;
  const digest = await crypto.subtle.digest('SHA-256', new TextEncoder().encode(canonical(copy)));
  return Array.from(new Uint8Array(digest), (x) => x.toString(16).padStart(2, '0')).join('');
}
const assert = (test, message) => {
  if (!test) throw new InputError(message);
};
const string = (v, max = 20000) => typeof v === 'string' && v.length <= max && !v.includes('\0');
const present = (v, max = 20000) => string(v, max) && v.trim().length > 0;
export const validDate = (v) =>
  typeof v === 'string' &&
  DATE.test(v) &&
  !Number.isNaN(Date.parse(v)) &&
  new Date(v).toISOString().slice(0, 10) === v;
const iso = (v) =>
  typeof v === 'string' &&
  /^\d{4}-\d{2}-\d{2}T.+(?:Z|[+-]\d{2}:\d{2})$/.test(v) &&
  Number.isFinite(Date.parse(v));
const day = (iso) => new Date(Date.parse(iso) + 8 * 3600000).toISOString().slice(0, 10);
const finite = (v) => typeof v === 'number' && Number.isFinite(v);
const strings = (v, max = 50) =>
  Array.isArray(v) && v.length <= max && v.every((x) => present(x, 2000));
export function publicUrl(v) {
  try {
    const u = new URL(v);
    return (
      ['http:', 'https:'].includes(u.protocol) &&
      !u.username &&
      !u.password &&
      !u.hash &&
      u.hostname.includes('.') &&
      !/^(localhost|.*\.localhost|.*\.local|127\.|10\.|192\.168\.|169\.254\.|0\.|172\.(1[6-9]|2\d|3[01])\.)/.test(
        u.hostname,
      )
    );
  } catch {
    return false;
  }
}
export function validateEdition(e, date) {
  assert(e && typeof e === 'object' && !Array.isArray(e), '日报必须为对象');
  assert(e.schema_version === 1 && e.simulation === false, '只接收正式日报schema_version=1');
  assert(validDate(e.date) && e.date === date, '日报日期不匹配');
  assert(
    iso(e.cutoff_at) && day(e.cutoff_at) === date && Date.parse(e.cutoff_at) <= Date.now() + 300000,
    '截止时间无效',
  );
  for (const key of ['title', 'intro'])
    assert(present(e[key], key === 'title' ? 160 : 4000), '缺少有效' + key);
  assert(
    present(e.teaser, 500) &&
      Array.from(e.teaser).length >= 80 &&
      Array.from(e.teaser).length <= 150,
    '导读需80至150字',
  );
  assert(strings(e.highlights, 3) && e.highlights.length === 3, '需要三项重点');
  assert(strings(e.warnings, 30), '缺项说明无效');
  assert(
    Array.isArray(e.sources) && e.sources.length >= 1 && e.sources.length <= 100,
    '来源列表无效',
  );
  const ids = new Set();
  const urls = new Set();
  const sourceUrls = new Map();
  for (const s of e.sources) {
    if (s?.image !== undefined) {
      assert(
        s.image &&
          (s.image.kind === 'illustration'
            ? s.image.url === '' &&
              ['technology', 'society-city', 'landscape'].includes(s.image.fallback_key) &&
              !s.image.asset_id
            : publicUrl(s.image.url)) &&
          (!s.image.kind ||
            ['original', 'event', 'theme', 'illustration'].includes(s.image.kind)) &&
          (!s.image.asset_id || HASH.test(s.image.asset_id)) &&
          (!s.image.page_url || publicUrl(s.image.page_url)) &&
          present(s.image.caption, 400) &&
          present(s.image.credit, 400),
        '来源配图无效',
      );
    }
    assert(
      s &&
        present(s.id, 80) &&
        !ids.has(s.id) &&
        present(s.title, 400) &&
        publicUrl(s.url) &&
        !urls.has(s.url),
      '来源标识或网址无效',
    );
    ids.add(s.id);
    urls.add(s.url);
    sourceUrls.set(s.id, s.url);
    assert(
      iso(s.published_at) &&
        iso(s.captured_at) &&
        Date.parse(s.published_at) >= Date.parse(e.cutoff_at) - 86400000 &&
        Date.parse(s.published_at) <= Date.parse(s.captured_at) &&
        Date.parse(s.captured_at) <= Date.parse(e.cutoff_at),
      '来源过期、未来时间或采集时间无效',
    );
  }
  const refs = (v) =>
    Array.isArray(v) &&
    v.length > 0 &&
    v.length <= 20 &&
    new Set(v).size === v.length &&
    v.every((x) => ids.has(x));
  assert(e.articles && typeof e.articles === 'object', '缺少新闻栏目');
  const articleIds = new Set();
  const primarySources = new Set();
  const titles = new Set();
  for (const key of ['ai', 'society']) {
    const articles = e.articles[key];
    assert(
      Array.isArray(articles) && articles.length >= 3 && articles.length <= 8,
      '每个新闻栏目需要3至8条',
    );
    for (const a of articles) {
      assert(
        a &&
          present(a.id, 80) &&
          !articleIds.has(a.id) &&
          present(a.title, 180) &&
          present(a.summary, 4000) &&
          refs(a.source_ids),
        '新闻条目或引用无效',
      );
      assert(
        !primarySources.has(sourceUrls.get(a.source_ids[0])) &&
          !titles.has(a.title.replace(/[\s\p{P}]/gu, '').toLocaleLowerCase()),
        '重复新闻事件',
      );
      primarySources.add(sourceUrls.get(a.source_ids[0]));
      titles.add(a.title.replace(/[\s\p{P}]/gu, '').toLocaleLowerCase());
      articleIds.add(a.id);
      for (const field of ['background', 'impact', 'uncertainty'])
        assert(string(a[field], 4000), '新闻说明无效');
    }
  }
  assert(Array.isArray(e.analysis) && e.analysis.length <= 8, '分析栏目无效');
  for (const a of e.analysis)
    assert(
      a && present(a.title, 180) && present(a.text, 12000) && refs(a.source_ids),
      '分析引用无效',
    );
  assert(e.markets && typeof e.markets === 'object', '缺少行情');
  for (const key of ['a', 'hk']) {
    const m = e.markets[key];
    assert(m && ['open', 'closed', 'unavailable'].includes(m.status), '行情状态无效');
    assert(
      string(m.source, 300) &&
        string(m.source_url, 2000) &&
        (!m.source_url || publicUrl(m.source_url)) &&
        string(m.classification, 200),
      '行情来源无效',
    );
    assert(strings(m.missing, 30), '行情缺项无效');
    assert(
      m.trade_date === null || (validDate(m.trade_date) && m.trade_date <= date),
      '交易日期无效',
    );
    assert(
      m.as_of === null || (iso(m.as_of) && Date.parse(m.as_of) <= Date.parse(e.cutoff_at)),
      '行情时间无效',
    );
    assert(
      Array.isArray(m.indices) &&
        m.indices.length <= 20 &&
        Array.isArray(m.gainers) &&
        Array.isArray(m.losers),
      '行情列表无效',
    );
    if (m.status === 'open')
      assert(
        m.trade_date === date &&
          m.as_of &&
          day(m.as_of) === date &&
          Date.parse(m.as_of) <= Date.parse(e.cutoff_at),
        '拒绝旧交易日行情',
      );
    if (m.status === 'closed')
      assert(
        m.gainers.length === 0 && m.losers.length === 0 && m.trade_date <= date,
        '休市日不生成涨跌榜',
      );
    if (m.status === 'unavailable')
      assert(
        m.gainers.length === 0 &&
          m.losers.length === 0 &&
          m.missing.length > 0 &&
          (!m.indices.length || m.trade_date !== null),
        '缺失行情不得伪造',
      );
    const seen = new Set();
    for (const q of m.indices) {
      assert(
        q &&
          present(q.code, 80) &&
          !seen.has(q.code) &&
          present(q.name, 160) &&
          finite(q.close) &&
          q.close > 0 &&
          finite(q.change_pct) &&
          (!('turnover' in q) || (finite(q.turnover) && q.turnover >= 0)),
        '指数数字无效',
      );
      seen.add(q.code);
    }
    seen.clear();
    for (const [list, sign] of [
      [m.gainers, 1],
      [m.losers, -1],
    ]) {
      assert(list.length <= 5, '行业榜最多5条');
      let prior = sign === 1 ? Infinity : -Infinity;
      for (const q of list) {
        assert(
          q &&
            present(q.code, 80) &&
            !seen.has(q.code) &&
            present(q.name, 160) &&
            finite(q.change_pct) &&
            Math.abs(q.change_pct) <= 100 &&
            q.change_pct * sign > 0 &&
            (sign === 1 ? q.change_pct <= prior : q.change_pct >= prior),
          '行业排名或正负方向无效',
        );
        prior = q.change_pct;
        seen.add(q.code);
      }
    }
  }
  assert(typeof e.content_hash === 'string' && HASH.test(e.content_hash), '内容摘要无效');
  return e;
}
const headers = {
  'content-type': 'application/json; charset=utf-8',
  'cache-control': 'no-store',
  'x-content-type-options': 'nosniff',
};
const json = (v, status = 200) => new Response(JSON.stringify(v), { status, headers });
async function authorized(request, expected) {
  if (typeof expected !== 'string' || expected.length < 32) return false;
  const actual = request.headers.get('authorization') || '';
  const hash = async (value) =>
    new Uint8Array(await crypto.subtle.digest('SHA-256', new TextEncoder().encode(value)));
  const [a, b] = await Promise.all([hash(actual), hash('Bearer ' + expected)]);
  let diff = 0;
  for (let i = 0; i < a.length; i++) diff |= a[i] ^ b[i];
  return diff === 0;
}
async function body(request) {
  if (Number(request.headers.get('content-length')) > MAX_BODY)
    throw new InputError('请求超过1MiB', 413);
  if (!request.headers.get('content-type')?.startsWith('application/json'))
    throw new InputError('需要JSON正文', 415);
  const reader = request.body?.getReader();
  if (!reader) throw new InputError('缺少正文');
  let total = 0;
  const chunks = [];
  while (true) {
    const { value, done } = await reader.read();
    if (done) break;
    total += value.length;
    if (total > MAX_BODY) {
      await reader.cancel();
      throw new InputError('请求超过1MiB', 413);
    }
    chunks.push(value);
  }
  const joined = new Uint8Array(total);
  let offset = 0;
  for (const c of chunks) {
    joined.set(c, offset);
    offset += c.length;
  }
  try {
    return JSON.parse(new TextDecoder('utf-8', { fatal: true }).decode(joined));
  } catch {
    throw new InputError('JSON编码无效');
  }
}
export async function api(request, env) {
  const u = new URL(request.url);
  const route = u.pathname;
  const match = /^\/api\/editions\/(\d{4}-\d{2}-\d{2})$/.exec(route);
  try {
    if (!env.DB) return json({ error: 'storage_unavailable' }, 503);
    const media = /^\/(api\/)?media\/([a-f0-9]{64})$/.exec(route);
    if (media && !media[1] && request.method === 'GET') {
      const row = await env.DB.prepare('SELECT body FROM media WHERE sha256=?')
        .bind(media[2])
        .first();
      return row
        ? new Response(new Uint8Array(row.body), {
            headers: {
              'content-type': 'image/webp',
              'cache-control': 'public,max-age=31536000,immutable',
              'x-content-type-options': 'nosniff',
            },
          })
        : json({ error: 'media_not_found' }, 404);
    }
    if (media && media[1] && request.method === 'PUT') {
      if (!(await authorized(request, env.DIGEST_PUBLISH_TOKEN)))
        return json({ error: 'unauthorized' }, 401);
      if (request.headers.get('content-type') !== 'image/webp')
        throw new InputError('需要WebP', 415);
      const reader = request.body?.getReader();
      if (!reader) throw new InputError('缺少图片');
      const chunks = [];
      let size = 0;
      while (true) {
        const { done, value } = await reader.read();
        if (done) break;
        size += value.length;
        if (size > 512 * 1024) {
          await reader.cancel();
          throw new InputError('图片超过512KiB', 413);
        }
        chunks.push(value);
      }
      const bytes = new Uint8Array(size);
      let offset = 0;
      for (const chunk of chunks) {
        bytes.set(chunk, offset);
        offset += chunk.length;
      }
      const ascii = (a, b) => new TextDecoder().decode(bytes.slice(a, b));
      assert(size >= 30 && ascii(0, 4) === 'RIFF' && ascii(8, 12) === 'WEBP', 'WebP格式无效');
      assert(new DataView(bytes.buffer).getUint32(4, true) + 8 === size, 'WebP长度无效');
      let width = 0,
        height = 0;
      const format = ascii(12, 16);
      if (
        format === 'VP8 ' &&
        size >= 30 &&
        bytes[23] === 0x9d &&
        bytes[24] === 0x01 &&
        bytes[25] === 0x2a
      ) {
        width = new DataView(bytes.buffer).getUint16(26, true) & 0x3fff;
        height = new DataView(bytes.buffer).getUint16(28, true) & 0x3fff;
      } else if (format === 'VP8L' && bytes[20] === 0x2f) {
        const bits = new DataView(bytes.buffer).getUint32(21, true);
        width = (bits & 0x3fff) + 1;
        height = ((bits >>> 14) & 0x3fff) + 1;
      } else if (format === 'VP8X') {
        width = 1 + bytes[24] + (bytes[25] << 8) + (bytes[26] << 16);
        height = 1 + bytes[27] + (bytes[28] << 8) + (bytes[29] << 16);
      }
      assert(width > 0 && height > 0 && width <= 1600 && height <= 1600, '图片尺寸无效');
      const digest = Array.from(new Uint8Array(await crypto.subtle.digest('SHA-256', bytes)), (x) =>
        x.toString(16).padStart(2, '0'),
      ).join('');
      assert(digest === media[2], '图片摘要不匹配');
      await env.DB.prepare('INSERT OR IGNORE INTO media(sha256,body,content_type) VALUES(?,?,?)')
        .bind(digest, bytes, 'image/webp')
        .run();
      return json({ sha256: digest, url: '/media/' + digest });
    }
    if (request.method === 'GET' && route === '/api/editions') {
      const cursor = u.searchParams.get('cursor');
      const limit = Number(u.searchParams.get('limit') || 20);
      assert(
        Number.isInteger(limit) && limit >= 1 && limit <= 50 && (!cursor || validDate(cursor)),
        '分页参数无效',
      );
      const result = await env.DB.prepare(
        'SELECT date,title,cutoff_at,content_hash FROM editions WHERE date < ? ORDER BY date DESC LIMIT ?',
      )
        .bind(cursor || '9999-12-31', limit + 1)
        .all();
      const items = result.results.slice(0, limit);
      return json({ items, next_cursor: result.results.length > limit ? items.at(-1).date : null });
    }
    if (request.method === 'GET' && (route === '/api/editions/latest' || match)) {
      if (match) assert(validDate(match[1]), '日期无效');
      const statement = route.endsWith('/latest')
        ? env.DB.prepare('SELECT body FROM editions ORDER BY date DESC LIMIT 1')
        : env.DB.prepare('SELECT body FROM editions WHERE date=?').bind(match[1]);
      const row = await statement.first();
      return row ? json(JSON.parse(row.body)) : json({ error: 'edition_not_found' }, 404);
    }
    if (request.method === 'PUT' && match) {
      if (!(await authorized(request, env.DIGEST_PUBLISH_TOKEN)))
        return json({ error: 'unauthorized' }, 401);
      assert(validDate(match[1]), '日期无效');
      const edition = validateEdition(await body(request), match[1]);
      assert((await contentHash(edition)) === edition.content_hash, '内容摘要不匹配');
      for (const source of edition.sources) {
        if (
          source.image?.asset_id &&
          !(await env.DB.prepare('SELECT sha256 FROM media WHERE sha256=?')
            .bind(source.image.asset_id)
            .first())
        )
          throw new InputError('请先上传绑定图片');
      }
      await env.DB.prepare(
        'INSERT OR IGNORE INTO editions(date,content_hash,title,cutoff_at,body) VALUES(?,?,?,?,?)',
      )
        .bind(
          edition.date,
          edition.content_hash,
          edition.title,
          edition.cutoff_at,
          JSON.stringify(edition),
        )
        .run();
      const saved = await env.DB.prepare('SELECT content_hash FROM editions WHERE date=?')
        .bind(edition.date)
        .first();
      return saved?.content_hash === edition.content_hash
        ? json({
            date: edition.date,
            content_hash: edition.content_hash,
            url: '/daily/' + edition.date,
          })
        : json({ error: 'edition_conflict' }, 409);
    }
    return json({ error: 'not_found' }, 404);
  } catch (error) {
    return error instanceof InputError
      ? json({ error: 'invalid_edition', message: error.message }, error.status)
      : json({ error: 'storage_unavailable' }, 503);
  }
}
