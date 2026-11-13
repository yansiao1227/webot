"""朋友圈发布的本机模拟HTTP、策略与恢复回归；不连接微信。"""
from concurrent.futures import ThreadPoolExecutor
from contextlib import redirect_stdout
import io
import json
from pathlib import Path
import secrets
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch, Mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from wechat_bridge import sns
from wechat_bridge.httpio import HookClient
from wechat_bridge.protocol import Protocol, TOOLS
from wechat_bridge.server import Server
from wechat_bridge.service import Bridge, MENTION_SOURCE
from wechat_bridge.simulation import SimulatedHook


class MomentsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.source = SimulatedHook()
        self.config = {'allow_direct': True, 'min_interval_seconds': 600,
                       'event_conversations': ['wxid_sim_peer', '12345@chatroom']}
        self.bridge = self.open_bridge()

    def open_bridge(self):
        return Bridge(self.temp.name, HookClient(self.source.url), self.source.account,
                      ['wxid_sim_peer', '12345@chatroom'], True, sns_policy=self.config)

    def tearDown(self):
        self.bridge.close()
        self.source.close()
        self.temp.cleanup()

    def publish(self, intent='first', text='模拟公开帖文 😀', **kwargs):
        return sns.publish(self.bridge, intent, text, **kwargs)

    def age(self):
        with self.bridge.store.tx() as db:
            db.execute('UPDATE sns_intents SET created=?', (time.time() - 1000,))

    def test_http_mcp_contract_and_dedup(self):
        server = Server(('127.0.0.1', 0), self.bridge, secrets.token_urlsafe(32))
        thread = threading.Thread(target=server.serve_forever)
        thread.start()
        try:
            client = HookClient(f'http://127.0.0.1:{server.server_port}', server.token)
            body = {'jsonrpc': '2.0', 'id': 1, 'method': 'tools/call', 'params': {
                'name': 'publish_moment', 'arguments': {'intent_id': 'first', 'text': '模拟'}}}
            first = client.request('POST', '/mcp', body)['result']
            second = client.request('POST', '/mcp', body)['result']
            self.assertFalse(first['isError'])
            self.assertEqual(first['structuredContent'], second['structuredContent'])
            self.assertTrue(first['structuredContent']['server_confirmed'])
            sent = self.source.published[0]
            self.assertEqual(set(sent), {'account', 'request_id', 'text', 'visibility'})
            self.assertEqual(sent['account'], self.source.account)
            self.assertRegex(sent['request_id'], r'^[A-Za-z0-9_-]{1,63}$')
            self.assertEqual(len(self.source.published), 1)
            definition = next(t for t in TOOLS if t['name'] == 'publish_moment')
            self.assertTrue(definition['annotations']['idempotentHint'])
            self.assertTrue(definition['annotations']['openWorldHint'])
        finally:
            server.shutdown(); server.server_close(); thread.join()

    def test_default_available_and_explicit_direct_authorization_revocation(self):
        self.bridge.sns_policy = sns.policy()
        self.assertNotIn('enabled', self.bridge.sns_policy)
        self.assertEqual(self.publish()['state'], 'confirmed')
        self.bridge.sns_policy['allow_direct'] = False
        with self.assertRaisesRegex(ValueError, '未授权'):
            self.publish(intent='second')
        self.assertEqual(len(self.source.published), 1)

    def test_event_whitelist_and_trusted_group_mention(self):
        self.bridge.sns_policy['allow_direct'] = False
        self.bridge.sns_policy['event_conversations'] = ['12345@chatroom']
        event = self.source.inject('wxid_sim_peer', '发朋友圈')
        self.bridge.store.ingest([event])
        with self.assertRaises(ValueError):
            self.publish(event_id=event['event_id'])
        event = self.source.inject('12345@chatroom', '@自己 发朋友圈', mentions={
            'state': 'known', 'source': MENTION_SOURCE, 'wxids': [], 'all': True})
        self.bridge.store.ingest([event])
        with self.assertRaises(ValueError):
            self.publish(event_id=event['event_id'])
        event = self.source.inject('12345@chatroom', '发朋友圈', mentions={
            'state': 'known', 'source': MENTION_SOURCE, 'wxids': [self.source.account], 'all': False})
        self.bridge.store.ingest([event])
        self.assertEqual(self.publish(event_id=event['event_id'])['state'], 'confirmed')

    def test_account_changed_and_invalid_content_never_posts(self):
        for text in ('', ' ', '\x00', '\x01', 'a' * 65537, '\ud800'):
            with self.assertRaises((ValueError, UnicodeError)):
                self.publish(text=text)
        self.source.account = 'wxid_other'
        with self.assertRaisesRegex(ValueError, '账号不匹配'):
            self.publish()
        self.assertFalse(self.source.published)

    def test_conflict_and_restart_keep_request_id(self):
        first = self.publish()
        with self.assertRaisesRegex(ValueError, '不同'):
            self.publish(text='不同内容')
        self.bridge.close()
        self.bridge = self.open_bridge()
        self.assertEqual(self.publish(), first)
        self.bridge.sns_policy['allow_direct'] = False
        self.assertEqual(sns.status(self.bridge, 'first'), first)
        self.assertEqual(len(self.source.published), 1)

    def test_concurrent_same_intent_submits_once(self):
        with ThreadPoolExecutor(max_workers=6) as pool:
            results = list(pool.map(lambda _: self.publish(), range(6)))
        self.assertTrue(all(r == results[0] for r in results))
        self.assertEqual(len(self.source.published), 1)

    def test_concurrent_distinct_intents_reserve_one_quota(self):
        def attempt(index):
            try:
                return self.publish(intent=str(index))['state']
            except ValueError:
                return 'rejected'
        with ThreadPoolExecutor(max_workers=6) as pool:
            results = list(pool.map(attempt, range(6)))
        self.assertEqual(results.count('confirmed'), 1)
        self.assertEqual(len(self.source.published), 1)

    def test_rolling_quota_persists_across_restart(self):
        self.bridge.sns_policy['max_writes'] = 1
        self.publish()
        self.age()
        self.config['max_writes'] = 1
        self.bridge.close(); self.bridge = self.open_bridge()
        with self.assertRaisesRegex(ValueError, '额度'):
            self.publish(intent='second')
        self.assertEqual(len(self.source.published), 1)

    def test_timeout_after_actual_submission_only_queries(self):
        original = self.bridge.hook.request
        def lost(method, path, body=None):
            value = original(method, path, body)
            if path == '/client_hook/sns/publish':
                raise TimeoutError()
            return value
        with patch.object(self.bridge.hook, 'request', side_effect=lost):
            unknown = self.publish()
        self.assertEqual(unknown['state'], 'unknown')
        self.assertTrue(sns.policy_status(self.bridge.store)['paused'])
        self.bridge.close(); self.bridge = self.open_bridge()
        self.assertEqual(self.publish()['state'], 'confirmed')
        self.assertTrue(sns.policy_status(self.bridge.store)['paused'])
        self.assertEqual(len(self.source.published), 1)
        with self.assertRaisesRegex(ValueError, '暂停'):
            self.publish(intent='second')

    def test_timeout_before_submission_and_missing_hook_intent_never_reposts(self):
        original = self.bridge.hook.request
        def lost(method, path, body=None):
            if path == '/client_hook/sns/publish':
                raise TimeoutError()
            return original(method, path, body)
        with patch.object(self.bridge.hook, 'request', side_effect=lost):
            self.assertEqual(self.publish()['state'], 'unknown')
        self.assertEqual(self.publish()['state'], 'unknown')
        with self.assertRaisesRegex(ValueError, '未知'):
            sns.set_pause(self.bridge.store, False)
        self.assertEqual(sns.status(self.bridge, 'first')['state'], 'unknown')
        self.assertFalse(self.source.published)

    def test_unknown_blocks_new_id_even_if_legacy_pause_state_was_cleared(self):
        original = self.bridge.hook.request
        def lost(method, path, body=None):
            if path == '/client_hook/sns/publish':
                raise TimeoutError()
            return original(method, path, body)
        with patch.object(self.bridge.hook, 'request', side_effect=lost):
            self.assertEqual(self.publish()['state'], 'unknown')
        self.age()
        # 仅模拟旧版本人工恢复遗留状态；生产恢复入口不允许此操作。
        with self.bridge.store.tx() as db:
            db.execute('UPDATE sns_policy_state SET paused=0')
        with self.assertRaisesRegex(ValueError, '未知'):
            self.publish(intent='changed-id')
        self.bridge.close(); self.bridge = self.open_bridge()
        self.assertTrue(sns.policy_status(self.bridge.store)['paused'])
        self.assertFalse(self.source.published)

    def test_resume_rejects_all_unresolved_states_and_preserves_ledger(self):
        self.publish()
        for state in ('submitting', 'queued', 'running', 'accepted', 'unknown'):
            with self.subTest(state=state), self.bridge.store.tx() as db:
                db.execute('UPDATE sns_intents SET state=?', (state,))
            sns.set_pause(self.bridge.store, True)
            with self.assertRaisesRegex(ValueError, '未完成或未知'):
                sns.set_pause(self.bridge.store, False)
            self.assertEqual(sns.row_for(self.bridge, 'first')['state'], state)
            self.assertTrue(sns.policy_status(self.bridge.store)['paused'])

    def unknown_without_submission(self, intent='first', text='模拟旧未知正文'):
        original = self.bridge.hook.request
        def lost(method, path, body=None):
            if path == '/client_hook/sns/publish':
                raise TimeoutError()
            return original(method, path, body)
        with patch.object(self.bridge.hook, 'request', side_effect=lost):
            return self.publish(intent=intent, text=text)

    def test_explicit_close_keeps_unknown_fact_and_allows_only_independent_content(self):
        first = self.unknown_without_submission()
        before = dict(self.bridge.store.db.execute('SELECT * FROM sns_intents').fetchone())
        policy = sns.set_pause(self.bridge.store, False, close_unknown=['first'])
        self.assertFalse(policy['paused'])
        self.assertEqual(policy['closed_unknown_count'], 1)
        self.assertEqual(dict(self.bridge.store.db.execute('SELECT * FROM sns_intents').fetchone()), before)
        stamp = self.bridge.store.db.execute('SELECT closed_at FROM sns_closed_intents').fetchone()[0]
        sns.set_pause(self.bridge.store, False, close_unknown=['first'])
        self.assertEqual(self.bridge.store.db.execute('SELECT closed_at FROM sns_closed_intents').fetchone()[0], stamp)
        self.bridge.close(); self.bridge = self.open_bridge()
        self.assertFalse(sns.policy_status(self.bridge.store)['paused'])
        old = sns.status(self.bridge, 'first')
        self.assertEqual(old['state'], 'unknown')
        self.assertEqual(old['request_id'], first['request_id'])
        self.assertTrue(old['closed_no_retry'])
        self.assertFalse(sns.policy_status(self.bridge.store)['paused'])
        self.assertEqual(self.publish(text='模拟旧未知正文'), old)
        self.assertFalse(self.source.published)
        self.age()
        self.assertEqual(self.publish(intent='new', text='新的独立内容')['state'], 'confirmed')
        self.assertEqual(len(self.source.published), 1)

    def test_closed_content_cannot_repost_after_window_or_with_new_event(self):
        self.unknown_without_submission()
        sns.set_pause(self.bridge.store, False, close_unknown=['first'])
        with self.bridge.store.tx() as db:
            db.execute('UPDATE sns_intents SET created=?', (time.time()-2*self.bridge.sns_policy['window_seconds'],))
        event = self.source.inject('wxid_sim_peer', '新事件也不能重发旧正文')
        self.bridge.store.ingest([event])
        for event_id in (None, event['event_id']):
            with self.assertRaisesRegex(ValueError, '禁止重发记录.*first'):
                self.publish(intent='replacement', text='模拟旧未知正文', event_id=event_id)
        self.assertFalse(self.source.published)

    def test_new_unknown_still_pauses_after_old_unknown_closed(self):
        self.unknown_without_submission()
        sns.set_pause(self.bridge.store, False, close_unknown=['first'])
        self.age()
        self.assertEqual(self.unknown_without_submission('new', '独立新意图')['state'], 'unknown')
        self.assertTrue(sns.policy_status(self.bridge.store)['paused'])
        with self.assertRaisesRegex(ValueError, '未完成或未知'):
            sns.set_pause(self.bridge.store, False)
        self.assertFalse(sns.row_for(self.bridge, 'new')['closed_request_id'])

    def test_invalid_or_partial_close_rolls_back_all_changes(self):
        self.unknown_without_submission()
        for ids in (['first', 'missing'], ['first', 'first']):
            with self.subTest(ids=ids), self.assertRaises(ValueError):
                sns.set_pause(self.bridge.store, False, close_unknown=ids)
            self.assertEqual(self.bridge.store.db.execute('SELECT COUNT(*) FROM sns_closed_intents').fetchone()[0], 0)
            self.assertTrue(sns.policy_status(self.bridge.store)['paused'])
        with self.bridge.store.tx() as db:
            db.execute("INSERT INTO sns_intents SELECT 'second','second-rid',params,evidence,'running',result,created FROM sns_intents")
        with self.assertRaisesRegex(ValueError, '未完成或未知'):
            sns.set_pause(self.bridge.store, False, close_unknown=['first'])
        self.assertEqual(self.bridge.store.db.execute('SELECT COUNT(*) FROM sns_closed_intents').fetchone()[0], 0)
        for state in ('submitting', 'queued', 'running', 'accepted', 'confirmed', 'failed'):
            with self.bridge.store.tx() as db:
                db.execute("UPDATE sns_intents SET state=? WHERE intent_id='first'", (state,))
            with self.subTest(state=state), self.assertRaisesRegex(ValueError, '只能人工结案'):
                sns.set_pause(self.bridge.store, False, close_unknown=['first'])

    def test_closure_binding_mismatch_cannot_bypass_resume_or_restart(self):
        self.unknown_without_submission()
        sns.set_pause(self.bridge.store, False, close_unknown=['first'])
        for column in ('request_id', 'params_sha256'):
            with self.bridge.store.tx() as db:
                db.execute('UPDATE sns_closed_intents SET '+column+"='mismatch'")
            with self.subTest(column=column), self.assertRaises(ValueError):
                sns.set_pause(self.bridge.store, False)
        self.bridge.close(); self.bridge = self.open_bridge()
        self.assertTrue(sns.policy_status(self.bridge.store)['paused'])
        self.assertFalse(sns.status(self.bridge, 'first')['closed_no_retry'])

    def test_closed_status_malformed_or_still_active_pauses(self):
        first = self.unknown_without_submission()
        sns.set_pause(self.bridge.store, False, close_unknown=['first'])
        original = self.bridge.hook.request
        def response(method, path, body=None):
            if path.startswith('/client_hook/sns/requests/'):
                return current
            return original(method, path, body)
        for current in ({'state': 'unknown'},
                        {'account': self.bridge.account, 'request_id': first['request_id'],
                         'action': 'publish', 'state': 'running'}):
            with self.subTest(current=current), patch.object(self.bridge.hook, 'request', side_effect=response):
                sns.status(self.bridge, 'first')
            self.assertTrue(sns.policy_status(self.bridge.store)['paused'])
            if current.get('state') == 'unknown':
                sns.set_pause(self.bridge.store, False)
        with self.assertRaises(ValueError):
            sns.set_pause(self.bridge.store, False)
        self.assertFalse(self.source.published)

    def test_close_unknown_cli_is_local_explicit_resume_only(self):
        from wechat_bridge.cli import main
        self.unknown_without_submission()
        self.bridge.close()
        args = ['bridge.py', 'sns-policy', '--state-dir', self.temp.name,
                '--account', self.source.account, '--simulate']
        with patch.object(sys, 'argv', args+['resume', '--close-unknown', 'first']), redirect_stdout(io.StringIO()):
            main()
        self.bridge = self.open_bridge()
        self.assertFalse(sns.policy_status(self.bridge.store)['paused'])
        self.assertTrue(sns.status(self.bridge, 'first')['closed_no_retry'])
        for action in ('pause', 'status'):
            with self.subTest(action=action), patch.object(sys, 'argv', args+[action, '--close-unknown', 'first']):
                with self.assertRaises(SystemExit):
                    main()
        self.assertFalse(self.source.published)

    def test_missing_or_disabled_native_capability_does_not_reserve_or_post(self):
        original = self.bridge.hook.request
        for status in ({}, {'capabilities': {'sns': {'publish': False}}},
                       {'capabilities': {'sns': {'publish': 'true'}}}):
            def unavailable(method, path, body=None):
                return status if path == '/client_hook/status' else original(method, path, body)
            with self.subTest(status=status), patch.object(self.bridge.hook, 'request', side_effect=unavailable):
                with self.assertRaisesRegex(ValueError, '适配不可用'):
                    self.publish()
            self.assertIsNone(sns.row_for(self.bridge, 'first'))
        self.assertFalse(self.source.published)

    def test_duplicate_text_new_intent_and_event_cannot_republish(self):
        self.publish()
        self.age()
        event = self.source.inject('wxid_sim_peer', '发布相同正文')
        self.bridge.store.ingest([event])
        for event_id in (None, event['event_id']):
            with self.subTest(event_id=event_id), self.assertRaisesRegex(ValueError, '相同帖文.*first'):
                self.publish(intent='new-id', event_id=event_id)
        self.assertIsNone(sns.row_for(self.bridge, 'new-id'))
        self.assertEqual(len(self.source.published), 1)
        self.assertEqual(self.publish(intent='different-text', text='另一条新的模拟正文')['state'], 'confirmed')

    def test_confirmed_then_capture_fault_pauses_without_erasing_confirmation(self):
        first = self.publish()
        original = self.bridge.hook.request
        def unavailable(method, path, body=None):
            if path.startswith('/messages?'):
                return {'events': [], 'capture': {'state': 'waiting_login'}}
            return original(method, path, body)
        with patch.object(self.bridge.hook, 'request', side_effect=unavailable):
            with self.assertRaisesRegex(ValueError, '采集未运行'):
                self.bridge.collect_once(0)
        self.assertEqual(sns.policy_status(self.bridge.store)['pause_reason'], 'upstream_capture_unavailable')
        self.assertEqual(sns.status(self.bridge, 'first'), first)
        self.bridge.close(); self.bridge = self.open_bridge()
        self.assertTrue(sns.policy_status(self.bridge.store)['paused'])
        self.assertEqual(sns.status(self.bridge, 'first'), first)
        # 上游恢复后普通消费仍可推进，但不能自动恢复朋友圈。
        self.bridge.hook.request('POST', '/consumers', {'consumer_id': self.bridge.consumer, 'start': 'earliest'})
        event = self.source.inject('wxid_sim_peer', '恢复后的普通消息')
        self.bridge.collect_once(0)
        self.assertEqual(self.bridge.store.event(event['event_id'])['event_id'], event['event_id'])
        self.assertTrue(sns.policy_status(self.bridge.store)['paused'])
        with self.assertRaisesRegex(ValueError, '暂停'):
            self.publish(intent='new-id', text='禁止自动续发')
        self.assertEqual(len(self.source.published), 1)

    def test_capture_fault_with_events_still_pauses_after_ingest(self):
        self.publish()
        self.bridge.hook.request('POST', '/consumers', {'consumer_id': self.bridge.consumer, 'start': 'earliest'})
        event = self.source.inject('wxid_sim_peer', '故障批次中已经收到的消息')
        original = self.bridge.hook.request
        def fault(method, path, body=None):
            response = original(method, path, body)
            if path.startswith('/messages?'):
                response['capture'] = {'state': 'fault'}
            return response
        with patch.object(self.bridge.hook, 'request', side_effect=fault):
            with self.assertRaisesRegex(ValueError, '采集未运行'):
                self.bridge.collect_once(0)
        self.assertEqual(self.bridge.store.event(event['event_id'])['event_id'], event['event_id'])
        self.assertTrue(sns.policy_status(self.bridge.store)['paused'])

    def test_collector_transport_fault_persists_pause_without_stopping_bridge(self):
        self.publish()
        stop = Mock()
        stop.is_set.side_effect = [False, True]
        with patch.object(self.bridge, 'stop', stop), patch.object(self.bridge.hook, 'request', side_effect=TimeoutError):
            self.bridge.collect_loop()
        self.assertEqual(sns.policy_status(self.bridge.store)['pause_reason'], 'upstream_unavailable')
        self.assertFalse(self.bridge.stop.is_set())
        self.assertEqual(sns.status(self.bridge, 'first')['state'], 'confirmed')

    def test_account_verification_failure_pauses_but_keeps_confirmed_result(self):
        first = self.publish()
        self.source.account = 'wxid_changed'
        with self.assertRaisesRegex(ValueError, '账号不匹配'):
            self.bridge.verify_account()
        self.assertEqual(sns.policy_status(self.bridge.store)['pause_reason'], 'upstream_account_unverified')
        self.assertEqual(sns.status(self.bridge, 'first'), first)
        self.source.account = self.bridge.account
        self.bridge.verify_account()
        self.assertTrue(sns.policy_status(self.bridge.store)['paused'])

    def test_source_fault_without_sns_history_does_not_latch_publish_pause(self):
        sns.source_fault(self.bridge, 'upstream_unavailable')
        self.assertFalse(sns.policy_status(self.bridge.store)['paused'])
        self.assertNotIn('sns_protection', self.bridge.worker_errors)

    def test_pause_storage_failure_keeps_in_process_write_guard(self):
        self.publish()
        self.age()
        with patch.object(self.bridge.store, 'tx', side_effect=OSError('simulated disk failure')):
            with self.assertRaises(OSError):
                sns.source_fault(self.bridge, 'upstream_unavailable')
        self.assertIn('sns_protection', self.bridge.worker_errors)
        with self.assertRaisesRegex(ValueError, '上游不可确认'):
            self.publish(intent='new-id', text='不得在保护落盘失败后继续发布')
        self.assertEqual(len(self.source.published), 1)

    def test_pause_after_reservation_prevents_post_and_preserves_reason(self):
        original = sns.row_for
        def pause_when_reserved(bridge, intent_id):
            row = original(bridge, intent_id)
            if row and row['state'] == 'submitting':
                sns.source_fault(bridge, 'upstream_capture_unavailable')
            return row
        with patch.object(sns, 'row_for', side_effect=pause_when_reserved):
            result = self.publish()
        self.assertEqual(result['state'], 'failed')
        self.assertFalse(result['operation']['result']['submitted'])
        self.assertEqual(sns.policy_status(self.bridge.store)['pause_reason'], 'upstream_capture_unavailable')
        self.assertFalse(self.source.published)

    def test_interrupted_submission_restart_pauses_and_never_replays(self):
        original = self.bridge.hook.request
        def crash(method, path, body=None):
            if path == '/client_hook/sns/publish':
                raise KeyboardInterrupt()
            return original(method, path, body)
        with patch.object(self.bridge.hook, 'request', side_effect=crash):
            with self.assertRaises(KeyboardInterrupt):
                self.publish()
        rid = sns.row_for(self.bridge, 'first')['request_id']
        self.bridge.close(); self.bridge = self.open_bridge()
        self.assertTrue(sns.policy_status(self.bridge.store)['paused'])
        self.assertEqual(sns.status(self.bridge, 'first')['request_id'], rid)
        self.assertFalse(self.source.published)

    def test_failed_or_invalid_completion_pauses(self):
        original = self.bridge.hook.request
        def malformed(method, path, body=None):
            value = original(method, path, body)
            if path == '/client_hook/sns/publish':
                value['result']['post_id'] = '0'
            return value
        with patch.object(self.bridge.hook, 'request', side_effect=malformed):
            self.assertEqual(self.publish()['state'], 'unknown')
        self.assertTrue(sns.policy_status(self.bridge.store)['paused'])

    def test_queued_and_accepted_block_new_intents_and_status_does_not_post(self):
        original = self.bridge.hook.request
        def queued(method, path, body=None):
            value = original(method, path, body)
            if path == '/client_hook/sns/publish':
                value.update(state='queued', server_confirmed=False)
            return value
        with patch.object(self.bridge.hook, 'request', side_effect=queued):
            self.assertEqual(self.publish()['state'], 'queued')
        self.age()
        with self.assertRaisesRegex(ValueError, '未完成'):
            self.publish(intent='second')
        op = self.source.sns_operations[self.source.published[0]['request_id']]['operation']
        op['state'] = 'accepted'
        status = sns.status(self.bridge, 'first')
        self.assertTrue(status['local_accepted'])
        self.assertFalse(status['server_confirmed'])
        self.assertEqual(len(self.source.published), 1)

    def test_policy_config_rejects_invalid_values(self):
        for config in ({'enabled': 1}, {'max_writes': 0}, {'window_seconds': float('nan')},
                       {'event_conversations': 'any'}, {'unexpected': True}):
            with self.assertRaises(ValueError):
                sns.policy(config)
        with self.assertRaises(ValueError):
            Protocol(self.bridge).invoke('publish_moment', {'intent_id': 'id', 'text': 'x', 'visibility': 'private'})

    def test_load_policy_default_override_and_explicit_missing_file(self):
        self.assertEqual(sns.load_policy(self.temp.name), sns.policy())
        path = Path(self.temp.name) / 'sns-policy.json'
        path.write_text(json.dumps({'max_writes': 2}), encoding='utf-8-sig')
        self.assertEqual(sns.load_policy(self.temp.name)['max_writes'], 2)
        override = Path(self.temp.name) / 'custom.json'
        override.write_text('{}', encoding='utf-8')
        self.assertEqual(sns.load_policy(self.temp.name, override), sns.policy())
        with self.assertRaises(FileNotFoundError):
            sns.load_policy(self.temp.name, path.parent / 'missing.json')

    def test_invalid_cli_configuration_closes_simulator(self):
        from wechat_bridge.cli import main
        simulator = Mock(account='wxid_sim_self')
        with patch('wechat_bridge.cli.SimulatedHook', return_value=simulator), patch.object(sys, 'argv',
                ['bridge.py', 'serve', '--simulate', '--transport', 'stdio', '--sns-config',
                 str(Path(self.temp.name) / 'missing.json')]):
            with self.assertRaises(FileNotFoundError):
                main()
        simulator.close.assert_called_once()

    def test_manual_pause_persists_and_cli_resume_preserves_ledger(self):
        from wechat_bridge.cli import main
        self.publish()
        sns.set_pause(self.bridge.store, True)
        self.bridge.close()
        output = io.StringIO()
        with patch.object(sys, 'argv', ['bridge.py', 'sns-policy', '--state-dir', self.temp.name,
                                       '--account', self.source.account, '--simulate', 'status']), redirect_stdout(output):
            main()
        self.assertTrue(json.loads(output.getvalue())['paused'])
        with patch.object(sys, 'argv', ['bridge.py', 'sns-policy', '--state-dir', self.temp.name,
                                       '--account', self.source.account, '--simulate', 'resume']), redirect_stdout(io.StringIO()):
            main()
        self.bridge = self.open_bridge()
        self.assertFalse(sns.policy_status(self.bridge.store)['paused'])
        self.assertEqual(self.publish()['state'], 'confirmed')
        with self.assertRaisesRegex(ValueError, '间隔'):
            self.publish(intent='second')
        self.assertEqual(len(self.source.published), 1)

    def test_explicit_hook_failure_pauses_and_is_not_retried(self):
        original = self.bridge.hook.request
        def failed(method, path, body=None):
            value = original(method, path, body)
            if path == '/client_hook/sns/publish':
                value.update(state='failed', server_confirmed=False)
            return value
        with patch.object(self.bridge.hook, 'request', side_effect=failed):
            self.assertEqual(self.publish()['state'], 'failed')
        self.assertTrue(sns.policy_status(self.bridge.store)['paused'])
        with patch.object(self.bridge.hook, 'request') as request:
            self.assertEqual(sns.status(self.bridge, 'first')['state'], 'failed')
            request.assert_not_called()
        self.assertEqual(len(self.source.published), 1)


if __name__ == '__main__':
    unittest.main()
