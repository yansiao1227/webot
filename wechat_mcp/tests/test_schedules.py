from concurrent.futures import ThreadPoolExecutor
import tempfile
import time
import unittest
from wechat_bridge.service import Bridge
from wechat_bridge.protocol import Protocol


class Hook:
    def __init__(self):
        self.account = 'self'
        self.sent = []
        self.operations = {}
        self.lose_response = False

    def request(self, method, path, body=None):
        if path == '/GetSelfProfile':
            return {'wxid': self.account}
        if path == '/SendTextMsg':
            self.sent.append(body)
            result = {'state': 'accepted', 'account': self.account}
            self.operations[body['request_id']] = result
            if self.lose_response:
                raise TimeoutError('response lost')
            return result
        if path.startswith('/client_hook/requests/'):
            return self.operations[path.rsplit('/', 1)[1]]
        raise AssertionError(path)


class ScheduleTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.hook = Hook()
        self.bridge = Bridge(self.temp.name, self.hook, 'self', ['peer', 'group@chatroom'], True)
        self.protocol = Protocol(self.bridge)
        self.event = {'event_id': 'source', 'account': 'self', 'conversation_id': 'peer',
                      'sender_id': 'peer', 'direction': 'incoming', 'message_type': 'text',
                      'payload': {'text': '每天提醒'}, 'captured_at': time.time()}
        self.bridge.store.ingest([self.event])

    def tearDown(self):
        self.bridge.close(); self.temp.cleanup()

    def call(self, name, **args):
        return self.protocol.invoke(name, args)

    def authorize(self):
        return self.call('authorize_schedule', event_id='source', schedule_id='task')

    def send(self, run='run', index=0, text='提醒', final=True):
        return self.call('send_scheduled_text', schedule_id='task', run_id=run, part_index=index, text=text, final=final)

    def test_long_term_grant_survives_retention_and_restart(self):
        self.authorize()
        self.bridge.store.prune(time.time() + 8 * 86400)
        self.assertEqual(self.bridge.store.db.execute('SELECT COUNT(*) FROM inbox').fetchone()[0], 0)
        self.bridge.close()
        self.bridge = Bridge(self.temp.name, self.hook, 'self', ['peer', 'group@chatroom'], True)
        self.protocol = Protocol(self.bridge)
        self.assertTrue(self.call('get_schedule_authorization', schedule_id='task')['active'])
        self.assertEqual(self.send()['state'], 'accepted')
        self.assertFalse(self.send()['delivery_confirmed'])
        self.assertEqual(len(self.hook.sent), 1)

    def test_concurrent_send_is_idempotent_and_body_cannot_change(self):
        self.authorize()
        with ThreadPoolExecutor(max_workers=4) as pool:
            results = list(pool.map(lambda _: self.send(), range(4)))
        self.assertEqual(len({r['request_id'] for r in results}), 1)
        self.assertEqual(len(self.hook.sent), 1)
        with self.assertRaises(ValueError):
            self.send(text='changed')
        self.send(run='next')
        self.assertEqual(len(self.hook.sent), 2)

    def test_lost_response_only_queries_original_request(self):
        self.authorize(); self.hook.lose_response = True
        self.assertEqual(self.send()['state'], 'accepted')
        self.assertEqual(self.send()['state'], 'accepted')
        self.assertEqual(len(self.hook.sent), 1)

    def test_unknown_never_sends_again_or_advances_part(self):
        self.authorize()
        self.send(final=False)
        self.bridge.store.db.execute("UPDATE scheduled_parts SET state='unknown'")
        self.assertEqual(self.send(final=False)['state'], 'unknown')
        with self.assertRaises(ValueError):
            self.send(index=1)
        self.assertEqual(len(self.hook.sent), 1)

    def test_order_final_revocation_and_account_switch(self):
        self.authorize()
        with self.assertRaises(ValueError):
            self.send(index=1)
        self.send(final=False)
        self.send(index=1)
        with self.assertRaises(ValueError):
            self.send(index=2)
        self.call('revoke_schedule', schedule_id='task')
        with self.assertRaises(ValueError):
            self.send(run='later')
        with self.assertRaises(ValueError):
            self.authorize()
        self.assertEqual(self.call('get_scheduled_text_status', schedule_id='task', run_id='run', part_index=1)['state'], 'accepted')
        self.call('authorize_schedule', event_id='source', schedule_id='new')
        self.hook.account = 'different'
        with self.assertRaises(ValueError):
            self.call('send_scheduled_text', schedule_id='new', run_id='run', part_index=0, text='x', final=True)
        self.assertEqual(len(self.hook.sent), 2)

    def test_group_requires_native_mention_and_keeps_original_target(self):
        group = {**self.event, 'event_id': 'group', 'conversation_id': 'group@chatroom', 'sender_id': 'member'}
        self.bridge.store.ingest([group])
        with self.assertRaises(ValueError):
            self.call('authorize_schedule', event_id='group', schedule_id='denied')
        mentioned = {**group, 'event_id': 'mentioned', 'mentions': {'state': 'known',
            'source': 'weixin41930_record_source_180', 'wxids': ['self']}}
        self.bridge.store.ingest([mentioned])
        self.call('authorize_schedule', event_id='mentioned', schedule_id='task')
        self.send()
        self.assertEqual(self.hook.sent[0]['toid'], 'group@chatroom')
        self.bridge.conversations = frozenset(['peer'])
        with self.assertRaises(ValueError):
            self.send(run='next')

    def test_invalid_identifiers_and_boolean_part_are_rejected(self):
        self.authorize()
        for index in (True, -1, 64, '0'):
            with self.assertRaises(ValueError):
                self.send(index=index)
        with self.assertRaises(ValueError):
            self.send(run='../other')
