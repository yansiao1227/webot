import base64
from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import secrets
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from wechat_bridge.httpio import CallbackHTTP, HookClient, RemoteError, encode, signature, verify_signature
from wechat_bridge.protocol import Protocol
from wechat_bridge.server import Server
from wechat_bridge.service import Bridge, EVENT_NAME
from wechat_bridge.simulation import SimulatedAgent, SimulatedHook
from wechat_bridge.store import Store


class BridgeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.source = SimulatedHook()
        self.hook = HookClient(self.source.url)
        self.bridge = Bridge(self.temp.name, self.hook, self.source.account, ['wxid_sim_peer'], True, start='earliest')
        self.protocol = Protocol(self.bridge)
        self.hook.request('POST', '/consumers', {'consumer_id': self.bridge.consumer, 'start': 'earliest'})
        self.server = Server(('127.0.0.1', 0), self.bridge, secrets.token_urlsafe(32))
        self.server_thread = threading.Thread(target=self.server.serve_forever)
        self.server_thread.start()
        self.url = f'http://127.0.0.1:{self.server.server_port}'
        self.client = HookClient(self.url, self.server.token)
        self.agents = []

    def tearDown(self):
        for agent in self.agents:
            agent.close()
        self.server.shutdown(); self.server.server_close(); self.server_thread.join()
        self.source.close(); self.bridge.close(); self.temp.cleanup()

    def rpc(self, method, params=None):
        return self.client.request('POST', '/mcp', {'jsonrpc': '2.0', 'id': 1, 'method': method, 'params': params or {}})

    def add_event(self, text='你好 😀', peer='wxid_sim_peer'):
        event = self.source.inject(peer, text)
        self.bridge.collect_once(0)
        return event

    def agent(self):
        secret = 'whsec_' + base64.b64encode(secrets.token_bytes(32)).decode()
        agent = SimulatedAgent(self.url, self.server.token, secret)
        self.agents.append(agent)
        return agent

    def subscription(self, agent, **extra):
        params = {'name': EVENT_NAME, 'arguments': {'conversation_ids': ['wxid_sim_peer']},
                  'delivery': {'mode': 'webhook', 'url': agent.url, 'secret': agent.secret}, **extra}
        response = self.rpc('events/subscribe', params)
        self.assertNotIn('error', response)
        return params, response['result']

    def test_http_auth_origin_and_protocol_discovery(self):
        with self.assertRaises(RemoteError) as error:
            HookClient(self.url).request('POST', '/mcp', {})
        self.assertEqual(error.exception.status, 401)
        import http.client
        conn = http.client.HTTPConnection('127.0.0.1', self.server.server_port)
        conn.request('POST', '/mcp', b'{}', {'Authorization': 'Bearer ' + self.server.token, 'Content-Type': 'application/json', 'Origin': 'https://evil.example'})
        response = conn.getresponse(); self.assertEqual(response.status, 403); response.read(); conn.close()
        result = self.rpc('server/discover')['result']
        self.assertIn('events', result['capabilities'])
        self.assertEqual(result['supportedVersions'], ['2026-07-28'])
        self.assertEqual(self.rpc('initialize', {'protocolVersion': '2025-06-18'})['result']['protocolVersion'], '2025-06-18')
        tools = self.rpc('tools/list')['result']['tools']
        self.assertTrue({'get_messages', 'ack_messages', 'reply_text'} <= {t['name'] for t in tools})
        with self.assertRaises(RemoteError) as missing_metadata:
            self.client.request('GET', '/.well-known/oauth-protected-resource/mcp')
        self.assertEqual(missing_metadata.exception.status, 404)
        with self.assertRaises(RemoteError) as public_metadata:
            HookClient(self.url).request('GET', '/.well-known/oauth-protected-resource/mcp')
        self.assertEqual(public_metadata.exception.status, 404)

    def test_signed_event_http_auto_reply_and_dedupe(self):
        agent = self.agent()
        self.subscription(agent)
        event = self.add_event()
        self.assertTrue(self.bridge.deliver_once())
        deadline = time.monotonic() + 3
        while not agent.results and time.monotonic() < deadline:
            time.sleep(.01)
        self.assertEqual(len(agent.results), 1)
        self.assertFalse(agent.results[0]['result']['isError'])
        self.assertEqual(self.source.sent[0]['toid'], event['conversation_id'])
        self.assertEqual(self.source.sent[0]['msg'], '模拟自动回复：你好 😀')
        self.bridge.reply_text(event['event_id'], '模拟自动回复：你好 😀')
        self.assertEqual(len(self.source.sent), 1)
        self.assertEqual(self.bridge.store.status()['deliveries'], {'delivered': 1})

    def test_filter_and_outgoing_do_not_trigger(self):
        agent = self.agent(); self.subscription(agent)
        self.add_event(peer='wxid_other')
        outgoing = self.source.inject('wxid_sim_peer', 'outgoing')
        outgoing['direction'] = 'outgoing'
        self.bridge.collect_once(0)
        self.assertFalse(self.bridge.deliver_once())
        self.assertEqual(self.bridge.store.status()['events'], 1)
        self.assertEqual(self.bridge.store.event(outgoing['event_id'])['direction'], 'outgoing')

    def test_group_mention_gate_blocks_publication_and_direct_reply(self):
        event_schema = self.rpc('events/list')['result']['events'][0]['payloadSchema']
        self.assertIn('mentions', event_schema['properties'])
        self.assertTrue(self.protocol.invoke('bridge_status', {})['group_reply_policy']['require_explicit_self_mention'])
        group = '12345@chatroom'
        self.bridge.conversations = frozenset(['wxid_sim_peer', group])
        agent = self.agent()
        self.bridge.subscribe({'name': EVENT_NAME, 'arguments': {'conversation_ids': [group]},
            'delivery': {'mode': 'webhook', 'url': agent.url, 'secret': agent.secret}}, 'local-owner')
        source = 'weixin41930_record_source_180'
        rejected = [None, {'state': 'unknown', 'wxids': [self.source.account], 'source': source},
                    {'state': 'known', 'wxids': [], 'all': True, 'source': source},
                    {'state': 'known', 'wxids': ['wxid_other'], 'source': source},
                    {'state': 'known', 'wxids': [self.source.account + '_suffix'], 'source': source},
                    {'state': 'known', 'wxids': [self.source.account], 'source': 'message_text'},
                    {'state': 'known', 'wxids': self.source.account, 'source': source}]
        for mention in rejected:
            event = self.source.inject(group, '@我 @昵称 <atuserlist>wxid_sim_self</atuserlist>', mentions=mention)
            self.bridge.collect_once(0)
            self.assertFalse(self.bridge.deliver_once())
            # 即使使用已保留事件、已知 event_id 或构造的工具调用，也不能
            # 通过跳过事件过滤绕过门禁。
            self.bridge.store.ingest([event])
            with self.assertRaises(ValueError):
                self.bridge.reply_text(event['event_id'], 'must not send')
            self.assertTrue(self.rpc('tools/call', {'name': 'reply_text', 'arguments': {
                'event_id': event['event_id'], 'text': 'must not send'}})['result']['isError'])
            self.bridge.deliver_once()  # 保留发件箱中的 pending 操作同样必须拒绝。
        self.assertEqual(self.source.sent, [])
        mention = {'state': 'known', 'wxids': [self.source.account], 'all': False, 'source': source}
        event = self.source.inject(group, '真实结构化@', mentions=mention)
        self.bridge.collect_once(0)
        self.assertTrue(self.bridge.deliver_once())
        deadline = time.monotonic() + 3
        while not agent.results and time.monotonic() < deadline:
            time.sleep(.01)
        self.assertEqual(len(agent.results), 1)
        self.assertEqual(len(self.source.sent), 1)
        self.assertEqual(agent.events[0]['data']['mentions'], mention)
        self.bridge.reply_text(event['event_id'], '模拟自动回复：真实结构化@')
        self.assertEqual(len(self.source.sent), 1)

    def test_subscribe_refresh_cancel_and_expiration(self):
        agent = self.agent()
        params, first = self.subscription(agent)
        self.add_event()
        agent.secret = 'whsec_' + base64.b64encode(secrets.token_bytes(32)).decode()
        _, second = self.subscription(agent)
        self.assertEqual(first['id'], second['id'])
        self.assertTrue(self.bridge.deliver_once())
        self.rpc('events/unsubscribe', {**params, 'delivery': {'mode': 'webhook', 'url': agent.url}})
        self.add_event('after cancel')
        self.assertFalse(self.bridge.deliver_once())
        self.subscription(agent, ttlMs=1000)
        with self.bridge.store.lock:
            self.bridge.store.db.execute('UPDATE subscriptions SET expires=0')
        self.add_event('expired')
        self.assertFalse(self.bridge.deliver_once())

    def test_retry_preserves_event_and_stops_terminal(self):
        agent = self.agent(); self.subscription(agent)
        event = self.add_event(); agent.failures = 1
        self.bridge.deliver_once()
        self.assertEqual(self.bridge.store.status()['deliveries'], {'pending': 1})
        with self.bridge.store.lock:
            self.bridge.store.db.execute('UPDATE deliveries SET due=0')
        self.bridge.deliver_once()
        self.assertEqual(agent.events[0]['eventId'], event['event_id'])
        self.add_event('terminal')
        with patch.object(self.bridge.callbacks, 'signed', side_effect=RemoteError(410)):
            self.bridge.deliver_once()
        self.assertEqual(self.bridge.store.status()['deliveries']['failed'], 1)

    def test_commit_before_ack_and_restart_consumption(self):
        event = self.source.inject('wxid_sim_peer', 'durable')
        original = self.hook.request
        def fail_ack(method, path, *args, **kwargs):
            if path.endswith('/ack'):
                raise OSError('disconnected')
            return original(method, path, *args, **kwargs)
        with patch.object(self.hook, 'request', side_effect=fail_ack):
            with self.assertRaises(OSError):
                self.bridge.collect_once(0)
        self.assertEqual(self.bridge.store.event(event['event_id']), event)
        self.bridge.collect_once(0)
        self.assertEqual(self.bridge.store.status()['events'], 1)
        batch = self.protocol.invoke('get_messages', {'consumer_id': 'agent-a'})
        second = self.protocol.invoke('get_messages', {'consumer_id': 'agent-b'})
        self.assertEqual(batch['events'], second['events'])
        self.bridge.store.close()
        self.bridge.store = Store(self.temp.name, self.source.account, True)
        self.assertEqual(batch, self.protocol.invoke('get_messages', {'consumer_id': 'agent-a'}))
        for _ in range(2):
            self.protocol.invoke('ack_messages', {'consumer_id': 'agent-a', 'ack_token': batch['ack_token']})
        self.assertEqual(self.protocol.invoke('get_messages', {'consumer_id': 'agent-a'})['events'], [])
        self.assertEqual(self.protocol.invoke('get_messages', {'consumer_id': 'agent-b'})['events'], batch['events'])

    def test_long_poll_wakes_after_ingest(self):
        with ThreadPoolExecutor() as pool:
            future = pool.submit(self.protocol.invoke, 'get_messages', {'consumer_id': 'waiter', 'timeout': 3})
            self.add_event('wake')
            self.assertEqual(future.result(2)['events'][0]['payload']['text'], 'wake')

    def test_reply_concurrent_conflict_and_unknown(self):
        event = self.add_event()
        with ThreadPoolExecutor(max_workers=4) as pool:
            replies = list(pool.map(lambda _: self.bridge.reply_text(event['event_id'], '回复'), range(4)))
        self.assertEqual(len({r['request_id'] for r in replies}), 1)
        self.assertEqual(len(self.source.sent), 1)
        with self.assertRaises(ValueError):
            self.bridge.reply_text(event['event_id'], '不同回复')
        another = self.add_event('ambiguous')
        self.bridge.store.prepare_reply(another['event_id'], self.source.account, 'pending')
        self.bridge.store.reply_state(another['event_id'], 'submitting')
        result = self.bridge.reply_text(another['event_id'], 'pending')
        self.assertEqual(result['state'], 'unknown')
        self.assertEqual(len(self.source.sent), 1)

    def test_post_response_lost_queries_original_operation(self):
        event = self.add_event()
        original = self.hook.request
        def lost(method, path, *args, **kwargs):
            result = original(method, path, *args, **kwargs)
            if path == '/SendTextMsg':
                raise OSError('response lost')
            return result
        with patch.object(self.hook, 'request', side_effect=lost):
            reply = self.bridge.reply_text(event['event_id'], 'one send')
        self.assertEqual(reply['state'], 'accepted')
        self.assertEqual(len(self.source.sent), 1)

    def test_wrong_account_no_ack_or_send(self):
        event = self.source.inject('wxid_sim_peer', 'wrong account')
        event['account'] = 'wxid_changed'
        with self.assertRaises(ValueError):
            self.bridge.collect_once(0)
        self.assertEqual(self.source.consumers['mcp-bridge']['pos'], 0)
        event['account'] = self.source.account
        self.bridge.collect_once(0)
        self.source.account = 'wxid_changed'
        with self.assertRaises(ValueError):
            self.bridge.reply_text(event['event_id'], 'blocked')
        self.assertEqual(len(self.source.sent), 0)
        self.assertEqual(self.bridge.refresh_reply(event['event_id'])['state'], 'prepared')
        self.source.account = event['account']
        self.assertEqual(self.bridge.reply_text(event['event_id'], 'blocked')['state'], 'accepted')

    def test_ssrf_signatures_and_encrypted_secret(self):
        with self.assertRaises(ValueError):
            CallbackHTTP().destination('http://127.0.0.1/events')
        for url in ('https://127.0.0.1/', 'https://169.254.169.254/', 'https://user:pass@example.com/'):
            with self.assertRaises(ValueError):
                CallbackHTTP().destination(url)
        agent = self.agent(); self.subscription(agent)
        value = self.bridge.store.db.execute('SELECT secret FROM subscriptions').fetchone()[0]
        self.assertNotIn(agent.secret, value)
        raw = encode({'example': '中文'})
        stamp = int(time.time())
        headers = {'webhook-id': 'event-1', 'webhook-timestamp': str(stamp),
                   'webhook-signature': signature(agent.secret, 'event-1', stamp, raw)}
        verify_signature(agent.secret, headers, raw)
        with self.assertRaises(ValueError):
            verify_signature(agent.secret, headers, raw + b'x')
        with self.assertRaises(ValueError):
            verify_signature(agent.secret, headers, raw, now=stamp+301)

    def test_state_lock_identity_and_strict_tool_arguments(self):
        with self.assertRaises(RuntimeError):
            Store(self.temp.name, self.source.account, True)
        for args in ({'event_id': 'missing', 'text': 'hi', 'toid': 'wxid_other'}, {'event_id': 'missing', 'text': ''}):
            result = self.rpc('tools/call', {'name': 'reply_text', 'arguments': args})
            self.assertTrue(result['result']['isError'])
        self.assertIn('error', self.rpc('events/subscribe', {'name': EVENT_NAME,
            'arguments': {'conversation_ids': ['wxid_other']}, 'delivery': {}}))
        self.bridge.store.close()
        try:
            with self.assertRaises(ValueError):
                Store(self.temp.name, 'other_account', True)
        finally:
            self.bridge.store = Store(self.temp.name, self.source.account, True)

    def test_store_capacity_rolls_back_and_does_not_ack(self):
        self.source.inject('wxid_sim_peer', 'one')
        self.source.inject('wxid_sim_peer', 'two')
        with patch('wechat_bridge.store.MAX_EVENTS', 1):
            with self.assertRaises(ValueError):
                self.bridge.collect_once(0)
        self.assertEqual(self.bridge.store.status()['events'], 0)
        self.assertEqual(self.source.consumers['mcp-bridge']['pos'], 0)
        self.bridge.collect_once(0)
        self.assertEqual(self.bridge.store.status()['events'], 2)

    def test_callback_challenge_failure_and_retry_budget(self):
        agent = self.agent()
        with patch.object(self.bridge.callbacks, 'signed', return_value={'challenge': 'wrong'}):
            response = self.rpc('events/subscribe', {'name': EVENT_NAME,
                'arguments': {'conversation_ids': ['wxid_sim_peer']},
                'delivery': {'mode': 'webhook', 'url': agent.url, 'secret': agent.secret}})
        self.assertEqual(response['error']['code'], -32015)
        self.assertEqual(self.bridge.store.status()['subscriptions_active'], 0)
        self.subscription(agent); self.add_event()
        with patch.object(self.bridge.callbacks, 'signed', side_effect=RemoteError(503)):
            for _ in range(8):
                with self.bridge.store.lock:
                    self.bridge.store.db.execute('UPDATE deliveries SET due=0')
                self.bridge.deliver_once()
        self.assertEqual(self.bridge.store.status()['deliveries'], {'failed': 1})
        self.assertFalse(self.bridge.deliver_once())

    def test_delivery_restart_and_canonical_event_deduplication(self):
        agent = self.agent(); self.subscription(agent)
        event = self.add_event()
        self.bridge.store.ingest([dict(reversed(list(event.items())))])
        claimed = self.bridge.store.next_delivery()
        self.assertEqual(claimed['event_id'], event['event_id'])
        self.bridge.store.close()
        self.bridge.store = Store(self.temp.name, self.source.account, True)
        self.bridge.deliver_once()
        self.assertEqual(agent.events[0]['eventId'], event['event_id'])

    def test_simulation_keeps_loopback_callbacks_and_rejects_legacy_remote_identity(self):
        directory = Path(self.temp.name) / 'simulation-identity'
        simulation = Bridge(directory, self.hook, self.source.account, ['wxid_sim_peer'], True)
        try:
            self.assertTrue(simulation.callbacks.simulation)
            with self.assertRaises(ValueError):
                simulation.callbacks.destination('https://example.com/events')
            # 已停用的远程模拟数据库不能转为本地或真实运行数据库。
            simulation.store.db.execute("UPDATE meta SET value=? WHERE key='identity'",
                                        (json.dumps([self.source.account, True, True]),))
        finally:
            simulation.close()
        with self.assertRaises(ValueError):
            Store(directory, self.source.account, True)


class StdioTests(unittest.TestCase):
    def test_stdio_initialization_and_eof_shutdown(self):
        with tempfile.TemporaryDirectory() as temp:
            cli = Path(__file__).resolve().parents[1] / 'bridge.py'
            request = {'jsonrpc': '2.0', 'id': 7, 'method': 'initialize', 'params': {'protocolVersion': '2025-06-18'}}
            result = subprocess.run([sys.executable, '-B', '-X', 'utf8', str(cli), 'serve', '--simulate',
                                     '--transport', 'stdio', '--state-dir', temp], input=encode(request)+b'\n',
                                    capture_output=True, timeout=12)
            self.assertEqual(result.returncode, 0, result.stderr.decode())
            response = json.loads(result.stdout)
            self.assertEqual(response['id'], 7)
            self.assertEqual(response['result']['serverInfo']['name'], 'wechat-mcp-bridge')


if __name__ == '__main__':
    unittest.main()
