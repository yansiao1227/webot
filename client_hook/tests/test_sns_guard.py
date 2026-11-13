"""Hook 执行边界回归；策略不执行，幂等与恢复边界保留。仅自有后端。"""
import json
from pathlib import Path
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from api.sns import Sns
from api.store import Store
from api.backend import NativeBackend
from api.errors import ApiError
from test_sns_api import SnsFixture

class LegacyPolicyFixture(SnsFixture):
    def sns_write_policy(self):
        raise AssertionError('Hook 不应调用 MCP 策略')

class SnsBoundaryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.temp.name) / 'messages.sqlite3')
        self.backend = LegacyPolicyFixture()
        self.backend.start(self.store)
        self.stop = threading.Event()
        self.sns = Sns(self.store, self.backend, self.stop)
        self.worker = None
    def tearDown(self):
        self.stop.set()
        if self.worker: self.worker.join(3)
        self.store.close()
        self.temp.cleanup()
    def request(self, name):
        return self.sns.request('like', {'account': self.backend.account, 'request_id': name,
                                        'post_id': '1', 'author_id': 'wxid_peer'})
    def run_worker(self):
        self.worker = threading.Thread(target=self.sns.run)
        self.worker.start()
    def wait(self, name):
        until = time.monotonic() + 3
        while time.monotonic() < until:
            result = self.sns.status(name, self.backend.account)
            if result['state'] not in ('queued', 'running'): return result
            time.sleep(.01)
        self.fail('自有后端未完成')
    def test_distinct_requests_have_no_hook_rate_or_pause_policy(self):
        for index in range(7): self.assertEqual(self.request('req_' + str(index))[0], 202)
        self.run_worker()
        for index in range(7): self.assertEqual(self.wait('req_' + str(index))['state'], 'accepted')
        self.assertEqual(len(self.backend.sns_calls), 7)
    def test_unknown_not_replayed_and_does_not_suspend_other_intents(self):
        self.backend.failure = RuntimeError('自有后端故障')
        self.run_worker()
        self.request('unknown')
        self.assertEqual(self.wait('unknown')['state'], 'unknown')
        self.backend.failure = None
        self.request('unknown')
        self.request('next')
        self.assertEqual(self.wait('next')['state'], 'accepted')
        self.assertEqual(len(self.backend.sns_calls), 2)
    def test_restart_preserves_unknown_and_legacy_policy_rows(self):
        self.request('queued')
        self.store.db.execute('CREATE TABLE sns_write_guard(account TEXT,suspended INTEGER)')
        self.store.db.execute('INSERT INTO sns_write_guard VALUES(?,1)', (self.backend.account,))
        self.sns = Sns(self.store, self.backend, self.stop)
        self.assertEqual(self.sns.status('queued', self.backend.account)['state'], 'unknown')
        self.request('queued')
        self.request('new')
        self.run_worker()
        self.assertEqual(self.wait('new')['state'], 'accepted')
        self.assertEqual(len(self.backend.sns_calls), 1)
        self.assertEqual(self.store.db.execute('SELECT suspended FROM sns_write_guard').fetchone()[0], 1)
    def test_capabilities_describe_build_not_policy(self):
        pins = {'rpc_version': 3, 'abi_fixture_passed': True,
                'supported_actions': ['like', 'unlike', 'comment', 'publish']}
        with patch('pathlib.Path.read_text', return_value=json.dumps(pins)):
            capabilities = NativeBackend().sns_capabilities()
            self.assertTrue(all(capabilities.values()))
        for invalid in ({**pins, 'abi_fixture_passed': False},
                        {**pins, 'rpc_version': 2},
                        {**pins, 'supported_actions': ['like', 'unlike', 'comment']}):
            with self.subTest(pins=invalid), patch('pathlib.Path.read_text', return_value=json.dumps(invalid)):
                self.assertFalse(NativeBackend().sns_capabilities()['publish'])
        self.assertFalse(hasattr(NativeBackend(), 'sns_write_policy'))

    def test_reopening_publish_preserves_unknown_and_does_not_requeue_it(self):
        body = {'account': self.backend.account, 'request_id': 'old_publish',
                'text': '原有测试意图', 'visibility': 'public'}
        self.sns.request('publish', body)
        self.sns = Sns(self.store, self.backend, self.stop)
        self.backend.available = False
        existing = self.sns.status('old_publish', self.backend.account)
        self.assertEqual(existing['state'], 'unknown')
        self.assertEqual(self.sns.request('publish', body)[1], existing)
        with self.assertRaises(ApiError) as caught:
            self.sns.request('publish', {**body, 'request_id': 'new_publish', 'text': '新测试意图'})
        self.assertEqual(caught.exception.code, 'sns_native_unavailable')
        self.backend.available = True
        self.sns.request('publish', {**body, 'request_id': 'new_publish', 'text': '新测试意图'})
        self.run_worker()
        self.assertEqual(self.wait('new_publish')['state'], 'accepted')
        self.assertEqual(self.sns.status('old_publish', self.backend.account), existing)
        self.assertEqual(len(self.backend.sns_calls), 1)
        self.assertEqual(self.store.db.execute('SELECT COUNT(*) FROM sns_operations').fetchone()[0], 2)
