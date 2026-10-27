"""Three receive-only start/stop rounds in the fixed official client."""

from pathlib import Path
import sys

MODULE_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(MODULE_DIR.parent))
sys.path.insert(0, str(MODULE_DIR))
sys.path.insert(0, str(MODULE_DIR / 'tests/lifecycle'))

import argparse
import json
import subprocess
import time
from test_resident import Client
from hook_runtime.loader import ROOT
from hook_runtime.service import WORK

def main():
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--endpoint',required=True);args=p.parse_args()
    records=[];clients=[]
    def command(name):
        process=subprocess.run([sys.executable,'-B','-X','utf8',str(Path(__file__).resolve().parents[1] / 'scripts/receiver.py'),name],
                               capture_output=True,text=True,encoding='utf-8',timeout=30)
        assert process.returncode==0,(process.stdout,process.stderr)
        return json.loads(process.stdout)
    try:
        for i in range(3):
            c=Client(None,endpoint=args.endpoint);clients.append(c)
            loading=c.next('loading');ready=c.next('ready')
            state=command('status');assert state['state']=='running' and state['generation']==ready['generation']
            time.sleep(.5)
            if i==1:c.stop()
            else:assert command('stop')['type']=='stop_requested'
            result,end=c.finish()
            after=command('status')
            assert after['state']=='idle' and after['installed']==0 and after['admitted']==0
            records.append(dict(loading=loading,ready=ready,end=end,result=result,after=after))
            if i:
                assert loading['dll_reused'] and loading['dll_base']==records[0]['loading']['dll_base']
                assert ready['generation']==records[-2]['ready']['generation']+1
        report={'passed':True,'rounds':records,'send_calls':0,'scope':'official_4.1.9.30_same_dll_start_stop_no_generated_test_input'}
        (WORK/'resident-live-results.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
        print(json.dumps({'passed':True,'rounds':3,'dll_reused':[r['loading']['dll_reused'] for r in records],
                          'dll_base':records[0]['loading']['dll_base'],'generations':[r['ready']['generation'] for r in records],
                          'messages':[r['end']['emitted'] for r in records],'final_state':'idle','send_calls':0}))
    finally:
        for c in clients:
            if c.process.poll() is None:
                try:c.stop()
                except OSError:pass
                c.process.wait(timeout=20)

if __name__=='__main__':main()
