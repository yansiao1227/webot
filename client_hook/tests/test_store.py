import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import tempfile
import threading
import time
import unittest

from api.errors import ApiError
from api.store import Store


def event(text='中文 😀'):
    return {'message_type': 'text', 'payload': {'text': text}, 'conversation_id': 'wxid_peer',
            'fromid': 'wxid_peer', 'toid': 'wxid_self', 'direction': 'incoming', 'source': 'live'}


class StoreTests(unittest.TestCase):
    def test_outgoing_sort_sequence_keeps_body_independent_identity(self):
        value={**event('same text'),'direction':'outgoing','native_sort_seq':'1792841763000',
               'native_created_at':1792841763,'native_record_metadata_source':'weixin41930_record_identity_v2'}
        first=self.store.append('wxid_self',value)
        self.assertEqual(first,self.store.append('wxid_self',{**value,'seq':22}))
        self.assertNotEqual(first,self.store.append('wxid_self',{**value,'native_sort_seq':'1792841763001'}))
        self.assertNotEqual(first,self.store.append('wxid_self',{**value,'message_id':'123'}))
        self.assertNotEqual(first,self.store.append('wxid_other',value))

    def test_outgoing_repeated_native_fact_deduplicates_without_body_guessing(self):
        value = {**event('same text'), 'direction':'outgoing', 'native_client_id':'native-A',
                 'native_client_id_source':'weixin41930_record_client278_v1'}
        first = self.store.append('wxid_self',value)
        self.assertEqual(self.store.append('wxid_self',{**value,'seq':999}),first)
        final = self.store.append('wxid_self',{**value,'message_id':'456'})
        self.assertNotEqual(first,final)
        self.assertNotEqual(final,self.store.append('wxid_self',{**value,'native_client_id':'native-B'}))

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / 'messages.db'
        self.store = Store(self.path)
        self.store.capture('running', account='wxid_self')

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def consumer(self, name='agent'):
        return self.store.consumer(name, 'wxid_self', 'earliest')

    def read(self, name='agent', **kwargs):
        return self.store.read(name, 'wxid_self', timeout=0, **kwargs)

    def test_ack_isolation_redelivery_restart_and_wrong_token(self):
        self.consumer()
        self.consumer('second')
        self.store.append('wxid_self', event())
        batch = self.read()
        self.assertEqual(batch['events'][0]['payload']['text'], '中文 😀')
        self.assertEqual(self.read()['ack_token'], batch['ack_token'])
        self.store.close()
        self.store = Store(self.path)
        self.assertEqual(self.read()['ack_token'], batch['ack_token'])
        with self.assertRaises(ApiError) as caught:
            self.store.ack('second', 'wxid_self', batch['ack_token'])
        self.assertEqual(caught.exception.status, 409)
        self.store.ack('agent', 'wxid_self', batch['ack_token'])
        self.store.ack('agent', 'wxid_self', batch['ack_token'])
        self.assertEqual(self.read()['events'], [])
        self.assertEqual(len(self.read('second')['events']), 1)

    def test_long_poll_wakes_on_commit_and_concurrent_read_rejected(self):
        self.consumer()
        result = []
        thread = threading.Thread(target=lambda: result.append(self.store.read('agent', 'wxid_self', timeout=2)))
        thread.start()
        until = time.monotonic() + 1
        while 'agent' not in self.store.readers:
            self.assertLess(time.monotonic(), until)
            time.sleep(.005)
        with self.assertRaises(ApiError) as caught:
            self.read()
        self.assertEqual(caught.exception.code, 'concurrent_read')
        start = time.monotonic()
        self.store.append('wxid_self', event())
        thread.join(1)
        self.assertFalse(thread.is_alive())
        self.assertLess(time.monotonic() - start, 1)
        self.assertEqual(len(result[0]['events']), 1)

    def test_expiry_requires_explicit_reset_and_revokes_batch(self):
        self.consumer()
        self.store.append('wxid_self', event())
        old = self.read()
        self.store.clock = lambda: time.time() + 8 * 86400
        self.store.prune()
        with self.assertRaises(ApiError) as caught:
            self.read()
        self.assertEqual(caught.exception.status, 410)
        self.store.reset('agent', 'wxid_self', 'earliest')
        with self.assertRaises(ApiError):
            self.store.ack('agent', 'wxid_self', old['ack_token'])
        self.assertEqual(self.read()['events'], [])

    def test_capacity_pruning_and_new_consumer_starts_at_available_messages(self):
        self.consumer()
        self.store.max_bytes = 1
        self.store.append('wxid_self', event())
        with self.assertRaises(ApiError) as caught:
            self.read()
        self.assertEqual(caught.exception.status, 410)
        self.consumer('new')
        self.assertEqual(self.read('new')['events'], [])

    def test_cursor_epoch_and_account_isolation(self):
        initial = self.consumer()
        self.store.append('wxid_other', event())
        self.assertEqual(self.read()['events'], [])
        self.store.reset('agent', 'wxid_self', 'latest')
        with self.assertRaises(ApiError):
            self.read(cursor=initial['cursor'])
        with self.assertRaises(ApiError):
            self.store.consumer('agent', 'wxid_other')

    def test_operation_idempotency_capacity_and_crash_boundary(self):
        params = {'toid': 'filehelper', 'msg': 'x'}
        self.assertTrue(self.store.enqueue('one', 'wxid_self', params, capacity=1))
        self.assertFalse(self.store.enqueue('one', 'wxid_self', params, capacity=1))
        with self.assertRaises(ApiError):
            self.store.enqueue('one', 'wxid_self', {'toid': 'filehelper', 'msg': 'y'})
        with self.assertRaises(ApiError) as caught:
            self.store.enqueue('two', 'wxid_self', params, capacity=1)
        self.assertEqual(caught.exception.status, 429)
        self.assertEqual(self.store.next_operation()[0], 'one')
        self.store.close()
        self.store = Store(self.path)
        self.assertEqual(self.store.operation('one')['state'], 'unknown')
        self.assertIsNone(self.store.next_operation())

    def test_storage_failure_rolls_back_event_and_does_not_advance_consumer(self):
        self.consumer()
        self.store.db.execute("CREATE TRIGGER reject_event BEFORE INSERT ON events BEGIN SELECT RAISE(ABORT,'disk failure'); END")
        import sqlite3
        with self.assertRaises(sqlite3.DatabaseError):
            self.store.append('wxid_self', event())
        self.assertEqual(self.store.status()['events'], 0)
        self.assertEqual(self.read()['events'], [])

    def test_reset_wakes_and_invalidates_inflight_read(self):
        self.consumer()
        failures = []
        def read():
            try:
                self.store.read('agent', 'wxid_self', timeout=3)
            except ApiError as error:
                failures.append(error.code)
        thread = threading.Thread(target=read)
        thread.start()
        until = time.monotonic() + 1
        while 'agent' not in self.store.readers:
            self.assertLess(time.monotonic(), until)
            time.sleep(.005)
        self.store.reset('agent', 'wxid_self', 'latest')
        thread.join(1)
        self.assertFalse(thread.is_alive())
        self.assertEqual(failures, ['consumer_reset'])


if __name__ == '__main__':
    unittest.main()
