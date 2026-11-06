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
RECOVERY_INTERVAL = 5
RECOVERY_MAX_INTERVAL = 30


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
        self.capture_pid = None
        self.capture_created = None
        self.capture_dll = None

    def start(self, store):
        self.store = store
        self.minimum_created_at = math.ceil(time.time())
        with store.transaction():
            store.db.execute('''CREATE TABLE IF NOT EXISTS native_resources(id TEXT PRIMARY KEY,account TEXT NOT NULL,
                conversation TEXT NOT NULL,kind TEXT NOT NULL,xml TEXT NOT NULL,created REAL NOT NULL)''')
        self._launch_receiver()
        self.thread = threading.Thread(target=self._supervise, daemon=True)
        self.thread.start()
        if not self.ready.wait(20):
            self.stop()
            raise RuntimeError(f'接收器启动未完成：{self.error or self.errors}')
        if self.error:
            self.stop()
            raise RuntimeError(self.error)

    def _launch_receiver(self, recovery=False):
        command = [sys.executable, '-B', '-X', 'utf8', str(MODULE / 'scripts/receiver.py'), 'events',
                   '--endpoint', self.endpoint]
        if self.args.fixture_pid:
            command += ['--fixture-pid', str(self.args.fixture_pid)]
        elif self.args.pid:
            command += ['--pid', str(self.args.pid)]
        elif recovery:
            command += ['--pid', str(self.capture_pid)]
        if recovery:
            command += ['--expected-account', self.account, '--expected-creation', self.capture_created]
        with self.stop_lock:
            if self.stopped.is_set():
                return False
            self.error = None
            if recovery:
                self.minimum_created_at = math.ceil(time.time())
            self.process = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                            stderr=subprocess.PIPE, text=True, encoding='utf-8')
        process = self.process
        self.errors = []
        def errors():
            for line in process.stderr:
                self.errors[:] = (self.errors + [line.strip()])[-10:]
        self.stderr_thread = threading.Thread(target=errors, daemon=True)
        self.stderr_thread.start()
        return True

    def _supervise(self):
        while not self.stopped.is_set():
            recoverable = self._collect()
            self._close_receiver_streams()
            if not recoverable or self.stopped.is_set():
                if not self.stopped.is_set():
                    self._recovery_status('blocked', reason='capture_exit_not_safe_for_recovery')
                return
            if not self._recover_login():
                return

    def _recovery_status(self, state, **details):
        self.store.set_meta('capture_recovery', {'state':state, 'updated_at':time.time(), **details})

    def _recover_login(self):
        from hook_runtime.login_identity import LoginUnavailable
        from hook_runtime.service import probe_login
        attempts = 0
        stable = 0
        interval = RECOVERY_INTERVAL
        self._recovery_status('waiting_login', attempts=attempts)
        while not self.stopped.wait(interval):
            attempts += 1
            try:
                login = probe_login(SimpleNamespace(pid=self.capture_pid, fixture_pid=self.args.fixture_pid))
                if (login['pid']!=self.capture_pid or str(login['created'])!=self.capture_created or
                        Path(login['dll']).resolve()!=Path(self.capture_dll).resolve()):
                    raise RuntimeError('recovery process or resident DLL changed')
                if login['account'] != self.account:
                    stable = 0
                    self._recovery_status('waiting_same_account', attempts=attempts)
                else:
                    stable += 1
                    self._recovery_status('confirming_login', attempts=attempts)
                    if stable >= 2:
                        self._recovery_status('reconnecting', attempts=attempts)
                        return self._launch_receiver(recovery=True)
                interval = RECOVERY_INTERVAL
            except LoginUnavailable:
                stable = 0
                self._recovery_status('waiting_login', attempts=attempts)
                interval = min(RECOVERY_MAX_INTERVAL, interval * 2)
            except (OSError, RuntimeError, ValueError, KeyError):
                self._recovery_status('blocked', attempts=attempts,
                                      reason='process_version_or_resident_validation_failed')
                return False
        self._recovery_status('stopped', attempts=attempts)
        return False

    def _collect(self):
        end = verified_end = None
        failed = False
        try:
            for line in self.process.stdout:
                item = json.loads(line)
                kind = item.get('type')
                if kind == 'loading':
                    if self.capture_pid is not None and (item['pid']!=self.capture_pid or
                            item.get('process_created')!=self.capture_created or item.get('dll')!=self.capture_dll):
                        raise RuntimeError('recovery receiver target mismatch')
                    self.instance = f"{item['pid']}:{item['run_id']}"
                    self.event_file = Path(item['event_file'])
                    if self.capture_pid is None:
                        self.capture_pid = item['pid']
                        self.capture_created = item.get('process_created')
                        self.capture_dll = item.get('dll')
                elif kind == 'ready':
                    if self.account is not None and item['self_id']!=self.account:
                        raise RuntimeError('recovery ready account mismatch')
                    self.account = item['self_id']
                    # 就绪前保留精确的秒级时间边界；
                    # 不采集出站 UI 路径加载的历史记录。
                    if not self.args.fixture_pid:
                        self.stopped.wait(max(0,self.minimum_created_at-time.time()))
                    self.store.capture('running', account=self.account, instance=self.instance,
                                       scope='text_and_six_attachment_kinds_incoming_outgoing_since_service_start',
                                       outgoing_min_created_at=self.minimum_created_at,native=item)
                    self.ready.set()
                    self._recovery_status('running')
                elif kind == 'message':
                    if item.get('self_id')!=self.account:
                        raise RuntimeError('receiver event account mismatch')
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
                    end = item
                    self.store.capture('stopped' if item['code'] == 0 else 'fault', account=self.account,
                                       native=item, reason=item['reason'])
                elif kind == 'verified_end':
                    verified_end = item
                elif kind == 'controller_error':
                    failed = True
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
            failed = True
            self.error = str(error)
            try:
                self.store.capture('fault', account=self.account, error=self.error, coverage_gap=True)
            finally:
                self._request_stop()
        finally:
            self.ready.set()
        recoverable = (not failed and end is not None and verified_end is not None and self.account is not None and
            self.capture_created is not None and self.capture_dll is not None and
            end.get('reason')=='identity_changed' and end.get('code')==4 and
            end.get('restored') is True and end.get('active')==0 and
            verified_end.get('state')=='idle' and verified_end.get('code')==4 and self.process.returncode==4)
        if recoverable and not self.stopped.is_set():
            gaps = self.store.get_meta('coverage_gaps', [])
            self.store.set_meta('coverage_gaps', (gaps + [{'reason':'login_identity_interrupted',
                'detected_at':time.time(), 'lost_message_count':None}])[-20:])
        return recoverable

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
        self.stopped.set()
        with self.stop_lock:
            self._request_stop()
            process = self.process
        if process:
            process.wait(timeout=15)
        if hasattr(self, 'thread') and self.thread is not threading.current_thread():
            self.thread.join(10)
            if self.thread.is_alive():
                raise RuntimeError('采集恢复线程尚未退出，保留运行状态')
        self._close_receiver_streams()

    def _close_receiver_streams(self):
        if self.process and self.process.poll() is not None:
            if hasattr(self, 'stderr_thread'):
                self.stderr_thread.join(5)
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
