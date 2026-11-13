import asyncio
from datetime import datetime
import json
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import AsyncMock, Mock, patch

from daily_digest.publisher import SitePublisher
from daily_digest.runner import DailyDigestRunner, format_moment
from daily_digest.store import BEIJING, DigestStore


def stamp(hour=17, minute=30, day=6):
    return datetime(2026, 11, day, hour, minute, tzinfo=BEIJING).timestamp()


class MomentFormatTests(unittest.TestCase):
    def test_fixed_layout_and_multiline_comments_cannot_add_extra_sections(self):
        text = format_moment({'highlights': ['AI\n观察', '社会观察', '市场观察']},
                             'http://example.org/daily/2026-11-11')
        self.assertEqual(text, '每日观察：http://example.org/daily/2026-11-11\n简评：\n1. AI 观察\n2. 社会观察\n3. 市场观察')

    def test_missing_verified_comments_refuses_to_create_share(self):
        with self.assertRaises(ValueError):
            format_moment({'highlights': []}, 'http://example.org/daily/2026-11-11')


class RunnerTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = DigestStore(self.temp.name, 'wxid_self')
        self.clock = stamp()
        self.edition = {'date': '2026-11-06', 'content_hash': 'a' * 64, 'teaser': '已验证的导读',
                        'highlights': ['AI简评', '社会简评', '市场简评']}
        from daily_digest.examples.build_preview import sample
        fixture = json.loads(json.dumps(sample()).replace('2030-01-02', '2026-11-06'))
        self.packet = {key: fixture[key] for key in ('date', 'cutoff_at', 'sources', 'markets', 'warnings')}
        self.packet['news'] = {key: [{'source_id': article['source_ids'][0]} for article in articles]
                               for key, articles in fixture['articles'].items()}
        self.backend = Mock(create_report_thread=AsyncMock(return_value='report-thread'),
                            generate_report=AsyncMock(return_value=(self.edition, {'turn_id': 'turn-1'})))
        self.publisher = Mock(history=AsyncMock(return_value=[]), upload_media=AsyncMock(), upload=AsyncMock(),
                              verify=AsyncMock(return_value='https://example.org/daily/2026-11-06'))
        self.calls = []

        async def call(name, **arguments):
            self.calls.append((name, arguments))
            if name == 'bridge_status':
                return {'account': 'wxid_self', 'simulation': False}
            return {'intent_id': arguments['intent_id'], 'state': 'confirmed', 'server_confirmed': True}

        self.mcp = Mock(call=AsyncMock(side_effect=call))
        self.config = {'site_url': 'https://example.org', 'mobile_verified_at': '2026-11-05T00:00:00Z'}
        self.collector = Mock(return_value=self.packet)
        self.runner = self.make_runner()
        self.row = self.store.ensure_today(self.clock, choose=lambda _: 0)

    def make_runner(self):
        return DailyDigestRunner(self.store, self.backend, self.mcp, self.publisher, self.config,
                                 collector=self.collector, validator=lambda value, **_: value,
                                 now=lambda: self.clock)

    async def asyncTearDown(self):
        await self.runner.close()
        self.temp.cleanup()

    async def test_image_stage_binds_all_candidates_and_media_precedes_edition(self):
        order = []
        async def media(*args): order.append('media')
        async def edition(*args): order.append('edition')
        self.publisher.upload_media.side_effect = media
        self.publisher.upload.side_effect = edition
        await self.runner.process(self.row['date'])
        packet = self.backend.generate_report.await_args.args[1]
        ids = {n['source_id'] for values in packet['news'].values() for n in values}
        self.assertTrue(all(s.get('image') for s in packet['sources'] if s['id'] in ids))
        self.assertEqual(order, ['media', 'edition'])

    async def test_failed_media_upload_can_resume_without_regeneration(self):
        self.publisher.upload_media.side_effect = RuntimeError('network')
        await self.runner.process(self.row['date'])
        self.assertEqual(self.store.run(self.row['date'])['state'], 'uploading_media')
        self.publisher.upload.assert_not_awaited()
        self.publisher.upload_media.side_effect = None
        await self.runner.process(self.row['date'])
        self.backend.generate_report.assert_awaited_once()
        self.publisher.upload.assert_awaited_once()

    async def test_full_pipeline_posts_once_and_freezes_original_text(self):
        await self.runner.process(self.row['date'])
        row = self.store.run(self.row['date'])
        self.assertEqual(row['state'], 'confirmed')
        self.assertEqual(row['thread_id'], 'report-thread')
        self.assertEqual(row['turn_id'], 'turn-1')
        self.assertEqual(row['post_text'], '每日观察：https://example.org/daily/2026-11-06\n简评：\n1. AI简评\n2. 社会简评\n3. 市场简评')
        await self.runner.tick()
        self.assertEqual(sum(name == 'publish_moment' for name, _ in self.calls), 1)

    def historical_edition(self, date='2026-11-06'):
        from daily_digest.examples.build_preview import sample
        from daily_digest.content import validate_edition
        value = json.loads(json.dumps(sample()).replace('2030-01-02', date))
        value.pop('content_hash', None)
        value['simulation'] = False
        return validate_edition(value)

    async def test_same_day_existing_edition_is_verified_then_recommended_without_generation(self):
        previous = self.historical_edition()
        self.publisher.history.return_value = [previous]
        await self.runner.process(self.row['date'])
        self.collector.assert_not_called()
        self.backend.generate_report.assert_not_called()
        self.publisher.upload_media.assert_not_called()
        self.publisher.upload.assert_not_called()
        self.publisher.verify.assert_awaited_once_with(previous)
        row = self.store.run(self.row['date'])
        self.assertEqual(row['edition']['content_hash'], previous['content_hash'])
        self.assertEqual(row['state'], 'confirmed')
        self.assertEqual(row['intent_id'], self.row['intent_id'])

    async def test_failed_history_sync_does_not_generate_publish_or_forge_sent_records(self):
        self.publisher.history.side_effect = RuntimeError('history unavailable')
        await self.runner.process(self.row['date'])
        self.assertEqual(self.store.run(self.row['date'])['state'], 'pending')
        self.assertEqual(self.store.meta('history_sync')['state'], 'failed')
        self.collector.assert_not_called()
        self.mcp.call.assert_not_called()

    async def test_history_before_today_only_seeds_used_source_exclusions(self):
        previous = self.historical_edition('2026-11-05')
        previous['sources'].append({**previous['sources'][0], 'id': 'unused', 'url': 'https://news.example.org/unselected'})
        previous.pop('content_hash')
        from daily_digest.content import validate_edition
        previous = validate_edition(previous)
        self.publisher.history.return_value = [previous]
        await self.runner.process(self.row['date'])
        self.assertIsNone(self.store.run('2026-11-05'))
        self.assertIsNotNone(self.store.historical_edition('2026-11-05'))
        excluded = self.collector.call_args.args[2]['exclude_urls']
        self.assertNotIn('https://news.example.org/unselected', excluded)
        self.assertTrue(excluded)

    async def test_history_hash_conflict_preserves_original_and_blocks_work(self):
        previous = self.historical_edition()
        self.store.sync_history([previous], self.clock)
        changed = json.loads(json.dumps(previous))
        changed['title'] = '不同内容'
        changed.pop('content_hash')
        from daily_digest.content import validate_edition
        self.publisher.history.return_value = [validate_edition(changed)]
        await self.runner.process(self.row['date'])
        self.assertFalse(self.runner.history_ready)
        self.assertEqual(self.store.historical_edition(previous['date'])['content_hash'], previous['content_hash'])
        self.collector.assert_not_called()

    async def test_pause_during_images_saves_stage_and_does_not_start_report(self):
        async def images(packet, *_, **__):
            self.store.meta('paused', True)
            return packet
        with patch('daily_digest.images.prepare_images', side_effect=images):
            await self.runner.process(self.row['date'])
        self.assertEqual(self.store.run(self.row['date'])['state'], 'imaged')
        self.backend.create_report_thread.assert_not_called()
        self.backend.generate_report.assert_not_called()

    async def test_deadline_during_images_or_thread_preparation_never_submits_report(self):
        async def images(packet, *_, **__):
            self.clock = stamp(18, 0)
            return packet
        with patch('daily_digest.images.prepare_images', side_effect=images):
            await self.runner.process(self.row['date'])
        self.assertEqual(self.store.run(self.row['date'])['state'], 'expired')
        self.backend.create_report_thread.assert_not_called()
        self.backend.generate_report.assert_not_called()
        self.clock = stamp(17, 30)
        self.store.update(self.row['date'], 'imaged', packet=self.packet)
        async def thread(_):
            self.clock = stamp(18, 0)
            return 'already-created-thread'
        self.backend.create_report_thread.side_effect = thread
        await self.runner.process(self.row['date'])
        self.assertEqual(self.store.run(self.row['date'])['state'], 'expired')
        self.assertEqual(self.store.run(self.row['date'])['thread_id'], 'already-created-thread')
        self.backend.generate_report.assert_not_called()

    async def test_pause_during_thread_preparation_keeps_thread_for_resume(self):
        async def thread(_):
            self.store.meta('paused', True)
            return 'prepared-thread'
        self.backend.create_report_thread.side_effect = thread
        await self.runner.process(self.row['date'])
        self.assertEqual(self.store.run(self.row['date'])['state'], 'imaged')
        self.assertEqual(self.store.run(self.row['date'])['thread_id'], 'prepared-thread')
        self.backend.generate_report.assert_not_called()

    async def test_body_generation_receives_absolute_window_deadline(self):
        await self.runner.process(self.row['date'])
        self.assertEqual(self.backend.generate_report.call_args.kwargs['deadline'], self.row['deadline'])

    async def test_explicit_mcp_pause_keeps_frozen_waiting_intent_without_submission(self):
        original = self.mcp.call.side_effect
        async def paused(name, **arguments):
            if name == 'bridge_status':
                self.calls.append((name, arguments))
                return {'account': 'wxid_self', 'simulation': False, 'sns_policy': {'paused': True}}
            self.fail('MCP暂停时不得调用任何发布或意图查询')
        self.mcp.call.side_effect = paused
        await self.runner.process(self.row['date'])
        row = self.store.run(self.row['date'])
        self.assertEqual((row['state'], row['error']), ('waiting', 'mcp_paused'))
        self.assertEqual(row['intent_id'], self.row['intent_id'])
        self.assertIsNone(row['operation'])
        self.assertTrue(row['post_text'])
        self.mcp.call.side_effect = original
        await self.runner.process(self.row['date'])
        final = self.store.run(self.row['date'])
        self.assertEqual(final['state'], 'confirmed')
        self.assertEqual(final['post_text'], row['post_text'])
        self.assertEqual(final['intent_id'], row['intent_id'])

    async def test_mcp_pause_until_deadline_expires_without_unknown_intent(self):
        self.store.update(self.row['date'], 'waiting', post_text='冻结正文', error='mcp_paused')
        self.clock = stamp(18, 0)
        await self.runner.tick()
        self.assertEqual(self.store.run(self.row['date'])['state'], 'expired')
        self.mcp.call.assert_not_called()

    async def test_status_exposes_moment_evidence_without_body_or_nested_private_data(self):
        self.store.update(self.row['date'], 'accepted', post_text='私有正文不应出现在状态', operation={
            'intent_id': self.row['intent_id'], 'request_id': 'stable-request', 'state': 'accepted',
            'server_confirmed': False, 'local_accepted': True, 'private': 'private-transport-detail'})
        status = self.store.status()
        self.assertEqual(status['latest']['moment'], {'state': 'accepted', 'server_confirmed': False,
            'local_accepted': True, 'request_id': 'stable-request', 'error': None})
        serialized = json.dumps(status, ensure_ascii=False)
        self.assertNotIn('私有正文', serialized)
        self.assertNotIn('private-transport-detail', serialized)
        self.store.update(self.row['date'], 'failed', operation={
            'state': 'failed', 'operation': {'result': {'error': {'code': 'native_rejected', 'message': 'private secret'}}}})
        self.assertEqual(self.store.status()['latest']['moment']['error'], 'native_rejected')
        self.assertNotIn('private secret', json.dumps(self.store.status()))

    async def test_paused_after_deadline_still_queries_original_submitted_intent(self):
        self.store.update(self.row['date'], 'unknown', post_text='冻结正文')
        with self.store.tx() as database:
            database.execute('UPDATE runs SET updated=0')
        self.store.meta('paused', True)
        self.clock = stamp(18, 10)
        await self.runner.tick()
        await self.runner.task
        self.assertEqual([name for name, _ in self.calls], ['get_moment_publish_status'])
        self.publisher.history.assert_not_called()
        self.assertTrue(self.store.meta('paused'))

    async def test_next_day_excludes_only_articles_actually_used_on_published_page(self):
        previous = self.store.ensure_today(stamp(day=5), choose=lambda _: 0)
        sources = [{'id': 'used', 'url': 'https://news.example.org/old'},
                   {'id': 'cross', 'url': 'https://news.example.org/cross'},
                   {'id': 'unused', 'url': 'https://news.example.org/unused'}]
        edition = {'sources': sources, 'articles': {'ai': [{'source_ids': ['used', 'cross']}], 'society': []}}
        # MCP失败不撤下已经成功读回的网页，次日仍须排除其中新闻。
        self.store.update(previous['date'], 'failed', edition=edition, post_text='已读回网页')
        not_published = self.store.ensure_today(stamp(day=4), choose=lambda _: 0)
        self.store.update(not_published['date'], 'validated', edition={
            'sources': [{'id': 'collected', 'url': 'https://news.example.org/collected'}],
            'articles': {'ai': [{'source_ids': ['collected']}]}})
        self.config['sources'] = {'exclude_urls': ['https://news.example.org/explicit']}
        await self.runner.process(self.row['date'])
        options = self.collector.call_args.args[2]
        self.assertEqual(set(options['exclude_urls']), {'https://news.example.org/old',
                         'https://news.example.org/cross', 'https://news.example.org/explicit'})
        self.assertEqual(self.config['sources']['exclude_urls'], ['https://news.example.org/explicit'])
        for state in ('expired', 'waiting', 'unknown', 'confirmed'):
            self.store.update(previous['date'], state)
            self.assertEqual(self.store.published_article_urls(self.row['date']),
                             {'https://news.example.org/old', 'https://news.example.org/cross'})

    async def test_website_readback_failure_never_calls_mcp(self):
        self.publisher.verify.side_effect = ValueError('mismatch')
        await self.runner.process(self.row['date'])
        self.assertEqual(self.store.run(self.row['date'])['state'], 'website_unknown')
        self.mcp.call.assert_not_called()

    async def test_upload_response_lost_reads_back_without_second_put(self):
        self.publisher.upload.side_effect = TimeoutError()
        await self.runner.process(self.row['date'])
        self.assertEqual(self.store.run(self.row['date'])['state'], 'confirmed')
        self.publisher.upload.assert_awaited_once()
        self.publisher.verify.assert_awaited_once()

    async def test_unknown_website_write_resumes_only_readback_after_restart(self):
        self.publisher.verify.side_effect = TimeoutError()
        await self.runner.process(self.row['date'])
        self.assertEqual(self.store.run(self.row['date'])['state'], 'website_unknown')
        await self.runner.close()
        self.store = DigestStore(self.temp.name, 'wxid_self')
        self.runner = self.make_runner()
        self.publisher.verify.side_effect = None
        await self.runner.process(self.row['date'])
        self.assertEqual(self.store.run(self.row['date'])['state'], 'confirmed')
        self.publisher.upload.assert_awaited_once()
        self.backend.generate_report.assert_awaited_once()

    async def test_only_one_runner_can_own_same_directory_but_cli_can_read(self):
        another = DigestStore(self.temp.name, 'wxid_self')
        try:
            self.assertEqual(another.status()['account'], 'wxid_self')
            with self.assertRaisesRegex(RuntimeError, '已有执行器'):
                DailyDigestRunner(another, self.backend, self.mcp, self.publisher, self.config,
                                  collector=self.collector, validator=lambda x, **_: x)
            self.assertIsNotNone(another.run(self.row['date']))
        finally:
            another.close()

    async def test_pause_during_collection_does_not_start_model(self):
        def collect(*_):
            self.store.meta('paused', True)
            return self.packet
        self.runner.collector = collect
        await self.runner.process(self.row['date'])
        self.assertEqual(self.store.run(self.row['date'])['state'], 'collected')
        self.backend.generate_report.assert_not_called()
        self.publisher.upload.assert_not_called()

    async def test_random_time_stable_across_restart_and_conflicting_account_rejected(self):
        planned = self.row['planned_at']
        await self.runner.close()
        with self.assertRaisesRegex(ValueError, '账号'):
            DigestStore(self.temp.name, 'wxid_other')
        self.store = DigestStore(self.temp.name, 'wxid_self')
        self.runner = self.make_runner()
        self.assertEqual(self.store.ensure_today(self.clock, choose=lambda _: 3599)['planned_at'], planned)
        self.assertEqual(self.store.run(self.row['date'])['intent_id'], self.row['intent_id'])

    async def test_before_prepare_and_after_deadline_do_not_create_runs(self):
        self.assertIsNone(self.store.ensure_today(stamp(16, 29, day=7)))
        self.assertIsNone(self.store.ensure_today(stamp(18, 0, day=7)))
        self.clock = stamp(18, 0)
        await self.runner.tick()
        self.assertEqual(self.store.run(self.row['date'])['state'], 'expired')
        self.collector.assert_not_called()

    async def test_no_activation_or_pause_blocks_execution(self):
        self.config['mobile_verified_at'] = None
        await self.runner.tick()
        self.assertIsNone(self.runner.task)
        self.config['mobile_verified_at'] = 'verified'
        self.store.meta('paused', True)
        await self.runner.tick()
        self.assertIsNone(self.runner.task)
        self.collector.assert_not_called()

    async def test_newly_paused_after_bridge_check_does_not_publish(self):
        async def call(name, **arguments):
            self.assertEqual(name, 'bridge_status')
            self.store.meta('paused', True)
            return {'account': 'wxid_self', 'simulation': False}
        self.mcp.call.side_effect = call
        await self.runner.process(self.row['date'])
        self.assertEqual(self.store.run(self.row['date'])['state'], 'waiting')
        self.assertEqual(self.mcp.call.await_count, 1)

    async def test_deadline_crossed_during_collection_does_not_generate_or_publish(self):
        def collect(*_):
            self.clock = stamp(18, 1)
            return self.packet
        self.runner.collector = collect
        await self.runner.process(self.row['date'])
        self.assertEqual(self.store.run(self.row['date'])['state'], 'expired')
        self.backend.generate_report.assert_not_called()
        self.mcp.call.assert_not_called()

    async def test_waits_until_original_planned_time(self):
        self.clock = stamp(16, 40)
        await self.runner.process(self.row['date'])
        self.assertEqual(self.store.run(self.row['date'])['state'], 'waiting')
        self.mcp.call.assert_not_called()
        self.clock = stamp(17, 1)
        await self.runner.process(self.row['date'])
        self.assertEqual(self.store.run(self.row['date'])['state'], 'confirmed')
        self.collector.assert_called_once()

    async def test_generation_exception_is_unknown_and_not_replayed(self):
        self.backend.generate_report.side_effect = TimeoutError()
        await self.runner.process(self.row['date'])
        self.assertEqual(self.store.run(self.row['date'])['state'], 'generation_unknown')
        await self.runner.tick()
        self.backend.generate_report.assert_awaited_once()

    async def test_timeout_stage_is_preserved_without_replaying_original_turn(self):
        from wechat_codex.local_agent.codex_backend import ModelTimeout
        self.backend.generate_report.side_effect = ModelTimeout('report-thread', 'original-turn', True, 'total')
        await self.runner.process(self.row['date'])
        row = self.store.run(self.row['date'])
        self.assertEqual(row['error'], 'ModelTimeout:total')
        self.assertEqual(row['turn_id'], 'original-turn')
        await self.runner.tick()
        self.backend.generate_report.assert_awaited_once()

    async def test_insufficient_news_fails_before_model_and_collection_has_total_timeout(self):
        self.collector.return_value = {**self.packet, 'news': {'ai': [], 'society': []}}
        await self.runner.process(self.row['date'])
        self.assertEqual(self.store.run(self.row['date'])['error'], 'insufficient_verified_news')
        self.backend.generate_report.assert_not_called()
        self.store.update(self.row['date'], 'pending')
        self.runner.collector = lambda *_: time.sleep(.05)
        with patch('daily_digest.runner.COLLECTION_TIMEOUT_SECONDS', .001):
            await self.runner.process(self.row['date'])
        self.assertEqual(self.store.run(self.row['date'])['state'], 'failed')
        self.backend.generate_report.assert_not_called()

    async def test_recovery_preserves_model_unknown_and_submission_query_only(self):
        self.store.update(self.row['date'], 'generating', thread_id='old-thread')
        self.store.recover()
        self.assertEqual(self.store.run(self.row['date'])['state'], 'generation_unknown')
        self.store.update(self.row['date'], 'submitting', post_text='frozen')
        with self.store.tx() as db:
            db.execute('UPDATE runs SET updated=0')
        await self.runner.process(self.row['date'])
        self.assertEqual([name for name, _ in self.calls], ['get_moment_publish_status'])
        self.assertEqual(self.store.run(self.row['date'])['post_text'], 'frozen')

    async def test_mcp_lost_response_only_queries_even_after_deadline(self):
        original = self.mcp.call.side_effect
        async def lost(name, **arguments):
            if name == 'publish_moment':
                self.calls.append((name, arguments))
                raise TimeoutError()
            return await original(name, **arguments)
        self.mcp.call.side_effect = lost
        await self.runner.process(self.row['date'])
        self.assertEqual(self.store.run(self.row['date'])['state'], 'unknown')
        self.clock = stamp(18, 10)
        with self.store.tx() as db:
            db.execute('UPDATE runs SET updated=0')
        await self.runner.process(self.row['date'])
        self.assertEqual(self.store.run(self.row['date'])['state'], 'confirmed')
        self.assertEqual(sum(name == 'publish_moment' for name, _ in self.calls), 1)

    async def test_accepted_is_not_confirmed_and_wrong_account_never_posts(self):
        async def wrong(name, **arguments):
            if name == 'bridge_status':
                return {'account': 'wxid_other', 'simulation': False}
            self.fail('unexpected write')
        self.mcp.call.side_effect = wrong
        await self.runner.process(self.row['date'])
        self.assertEqual(self.store.run(self.row['date'])['state'], 'failed')
        self.runner.save_operation(self.row, {'intent_id': self.row['intent_id'], 'state': 'accepted'})
        self.assertEqual(self.store.run(self.row['date'])['state'], 'accepted')


class PublisherTests(unittest.TestCase):
    def test_https_origin_and_secret_validation(self):
        for value in ('http://example.org', 'https://localhost', 'https://127.0.0.1',
                      'https://user:pass@example.org', 'https://example.org/path', 'https://example.org:8443'):
            with self.assertRaises(ValueError):
                SitePublisher(value, 'x' * 32)
        self.assertEqual(SitePublisher('https://example.org/', 'x' * 32).origin, 'https://example.org')

    def test_redirect_does_not_forward_publish_credential(self):
        connection = Mock()
        response = Mock(status=302)
        response.read.return_value = b''
        connection.getresponse.return_value = response
        publisher = SitePublisher('https://example.org', 'secret-' * 8)
        with patch('daily_digest.publisher.http.client.HTTPSConnection', return_value=connection):
            with self.assertRaises(RuntimeError):
                publisher._request('PUT', '/api/editions/2026-11-06', {}, authenticated=True)
        self.assertEqual(connection.request.call_count, 1)
        self.assertEqual(connection.request.call_args.args[3]['User-Agent'], 'DailyObserver/1.0')

    def test_public_readback_uses_honest_agent_without_authorization(self):
        connection = Mock()
        response = Mock(status=200)
        response.read.return_value = b'{"date":"2026-11-06"}'
        response.getheader.return_value = 'application/json'
        connection.getresponse.return_value = response
        publisher = SitePublisher('https://example.org', 'secret-' * 8)
        with patch('daily_digest.publisher.http.client.HTTPSConnection', return_value=connection):
            result = publisher._request('GET', '/api/editions/2026-11-06')
        self.assertEqual(result['date'], '2026-11-06')
        headers = connection.request.call_args.args[3]
        self.assertEqual(headers['User-Agent'], 'DailyObserver/1.0')
        self.assertNotIn('Authorization', headers)


if __name__ == '__main__':
    unittest.main()
