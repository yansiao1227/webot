import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import json
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.request import Request, urlopen
from urllib.error import HTTPError

from api.backend import FixtureBackend
from api.server import Application, Server
from client import Client, ClientError
from test_store import event


class HttpTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.backend = FixtureBackend()
        self.app = Application(self.temp.name, self.backend)
        self.server = Server(('127.0.0.1', 0), self.app, token='test-token')
        self.thread = threading.Thread(target=self.server.serve_forever)
        self.thread.start()
        self.url = f'http://127.0.0.1:{self.server.server_port}'
        self.client = Client(self.url, 'test-token')

    def tearDown(self):
        self.server.shutdown()
        self.app.store.capture('stopped', account=self.backend.account)
        self.server.server_close()
        self.app.close()
        self.temp.cleanup()

    def test_auth_validation_alias_idempotency_and_unimplemented(self):
        with self.assertRaises(ClientError) as caught:
            Client(self.url).status()
        self.assertEqual(caught.exception.status, 401)
        one = self.client.send_text('filehelper', '中文 😀\nhello', 'req1')
        two = self.client.send_text('filehelper', '中文 😀\nhello', 'req1')
        self.assertEqual(one['state'], 'accepted')
        self.assertEqual(two['request_id'], one['request_id'])
        self.assertEqual(len(self.backend.sent), 1)
        with self.assertRaises(ClientError) as caught:
            self.client.send_text('filehelper', 'different', 'req1')
        self.assertEqual(caught.exception.status, 409)
        alias = self.client.request('POST', '/SendTextMsg', {'wxidorgid': 'filehelper', 'content': 'alias'})
        self.assertEqual(alias['state'], 'accepted')
        for body in ({'toid': 'filehelper', 'wxidorgid': 'wxid_other', 'msg': 'x'},
                     {'toid': 'filehelper', 'msg': '\0'}, {'toid': 'filehelper', 'msg': ''}):
            with self.assertRaises(ClientError) as caught:
                self.client.request('POST', '/SendTextMsg', body)
            self.assertEqual(caught.exception.status, 400)
        with self.assertRaises(ClientError) as caught:
            self.client.request('POST', '/SendVoiceMsg', {})
        self.assertEqual(caught.exception.body['error']['code'], 'not_implemented')
        spec = self.client.request('GET', '/openapi.json')
        self.assertEqual(spec['openapi'], '3.1.0')
        self.assertIn('/messages', spec['paths'])

    def test_realtime_delivery_and_ack(self):
        self.client.consumer('agent')
        result = []
        thread = threading.Thread(target=lambda: result.append(self.client.messages('agent', timeout=2)))
        thread.start()
        until = time.monotonic() + 1
        while 'agent' not in self.app.store.readers:
            self.assertLess(time.monotonic(), until)
            time.sleep(.005)
        self.app.store.append(self.backend.account, event('latest'))
        thread.join(1)
        self.assertFalse(thread.is_alive())
        self.assertEqual(result[0]['events'][0]['payload']['text'], 'latest')
        batch = self.client.messages('agent', timeout=0)
        self.assertEqual(batch['ack_token'], result[0]['ack_token'])
        self.client.ack('agent', batch['ack_token'])
        self.assertEqual(self.client.messages('agent', timeout=0)['events'], [])

    def test_sdk_handler_failure_never_acknowledges(self):
        self.app.store.append(self.backend.account, event())
        def fail(_):
            raise RuntimeError('business failed')
        with self.assertRaises(RuntimeError):
            self.client.listen('sdk', fail)
        self.assertEqual(len(self.client.messages('sdk', timeout=0)['events']), 1)

    def test_state_directory_exclusive(self):
        with self.assertRaises(RuntimeError):
            Application(self.temp.name, FixtureBackend())

    def test_long_poll_quota_leaves_send_and_status_available(self):
        threads, failures = [], []
        def poll(name):
            try:
                self.client.messages(name, timeout=3)
            except Exception as error:
                failures.append(str(error))
        for i in range(16):
            name = f'wait{i}'
            self.client.consumer(name)
            thread = threading.Thread(target=poll, args=(name,))
            thread.start()
            threads.append(thread)
        try:
            until = time.monotonic() + 2
            while len(self.app.store.readers) < 16:
                self.assertLess(time.monotonic(), until)
                time.sleep(.005)
            self.client.consumer('overflow')
            with self.assertRaises(ClientError) as caught:
                self.client.messages('overflow', timeout=0)
            self.assertEqual(caught.exception.status, 429)
            self.assertEqual(self.client.send_text('filehelper', 'not blocked')['state'], 'accepted')
            self.assertEqual(self.client.status()['capture']['state'], 'running')
        finally:
            self.app.store.append(self.backend.account, event())
            for thread in threads:
                thread.join(4)
        self.assertEqual(failures, [])

    def test_callback_failure_pauses_and_resume_redelivers_same_event(self):
        received = []
        class Receiver(BaseHTTPRequestHandler):
            fail = True
            def do_POST(self):
                received.append(json.loads(self.rfile.read(int(self.headers['Content-Length']))))
                self.send_response(503 if Receiver.fail else 204)
                self.end_headers()
            def log_message(self, *args):
                pass
        receiver = ThreadingHTTPServer(('127.0.0.1', 0), Receiver)
        thread = threading.Thread(target=receiver.serve_forever)
        thread.start()
        try:
            self.app.callback_url = f'http://127.0.0.1:{receiver.server_port}/callback'
            self.app.store.consumer('callback', self.backend.account, 'earliest')
            self.app._thread(self.app._callback_loop)
            self.app.store.append(self.backend.account, event('callback text'))
            until = time.monotonic() + 3
            while not self.app.store.get_meta('callback', {}).get('paused'):
                self.assertLess(time.monotonic(), until)
                time.sleep(.01)
            time.sleep(.1)
            self.assertEqual(len(received), 1)
            Receiver.fail = False
            self.client.request('POST', '/client_hook/callback/resume', {})
            until = time.monotonic() + 3
            while self.app.store.consumer('callback', self.backend.account)['pending']:
                self.assertLess(time.monotonic(), until)
                time.sleep(.01)
            self.assertEqual(len(received), 2)
            self.assertEqual(received[0]['msglist'][0]['event_id'], received[1]['msglist'][0]['event_id'])
            self.assertEqual(received[1]['msglist'][0]['msg'], 'callback text')
        finally:
            receiver.shutdown()
            receiver.server_close()

    def test_malformed_duplicate_and_nonobject_json(self):
        for raw in (b'[]', b'{', b'{"msg":"a","msg":"b"}', b'{"timeout":NaN}'):
            request = Request(self.url + '/SendTextMsg', raw,
                              {'Content-Type': 'application/json', 'Authorization': 'Bearer test-token'})
            with self.assertRaises(HTTPError) as caught:
                urlopen(request, timeout=3)
            self.assertEqual(caught.exception.code, 400)
            caught.exception.close()


if __name__ == '__main__':
    unittest.main()
