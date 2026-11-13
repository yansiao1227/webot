import test from 'node:test';
import assert from 'node:assert/strict';
import { frontTitle } from '../src/utils/headline.ts';
import { brief } from '../src/routes.ts';

test('front titles retain the event and preserve qualifications and numeric colons', () => {
  assert.equal(frontTitle('Gemini视频模型更新：从延长片段到减少整段重做'), 'Gemini视频模型更新');
  assert.equal(
    frontTitle('Cosmos 3新增四个检查点，聚焦动作驱动的世界模拟'),
    'Cosmos 3新增四个检查点',
  );
  for (const title of [
    '公司宣布启动上市计划，相关消息尚未证实',
    '网传某公司正式上市，官方否认',
    '新模型支持1:1视频生成',
  ])
    assert.equal(frontTitle(title), title);
  assert.equal(frontTitle('新模型'.repeat(12)).length, 24);
});

test('longer front summaries expose more source text without changing the source', () => {
  const summary = '第一段消息提供新闻背景和事件的基本情况。'.repeat(12);
  const original = summary;
  assert.ok(brief(summary, 180).length > brief(summary, 90).length);
  assert.ok(brief(summary, 120).length > brief(summary, 64).length);
  assert.equal(summary, original);
});
