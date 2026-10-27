"""驻留引擎适配；测试后端必须显式选择，不会回退到模拟成功。"""
from __future__ import annotations

import json
import math
from pathlib import Path
import subprocess
import sys
import threading
import time
from types import SimpleNamespace

from .errors import ApiError
from .mentions import parse_mentions
from .messages import native_event, current_outgoing

MODULE = Path(__file__).resolve().parents[1]


class NativeBackend:
    def __init__(self, pid=None, fixture_pid=None, endpoint='*'):
        self.args = SimpleNamespace(pid=pid, fixture_pid=fixture_pid)
        self.endpoint = endpoint
        self.account = None
        self.instance = None
        self.process = None
        self.stopped = threading.Event()
        self.ready = threading.Event()
        self.error = None
        self.stop_lock = threading.Lock()

    def start(self, store):
        self.store = store
        self.minimum_created_at = math.ceil(time.time())
        with store.transaction():
            store.db.execute('''CREATE TABLE IF NOT EXISTS native_resources(id TEXT PRIMARY KEY,account TEXT NOT NULL,
                conversation TEXT NOT NULL,kind TEXT NOT NULL,xml TEXT NOT NULL,created REAL NOT NULL)''')
        command = [sys.executable, '-B', '-X', 'utf8', str(MODULE / 'scripts/receiver.py'), 'events',
                   '--endpoint', self.endpoint]
        if self.args.fixture_pid:
            command += ['--fixture-pid', str(self.args.fixture_pid)]
        elif self.args.pid:
            command += ['--pid', str(self.args.pid)]
        self.process = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                        stderr=subprocess.PIPE, text=True, encoding='utf-8')
        self.errors = []
        def errors():
            for line in self.process.stderr:
                self.errors[:] = (self.errors + [line.strip()])[-10:]
        threading.Thread(target=errors, daemon=True).start()
        self.thread = threading.Thread(target=self._collect, daemon=True)
        self.thread.start()
        if not self.ready.wait(20):
            self.stop()
            raise RuntimeError(f'接收器启动未完成：{self.error or self.errors}')
        if self.error:
            self.stop()
            raise RuntimeError(self.error)

    def _collect(self):
        try:
            for line in self.process.stdout:
                item = json.loads(line)
                kind = item.get('type')
                if kind == 'loading':
                    self.instance = f"{item['pid']}:{item['run_id']}"
                    self.event_file = Path(item['event_file'])
                elif kind == 'ready':
                    self.account = item['self_id']
                    # 就绪前保留精确的秒级时间边界；
                    # 不采集出站 UI 路径加载的历史记录。
                    if not self.args.fixture_pid:
                        self.stopped.wait(max(0,self.minimum_created_at-time.time()))
                    self.store.capture('running', account=self.account, instance=self.instance,
                                       scope='text_and_six_attachment_kinds_incoming_outgoing_since_service_start',
                                       outgoing_min_created_at=self.minimum_created_at,native=item)
                    self.ready.set()
                elif kind == 'message':
                    event = native_event(item, self.instance)
                    if (not self.args.fixture_pid and event['direction']=='outgoing'
                            and event.get('native_record_metadata_source')=='weixin41930_record_identity_v2'):
                        from .native_identity import enrich_outgoing
                        event=enrich_outgoing(event,item['self_id'],event['conversation_id'])
                    if not self.args.fixture_pid and event['direction']=='outgoing':
                        stats=self.store.get_meta('capture').get('history_filter',
                            {'filtered_old':0,'accepted_current':0,'unresolved':0,
                             'source':'exact_native_db_record_metadata'})
                        allowed=current_outgoing(event,self.minimum_created_at)
                        verified=event.get('native_message_id_source')=='weixin41930_message_db_exact_sort_seq_v1'
                        stats['accepted_current' if allowed else 'filtered_old' if verified else 'unresolved']+=1
                        self.store.set_meta('capture',{**self.store.get_meta('capture'),'history_filter':stats})
                        if not allowed:
                            continue
                    native_resource = event.pop('_native_resource', None)
                    if native_resource:
                        with self.store.transaction():
                            self.store.db.execute('INSERT OR IGNORE INTO native_resources VALUES(?,?,?,?,?,?)',
                                (native_resource['id'], item['self_id'], event['conversation_id'],
                                 native_resource['kind'], native_resource['xml'], self.store.clock()))
                    event['mentions'] = parse_mentions(item)
                    self.store.append(item['self_id'], event)
                elif kind == 'end':
                    self.store.capture('stopped' if item['code'] == 0 else 'fault', account=self.account,
                                       native=item, reason=item['reason'])
                elif kind == 'controller_error':
                    self.error = item['error']
                    self.store.capture('fault', account=self.account, error=self.error)
                    self.ready.set()
            code = self.process.wait()
            if not self.stopped.is_set():
                self.error = self.error or f'接收器退出：{code}'
                if self.store.get_meta('capture', {}).get('state') != 'fault':
                    self.store.capture('fault', account=self.account, error=self.error,
                                       coverage_gap=True)
        except Exception as error:
            self.error = str(error)
            try:
                self.store.capture('fault', account=self.account, error=self.error, coverage_gap=True)
            finally:
                self._request_stop()
        finally:
            self.ready.set()

    def fetch_resource(self, event):
        self.profile()
        from .native_assets import NativeAssets
        if not hasattr(self, 'assets'):
            self.assets = NativeAssets(self.store, self.account)
        return self.assets.fetch(event)

    def prune_resources(self, retention, now):
        if hasattr(self, 'assets'):
            from .native_download import prune
            prune(self.account, retention, now)

    def profile(self):
        capture = self.store.get_meta('capture')
        if capture.get('state') != 'running' or not self.account:
            raise ApiError(503, 'client_not_ready', '客户端身份未确认或接收已停止', capture=capture)
        return {'wxid': self.account, **{k: None for k in
                ('alias', 'nickname', 'email', 'qq', 'phone', 'proiv', 'area', 'signinfo')}}

    def status(self):
        from hook_runtime.service import inspect_status
        try:
            return inspect_status(self.args)
        except (RuntimeError, OSError, ValueError) as error:
            return {'state': 'unknown', 'error': str(error)}

    def send(self, request_id, account, params):
        from hook_runtime.sender import send_rpc
        if self.profile()['wxid'] != account:
            raise ApiError(409, 'account_changed', '发送账号已变化')
        result = send_rpc(self.args, request_id, account, params['toid'], params['msg'])
        while result['state'] in ('pending', 'preparing') and not self.stopped.wait(.1):
            preparing = result['state'] == 'preparing'
            result = send_rpc(self.args, request_id, account, params['toid'],
                              params['msg'] if preparing else '', query=not preparing)
        if result['state'] in ('pending', 'preparing'):
            result = {**result, 'state': 'unknown'}
        if (not self.args.fixture_pid and result['state']=='accepted' and
                result.get('native_sort_seq_source')=='weixin41930_send_task_record110_v1'):
            from .native_identity import enrich_outgoing
            result=enrich_outgoing(result,account,params['toid'])
        return result

    def _request_stop(self):
        if self.process and self.process.poll() is None:
            try:
                self.process.stdin.write('stop\n')
                self.process.stdin.flush()
            except (OSError, ValueError):
                pass

    def stop(self):
        with self.stop_lock:
            self.stopped.set()
            self._request_stop()
            if self.process:
                self.process.wait(timeout=15)
            if hasattr(self, 'thread') and self.thread is not threading.current_thread():
                self.thread.join(5)
            if self.process and self.process.poll() is not None:
                for stream in (self.process.stdin, self.process.stdout, self.process.stderr):
                    if stream:
                        stream.close()


class FixtureBackend:
    """仅用于纯 HTTP/存储测试；不会加载 DLL，能力状态明确为 simulation。"""
    account = 'wxid_fixture_self'
    def start(self, store):
        self.store = store
        self.sent = []
        store.capture('running', account=self.account, scope='simulation_only')

    def profile(self):
        return {'wxid': self.account, 'simulation': True}

    def send(self, request_id, account, params):
        self.sent.append((request_id, params))
        return {'state': 'accepted', 'simulation': True, 'local_accepted': True}

    def stop(self):
        pass
