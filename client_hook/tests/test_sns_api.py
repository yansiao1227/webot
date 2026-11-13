"""朋友圈契约、HTTP 和持久意图回归；仅显式自有测试后端。"""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import tempfile
import threading
import time
import unittest

from api.backend import FixtureBackend
from api.errors import ApiError
from api.server import Application, Server
from api.sns import ACTIONS, Sns
from api.store import Store
from client import Client, ClientError


class SnsFixture(FixtureBackend):
    def start(self, store):
        super().start(store)
        self.sns_calls = []
        self.available = True
        self.failure = None

    def sns_capabilities(self):
        return {action: self.available for action in ACTIONS}

    def sns_read(self, account, params):
        return {'account': account, 'items': [], 'simulation': True, 'params': params}

    def sns_execute(self, account, action, params, request_id):
        self.sns_calls.append((account, action, params, request_id))
        if self.failure:
            raise self.failure
        return {'state': 'accepted', 'local_accepted': True, 'server_confirmed': False,
                'simulation': True}


class SnsApiTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.backend = SnsFixture()
        self.app = Application(self.temp.name, self.backend)
        self.server = Server(('127.0.0.1', 0), self.app, token='sns-test')
        self.thread = threading.Thread(target=self.server.serve_forever)
        self.thread.start()
        self.url = f'http://127.0.0.1:{self.server.server_port}'
        self.client = Client(self.url, 'sns-test')
        self.account = self.backend.account

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.app.close()
        self.temp.cleanup()

    def completed(self, request_id):
        deadline = time.monotonic() + 3
        while True:
            operation = self.client.sns_operation(self.account, request_id)
            if operation['state'] not in ('queued', 'running'):
                return operation
            self.assertLess(time.monotonic(), deadline)
            time.sleep(.01)

    def test_sdk_routes_auth_and_all_actions(self):
        with self.assertRaises(ClientError) as caught:
            Client(self.url).sns_timeline(self.account)
        self.assertEqual(caught.exception.status, 401)
        self.assertTrue(self.client.sns_timeline(self.account)['simulation'])
        self.client.sns_like(self.account, '18446744073709551615', 'wxid_peer', request_id='like1')
        self.client.sns_unlike(self.account, '18446744073709551615', 'wxid_peer', request_id='unlike1')
        self.client.sns_comment(self.account, '123', 'wxid_peer', '测试正文', request_id='comment1')
        self.client.sns_publish(self.account, '测试发布', request_id='publish1')
        for request_id in ('like1', 'unlike1', 'comment1', 'publish1'):
            operation = self.completed(request_id)
            self.assertEqual(operation['state'], 'accepted')
            self.assertTrue(operation['local_accepted'])
            self.assertFalse(operation['server_confirmed'])
        paths = self.client.request('GET', '/openapi.json')['paths']
        for action in ACTIONS:
            self.assertIn('/client_hook/sns/' + action, paths)

    def test_duplicate_does_not_execute_again_and_conflicts_are_bound(self):
        self.client.sns_like(self.account, '123', 'wxid_peer', request_id='same')
        self.completed('same')
        self.backend.available = False
        for _ in range(3):
            self.assertEqual(self.client.sns_like(self.account, '123', 'wxid_peer',
                                                request_id='same')['request_id'], 'same')
        self.assertEqual(len(self.backend.sns_calls), 1)
        cases = [lambda: self.client.sns_unlike(self.account, '123', 'wxid_peer', request_id='same'),
                 lambda: self.client.sns_like(self.account, '124', 'wxid_peer', request_id='same'),
                 lambda: self.client.sns_like('wxid_other', '123', 'wxid_peer', request_id='same')]
        for call in cases:
            with self.assertRaises(ClientError) as caught:
                call()
            self.assertEqual(caught.exception.status, 409)
        with self.assertRaises(ClientError) as caught:
            self.client.sns_operation('wxid_other', 'same')
        self.assertEqual(caught.exception.status, 404)

    def test_unavailable_rejects_without_persisting(self):
        self.backend.available = False
        with self.assertRaises(ClientError) as caught:
            self.client.sns_like(self.account, '123', 'wxid_peer', request_id='not_created')
        self.assertEqual(caught.exception.status, 503)
        self.assertEqual(caught.exception.body['error']['code'], 'sns_native_unavailable')
        self.assertFalse(caught.exception.body['error']['retryable'])
        with self.assertRaises(ClientError) as caught:
            self.client.sns_operation(self.account, 'not_created')
        self.assertEqual(caught.exception.status, 404)

    def test_invalid_numbers_fields_and_account_are_rejected(self):
        for post_id in (123, '001', '0', '-1', '18446744073709551616'):
            with self.assertRaises(ClientError) as caught:
                self.client.sns_like(self.account, post_id, 'wxid_peer', request_id='invalid')
            self.assertEqual(caught.exception.status, 400)
        with self.assertRaises(ClientError) as caught:
            self.client.sns_like('wxid_other', '123', 'wxid_peer', request_id='mismatch')
        self.assertEqual(caught.exception.status, 409)
        for params in ({'account': self.account, 'limit': True},
                       {'account': self.account, 'arbitrary_path': 'x'}):
            with self.assertRaises(ClientError) as caught:
                self.client.request('POST', '/client_hook/sns/timeline', params)
            self.assertEqual(caught.exception.status, 400)

    def test_provider_exception_is_unknown_and_not_reexecuted(self):
        self.backend.failure = ApiError(503, 'account_changed', '已进入提供者后的异常')
        self.client.sns_like(self.account, '123', 'wxid_peer', request_id='uncertain')
        self.assertEqual(self.completed('uncertain')['state'], 'unknown')
        self.client.sns_like(self.account, '123', 'wxid_peer', request_id='uncertain')
        self.assertEqual(len(self.backend.sns_calls), 1)

    def test_confirmed_requires_explicit_server_confirmation(self):
        self.backend.sns_execute = lambda *args: {'state': 'confirmed', 'server_confirmed': False}
        self.client.sns_like(self.account, '123', 'wxid_peer', request_id='not_confirmed')
        result = self.completed('not_confirmed')
        self.assertEqual(result['state'], 'unknown')
        self.assertFalse(result['server_confirmed'])

    def test_concurrent_duplicate_and_bounded_execution(self):
        entered, release = threading.Event(), threading.Event()
        original = self.backend.sns_execute

        def blocked(*args):
            entered.set()
            if not release.wait(3):
                raise RuntimeError('测试执行等待超时')
            return original(*args)

        self.backend.sns_execute = blocked
        self.app.sns.capacity = 1
        self.client.sns_like(self.account, '123', 'wxid_peer', request_id='blocked')
        self.assertTrue(entered.wait(1))
        try:
            results = []
            calls = [threading.Thread(target=lambda: results.append(self.client.sns_like(
                self.account, '123', 'wxid_peer', request_id='blocked'))) for _ in range(4)]
            for call in calls:
                call.start()
            for call in calls:
                call.join(2)
                self.assertFalse(call.is_alive())
            self.assertEqual(len(results), 4)
            with self.assertRaises(ClientError) as caught:
                self.client.sns_like(self.account, '124', 'wxid_peer', request_id='overflow')
            self.assertEqual(caught.exception.status, 429)
        finally:
            release.set()
        self.assertEqual(self.completed('blocked')['state'], 'accepted')
        self.assertEqual(len(self.backend.sns_calls), 1)


class SnsPersistenceTests(unittest.TestCase):
    def test_running_becomes_unknown_and_is_not_queued_on_restart(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / 'sns.sqlite3'
            store = Store(path)
            Sns(store, FixtureBackend(), threading.Event())
            store.db.execute("INSERT INTO sns_operations VALUES(?,?,?,?,?,?,?)",
                ('crashed', 'wxid_fixture_self', 'like', '{}', 'running', None, store.clock()))
            store.close()
            store = Store(path)
            try:
                sns = Sns(store, FixtureBackend(), threading.Event())
                self.assertEqual(sns.status('crashed', 'wxid_fixture_self')['state'], 'unknown')
                self.assertEqual(store.db.execute(
                    "SELECT count(*) FROM sns_operations WHERE state='queued'").fetchone()[0], 0)
            finally:
                store.close()


if __name__ == '__main__':
    unittest.main()
