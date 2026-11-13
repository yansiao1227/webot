import test from 'node:test';
import assert from 'node:assert/strict';
import { parseRoute, editionBase, articleHref, brief } from '../src/routes.ts';

test('dated article and analysis links retain their own edition and survive direct navigation', () => {
  const base = editionBase('2026-11-06');
  assert.deepEqual(parseRoute(articleHref(base, 'ai/带空格 id')), {
    kind: 'article',
    date: '2026-11-06',
    sample: false,
    id: 'ai/带空格 id',
  });
  assert.deepEqual(parseRoute(base + '/analysis/2'), {
    kind: 'analysis',
    date: '2026-11-06',
    sample: false,
    id: '2',
  });
  assert.equal(parseRoute(base + '/markets').kind, 'markets');
  assert.equal(parseRoute(base + '/notes').kind, 'notes');
  for (const path of [
    '/unknown',
    '/daily/2026-11-06/article/',
    '/daily/2026-11-06/article/%xx',
    '/daily/2026-11-06/unknown',
  ])
    assert.equal(parseRoute(path).kind, 'missing');
});
test('sample detail pages remain in the simulated edition rather than loading latest real news', () => {
  assert.equal(editionBase('2030-01-02', true), '/preview');
  assert.deepEqual(parseRoute('/preview/article/ai-1'), {
    kind: 'article',
    date: undefined,
    sample: true,
    id: 'ai-1',
  });
  assert.equal(parseRoute('/preview/markets').sample, true);
});
test('front page briefs omit long prose without changing source paragraphs', () => {
  const original = '事实说明。' + '背景与影响，'.repeat(30);
  assert.ok(brief(original, 64).length <= 65);
  assert.equal(brief('短讯。', 64), '短讯。');
  assert.ok(original.length > 100);
});
