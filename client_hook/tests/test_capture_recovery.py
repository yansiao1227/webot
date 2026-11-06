"""登录恢复的只读身份校验、退出门禁和自有 host 生命周期。"""
import ctypes as C
import io
import json
from pathlib import Path
import struct
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from api.backend import NativeBackend
from api.server import Application
from api.store import Store
from hook_runtime.login_identity import LoginUnavailable, read_identity
from hook_runtime.loader import Win, PE, export_rva, normalized
from hook_runtime.service import WORK


class IdentityTests(unittest.TestCase):
    def setUp(self):
        self.base, self.root, self.obj = 0x180000000, 0x200000000, 0x300000000
        self.account = b'wxid_test_self'
        self.header = self.account + b'\0' * (16-len(self.account)) + struct.pack('<QQ',len(self.account),15)
        self.memory = {
            (self.base+0xa6634b8,8):struct.pack('<Q',self.root),
            (self.root,8):struct.pack('<Q',self.base+0x8040838),
            (self.root+0x68,16):struct.pack('<QQ',self.obj,1),
            (self.obj,8):struct.pack('<Q',self.base+0x80e58e8),
            (self.base+0x80e58e8+0x18,8):struct.pack('<Q',self.base+0x3035b0),
            (self.base+0x3035b0,5):bytes.fromhex('488d4148c3'),
            (self.obj+0x48,32):self.header,
            (self.obj+0x48,len(self.account)+1):self.account+b'\0',
        }
        self.api=Mock()
        self.api.read.side_effect=lambda process,address,size:self.memory[(address,size)]

    def test_stable_fixed_version_identity_is_read_without_writes(self):
        self.assertEqual(read_identity(self.api,1,self.base),self.account.decode())
        self.assertEqual(set(c[0] for c in self.api.method_calls),{'read'})

    def test_logged_out_or_wrong_structures_are_not_accepted(self):
        self.memory[(self.root,8)]=struct.pack('<Q',0)
        with self.assertRaises(LoginUnavailable):read_identity(self.api,1,self.base)

    def test_identity_change_between_snapshots_is_rejected(self):
        count=0
        def read(process,address,size):
            nonlocal count
            if (address,size)==(self.root+0x68,16):
                count+=1
                if count==2:return struct.pack('<QQ',self.obj+16,1)
            return self.memory[(address,size)]
        self.api.read.side_effect=read
        with self.assertRaises(LoginUnavailable):read_identity(self.api,1,self.base)

    def test_heap_identity_and_invalid_pointer_bounds(self):
        data=0x400000000
        self.memory[(self.obj+0x48,32)]=struct.pack('<QQQQ',data,0,len(self.account),31)
        self.memory[(data,len(self.account)+1)]=self.account+b'\0'
        self.assertEqual(read_identity(self.api,1,self.base),self.account.decode())
        self.memory[(self.obj+0x48,32)]=struct.pack('<QQQQ',(1<<64)-1,0,len(self.account),31)
        with self.assertRaises(LoginUnavailable):read_identity(self.api,1,self.base)


class RecoveryTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.store=Store(Path(self.temp.name)/'messages.db')
        self.backend=NativeBackend(fixture_pid=123)
        self.backend.store=self.store
        self.backend.account='wxid_fixture_self'
        self.backend.capture_pid=123
        self.backend.capture_created='456'
        self.backend.capture_dll=str(Path(self.temp.name)/'hook-fixture.dll')
        self.backend.stopped=Mock()
        self.backend.stopped.is_set.return_value=False
        self.backend.stopped.wait.side_effect=[False]*10+[True]
        self.login={'account':self.backend.account,'pid':123,'created':456,'dll':self.backend.capture_dll}

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def test_same_account_requires_two_stable_probes_and_keeps_stop_interruptible(self):
        with patch('hook_runtime.service.probe_login',side_effect=[LoginUnavailable(),self.login,self.login]) as probe, \
                patch.object(self.backend,'_launch_receiver',return_value=True) as launch:
            self.assertTrue(self.backend._recover_login())
        self.assertEqual(probe.call_count,3)
        launch.assert_called_once_with(recovery=True)
        self.assertEqual([c.args[0] for c in self.backend.stopped.wait.call_args_list],[5,10,5])

    def test_wrong_account_never_changes_bound_account_or_launches(self):
        with patch('hook_runtime.service.probe_login',return_value={**self.login,'account':'wxid_other'}), \
                patch.object(self.backend,'_launch_receiver') as launch:
            self.assertFalse(self.backend._recover_login())
        launch.assert_not_called()
        self.assertEqual(self.backend.account,'wxid_fixture_self')

    def test_explicit_stop_cancels_recovery_without_probe_or_launch(self):
        self.backend.stopped.wait.side_effect=[True]
        with patch('hook_runtime.service.probe_login') as probe, patch.object(self.backend,'_launch_receiver') as launch:
            self.assertFalse(self.backend._recover_login())
        probe.assert_not_called();launch.assert_not_called()

    def test_process_or_dll_change_blocks_recovery(self):
        for update in ({'pid':124},{'created':457},{'dll':str(Path(self.temp.name)/'other.dll')}):
            with self.subTest(update=update), patch('hook_runtime.service.probe_login',return_value={**self.login,**update}), \
                    patch.object(self.backend,'_launch_receiver') as launch:
                self.assertFalse(self.backend._recover_login())
                self.assertEqual(self.store.get_meta('capture_recovery')['state'],'blocked')
                launch.assert_not_called()

    def test_unknown_exit_or_incomplete_drain_does_not_retry(self):
        loading={'type':'loading','pid':123,'run_id':'test','event_file':'test.ndjson',
                 'process_created':'456','dll':self.backend.capture_dll}
        for reason,restored,active,verified in [('identity_changed',True,0,True),
                ('queue_overflow',True,0,True),('identity_changed',False,0,True),
                ('identity_changed',True,1,True),('identity_changed',True,0,False)]:
            lines=[loading,{'type':'end','code':4,'reason':reason,'restored':restored,'active':active}]
            if verified:lines.append({'type':'verified_end','code':4,'state':'idle'})
            p=Mock(returncode=4);p.stdout=io.StringIO(''.join(json.dumps(x)+'\n' for x in lines));p.wait.return_value=4
            self.backend.process=p
            with self.subTest(reason=reason,restored=restored,active=active,verified=verified):
                self.assertEqual(self.backend._collect(),reason=='identity_changed' and restored and active==0 and verified)

    def test_wrong_ready_account_and_events_are_not_ingested(self):
        p=Mock(returncode=4)
        p.stdout=io.StringIO(json.dumps({'type':'ready','self_id':'wxid_other'})+'\n')
        self.backend.process=p
        with patch.object(self.backend,'_request_stop'):
            self.assertFalse(self.backend._collect())
        self.assertEqual(self.backend.account,'wxid_fixture_self')
        self.assertEqual(self.store.status()['events'],0)


class OwnedHostRecoveryTests(unittest.TestCase):
    def test_same_process_logout_login_reuses_dll_and_preserves_pending_batch(self):
        # 仅操作本测试创建的 hook-host.exe，不连接真实微信。
        pins=json.loads((WORK/'runtime-current.json').read_text(encoding='utf-8-sig'))
        directory=Path(pins['directory'])
        host=subprocess.Popen([str(directory/'hook-host.exe')],stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,text=True,encoding='utf-8')
        app=None
        api=Win();handle=None
        temp=tempfile.TemporaryDirectory()
        def command(text):
            host.stdin.write(text+'\n');host.stdin.flush()
            return json.loads(host.stdout.readline())
        def wait_for(check):
            until=time.monotonic()+12
            while not check():
                self.assertLess(time.monotonic(),until)
                time.sleep(.02)
        try:
            self.assertEqual(json.loads(host.stdout.readline())['type'],'host_ready')
            with patch('api.backend.RECOVERY_INTERVAL',.05), \
                    patch('api.backend.RECOVERY_MAX_INTERVAL',.1):
                app=Application(temp.name,NativeBackend(fixture_pid=host.pid))
                account=app.backend.account
                before=app.backend.status()
                app.store.consumer('stable-consumer',account,'latest')
                with app.store.condition:
                    app.store.enqueue('original-unknown-send',account,{'toid':'filehelper','msg':'不得自动重发'})
                    app.store.finish('original-unknown-send','unknown',{'reason':'original_uncertain_send'})
                command('emit')
                batch=app.store.read('stable-consumer',account,timeout=3,limit=1)
                self.assertEqual(len(batch['events']),1)
                command('logout')
                wait_for(lambda:app.store.get_meta('capture_recovery',{}).get('state')=='waiting_login')
                self.assertEqual(app.store.get_meta('capture')['state'],'fault')
                # 恢复自有 host 的登录标志，无需构建或改写已加载 DLL。
                handle=api.api.OpenProcess(0x1000|0x10|0x20|0x8,False,host.pid)
                self.assertTrue(handle)
                pe=PE(str(directory/'hook-host.exe'))
                base=next(base for path,base,_ in api.modules(host.pid) if path==normalized(directory/'hook-host.exe'))
                value=C.c_int32(1);written=C.c_size_t()
                self.assertTrue(api.api.WriteProcessMemory(handle,base+export_rva(pe,'fixture_logged_in',code=False),
                    C.byref(value),4,C.byref(written)))
                self.assertEqual(written.value,4)
                wait_for(lambda:app.store.get_meta('capture')['state']=='running')
                after=app.backend.status()
                self.assertGreater(after['generation'],before['generation'])
                self.assertEqual(after['dll_base'],before['dll_base'])
                self.assertEqual(app.store.operation('original-unknown-send')['state'],'unknown')
                self.assertEqual(command('stats')['submitted'],0)
                recovered=app.store.read('stable-consumer',account,timeout=0,limit=1)
                self.assertEqual({k:v for k,v in recovered.items() if k!='capture'},
                                 {k:v for k,v in batch.items() if k!='capture'})
                app.store.ack('stable-consumer',account,batch['ack_token'])
                command('emit')
                following=app.store.read('stable-consumer',account,timeout=3,limit=1)
                self.assertEqual(len(following['events']),1)
                self.assertNotEqual(following['events'][0]['event_id'],batch['events'][0]['event_id'])
                app.close();app=None
        finally:
            if handle:api.api.CloseHandle(handle)
            if app:app.close()
            if host.poll() is None:
                host.stdin.write('quit\n');host.stdin.flush();host.wait(timeout=10)
            host.stdin.close();host.stdout.close()
            temp.cleanup()
