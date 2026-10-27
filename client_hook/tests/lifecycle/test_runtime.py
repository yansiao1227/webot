"""Exercise DLL loading, local output and stop/expiry in our own host process."""

from pathlib import Path
import sys

MODULE_DIR = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(MODULE_DIR.parent))
sys.path.insert(0, str(MODULE_DIR))

import json
import queue
import subprocess
import threading
import time
from hook_runtime.service import WORK

ROOT=Path(__file__).resolve().parents[3]


def exercise(expire=False, automatic=False, slow=False):
    pins=json.loads((WORK/'runtime-current.json').read_text(encoding='utf-8-sig'))
    directory=Path(pins['directory'])
    host=subprocess.Popen([str(directory/'hook-host.exe')],stdin=subprocess.PIPE,stdout=subprocess.PIPE,
                          stderr=subprocess.PIPE,text=True,encoding='utf-8')
    receiver=None
    seen=[]
    try:
        assert json.loads(host.stdout.readline())['type']=='host_ready'
        receiver=subprocess.Popen([sys.executable,'-B','-X','utf8',str(Path(__file__).resolve().parents[2] / 'scripts/receiver.py'),
            '--fixture-pid',str(host.pid),'--endpoint','wxid_fixture_peer',
            '--duration','2' if expire else '20','--max-events','1' if automatic else '0'],stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True,encoding='utf-8')
        lines=queue.Queue()
        def consume():
            for line in receiver.stdout: lines.put(line)
            lines.put(None)
        threading.Thread(target=consume,daemon=True).start()
        def next_event(timeout=20):
            line=lines.get(timeout=timeout)
            if line is None: raise AssertionError('receiver ended: '+receiver.stderr.read())
            item=json.loads(line);seen.append(item)
            if item['type']=='controller_error': raise AssertionError(item)
            return item
        while next_event()['type']!='ready': pass
        if not expire:
            for _ in range(1 if automatic or slow else 3):
                host.stdin.write('emit-slow\n' if slow else 'emit\n');host.stdin.flush()
                if not slow: assert json.loads(host.stdout.readline())['type']=='emitted'
                while True:
                    item=next_event()
                    if item['type']=='message':
                        assert item['body']=='中文 local event\n"fixture"'
                        assert item['endpoint_18']=='wxid_fixture_peer'
                        break
            if not automatic:
                receiver.stdin.write('stop\n');receiver.stdin.flush()
        while next_event()['type']!='verified_end': pass
        assert receiver.wait(timeout=10)==0
        end=next(x for x in reversed(seen) if x['type']=='end')
        assert end['restored'] and end['active']==0 and end['dropped']==0
        expected = 0 if expire else 1 if automatic or slow else 3
        assert end['emitted']==expected
        assert end['entry_hits']==expected and end['route_missed']==0
        if slow: assert json.loads(host.stdout.readline())['type']=='emitted'
        # Original is still callable after worker exits; DLL stays resident.
        host.stdin.write('emit\n');host.stdin.flush()
        assert json.loads(host.stdout.readline())['calls']==expected+1
        return {'mode':'expiry' if expire else 'max_events' if automatic else 'in_flight_stop' if slow else 'explicit_stop','passed':True,'events':seen}
    finally:
        if receiver and receiver.poll() is None:
            try: receiver.stdin.write('stop\n');receiver.stdin.flush()
            except OSError: pass
            receiver.wait(timeout=30)
        if host.poll() is None:
            host.stdin.write('quit\n');host.stdin.flush();host.wait(timeout=10)


if __name__=='__main__':
    results=[exercise(),exercise(True),exercise(automatic=True),exercise(slow=True)]
    (WORK/'runtime-test-results.json').write_text(json.dumps(results,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps({'passed':all(x['passed'] for x in results),'cases':len(results)}))
