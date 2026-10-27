"""同一宿主上重复会话、租约丢失及排空生命周期的回归测试。"""

from pathlib import Path
import sys

MODULE_DIR = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(MODULE_DIR.parent))
sys.path.insert(0, str(MODULE_DIR))

import ctypes as C
import json
import queue
import subprocess
import threading
import time
from hook_runtime.loader import ROOT
from hook_runtime.service import WORK
from hook_runtime.loader import Win,normalized

SCRIPT=Path(__file__).resolve().parents[2] / 'scripts/receiver.py'

class Client:
    def __init__(self,pid,endpoint='wxid_fixture_peer',maximum=0):
        target=['--fixture-pid',str(pid)] if pid is not None else []
        self.process=subprocess.Popen([sys.executable,'-B','-X','utf8',str(SCRIPT),'events',
            *target,'--endpoint',endpoint,'--duration','0','--max-events',str(maximum)],
            stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True,encoding='utf-8')
        self.lines=queue.Queue();self.events=[]
        def read():
            for line in self.process.stdout:self.lines.put(line)
            self.lines.put(None)
        threading.Thread(target=read,daemon=True).start()
    def next(self,kind=None,timeout=20):
        deadline=time.monotonic()+timeout
        while True:
            line=self.lines.get(timeout=max(.01,deadline-time.monotonic()))
            if line is None:raise AssertionError(('unexpected controller exit',self.process.poll(),self.events,self.process.stderr.read()))
            event=json.loads(line);self.events.append(event)
            if event['type']=='controller_error':raise AssertionError(event)
            if kind is None or event['type']==kind:return event
    def stop(self):
        self.process.stdin.write('stop\n');self.process.stdin.flush()
    def finish(self):
        result=self.next('verified_end')
        assert self.process.wait(timeout=10)==0
        end=next(e for e in reversed(self.events) if e['type']=='end')
        assert end['restored'] and end['active']==0 and end['dropped']==0
        return result,end

def main():
    pins=json.loads((WORK/'runtime-current.json').read_text(encoding='utf-8-sig'))
    directory=Path(pins['directory'])
    host=subprocess.Popen([str(directory/'hook-host.exe')],stdin=subprocess.PIPE,stdout=subprocess.PIPE,
                          stderr=subprocess.PIPE,text=True,encoding='utf-8')
    clients=[];checks={};api=Win();release=None
    try:
        assert json.loads(host.stdout.readline())['type']=='host_ready'
        release=api.api.CreateEventW(None,True,True,f'Local\\HookFixtureRelease-{host.pid}')
        def emit(command='emit',wait=True):
            host.stdin.write(command+'\n');host.stdin.flush()
            if wait:assert json.loads(host.stdout.readline())['type']=='emitted'
        def command(name):
            p=subprocess.run([sys.executable,'-B','-X','utf8',str(SCRIPT),name,'--fixture-pid',str(host.pid)],
                             capture_output=True,text=True,encoding='utf-8',timeout=20)
            assert p.returncode==0,(p.stdout,p.stderr)
            return json.loads(p.stdout)
        def start(**kwargs):
            c=Client(host.pid,**kwargs);clients.append(c)
            loading=c.next('loading');ready=c.next('ready')
            return c,loading,ready
        first,loading,ready=start()
        assert not loading['dll_reused'] and ready['generation']==1 and ready['duration_ms']==0
        base=loading['dll_base'];dll=Path(directory/'hook-fixture.dll')
        for i in (1,2):
            emit();event=first.next('message')
            assert event['generation']==1 and event['seq']==i and '中文' in event['body']
        checks['continuous_session_multiple_messages']=True
        competing=subprocess.run([sys.executable,'-B',str(SCRIPT),'events','--fixture-pid',str(host.pid),
            '--endpoint','wxid_fixture_peer','--duration','1'],capture_output=True,text=True,timeout=20)
        assert competing.returncode!=0 and 'another receive observer' in competing.stdout
        checks['concurrent_start_rejected']=True
        state=command('status');assert state['state']=='running' and state['generation']==1
        assert command('stop')['type']=='stop_requested'
        _,end=first.finish();assert end['emitted']==2 and end['reason']=='stop'
        assert command('stop')['already_stopped']
        before=Path(loading['event_file']).stat().st_size;emit()
        assert Path(loading['event_file']).stat().st_size==before
        checks['external_stop_idempotence_and_idle_silence']=True
        second,loaded2,ready2=start(maximum=1)
        assert loaded2['dll_reused'] and loaded2['dll_base']==base and ready2['generation']==2
        emit();second.next('message');second.finish()
        third,loaded3,ready3=start(endpoint='wxid_different_peer')
        emit();third.stop();_,end3=third.finish()
        assert end3['emitted']==0 and end3['filtered']==1 and end3['dropped']==0
        checks['session_subscription_and_counters_isolated']=True
        crashed,crash_load,crash_ready=start()
        emit();crashed.next('message')
        crashed.process.kill();crashed.process.wait(timeout=10) # 仅终止自有控制器。
        until=time.monotonic()+10
        while command('status')['state']!='idle':
            assert time.monotonic()<until
            time.sleep(.05)
        log=Path(crash_load['event_file'])
        end_crash=json.loads(log.read_text(encoding='utf-8').splitlines()[-1])
        assert end_crash['reason']=='controller_exit' and end_crash['restored'] and end_crash['active']==0
        checks['controller_death_restores_hook']=True
        recovered,loaded5,ready5=start(maximum=1)
        emit();recovered.next('message');recovered.finish()
        assert loaded5['dll_reused'] and loaded5['dll_base']==base
        checks['same_dll_reuse_after_controller_death']=True
        held,held_load,held_ready=start()
        emit('emit-held',wait=False);held.next('message');held.stop()
        # 会话门禁关闭后，原始调用仍按设计保持阻塞。
        assert held.process.wait(timeout=15)!=0
        state=command('status');assert state['state']=='draining' and state['admitted']==1 and not state['installed']
        blocked=Client(host.pid);clients.append(blocked)
        assert blocked.process.wait(timeout=15)!=0
        assert command('status')['generation']==held_ready['generation']
        checks['draining_blocks_restart_without_freeing_session']=True
        api.api.SetEvent(release)
        assert json.loads(host.stdout.readline())['type']=='emitted'
        reaped,loaded7,ready7=start(maximum=1)
        assert ready7['generation']==held_ready['generation']+1 and loaded7['dll_base']==base
        emit();fresh=reaped.next('message');_,end7=reaped.finish()
        assert fresh['seq']==1 and end7['entry_hits']==1 and end7['rejected']==end7['cancelled']==0
        checks['retired_session_reaped_and_fresh_session_clean']=True
        modules=[m for m in api.modules(host.pid) if m[0]==normalized(dll)]
        assert len(modules)==1 and hex(modules[0][1])==base
        state=command('status');assert state['state']=='idle' and state['admitted']==0 and not state['installed']
        checks['one_loaded_dll_for_all_sessions']=True
        result={'passed':True,'checks':checks,'dll_base':base,'generation':state['generation'],
                'host_pid':host.pid,'scope':'own_host_only_no_weixin_calls'}
        (WORK/'resident-test-results.json').write_text(json.dumps(result,indent=2),encoding='utf-8')
        print(json.dumps(result))
    finally:
        if release:api.api.SetEvent(release)
        for c in clients:
            if c.process.poll() is None:
                try:c.stop()
                except OSError:pass
                c.process.wait(timeout=15)
        if host.poll() is None:
            host.stdin.write('quit\n');host.stdin.flush();host.wait(timeout=15)
        if release:api.api.CloseHandle(release)

if __name__=='__main__':main()
