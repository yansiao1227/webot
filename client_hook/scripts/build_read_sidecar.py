"""Build only: pinned native MarkSessionRead wrapper, no hooks or UI automation."""

from pathlib import Path
import sys

MODULE_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(MODULE_DIR.parent))
sys.path.insert(0, str(MODULE_DIR))

import hashlib
import json
import shutil
import subprocess
import time
from hook_runtime.pe_local import PE
from hook_runtime.service import current_build
from build_resource_sidecar import CORE, SHA, ROOT
from tool.settings import executable_setting

def main():
    pe=PE(str(CORE))
    if hashlib.sha256(pe.data).hexdigest()!=SHA:
        raise ValueError('微信核心摘要不符')
    directory=ROOT/'build'/('read-'+str(time.time_ns()));directory.mkdir()
    prior,_=current_build()
    for name in ('queue-real-profile.h','queue-fixture-profile.h','runtime_pins.h'):
        shutil.copyfile(prior/name,directory/name)
    # Direct wrapper and its queue/closure/core dispatch chain, not arbitrary addresses.
    entries=[]
    for rva in (0x15b3570,0x15d4f50,0x291e990,0xd66cb0):
        raw=pe.data[pe.rva_to_offset(rva):][:16]
        entries.append(' {0x%xu,{%s}},'%(rva,','.join('0x%02x'%b for b in raw)))
    (directory/'read-profile.h').write_text('static const struct profile_entry read_functions[]={\n'+'\n'.join(entries)+'\n};\n')
    vendor=ROOT/'vendor/minhook'
    sources=[ROOT/'native/read_sidecar.c',ROOT/'native/stream_snapshot.c']+[
        vendor/p for p in ('src/buffer.c','src/hook.c','src/trampoline.c','src/hde/hde64.c')]
    digest=hashlib.sha256(b''.join(p.read_bytes() for p in sorted((ROOT/'native').glob('*')) if p.is_file())).hexdigest()[:12]
    name='client-hook-read-'+digest+'.dll'
    command=[executable_setting('RE_WECHAT_GCC', 'gcc'),'-std=c11','-O2','-Wall','-Wextra','-Werror=format','-static-libgcc',
        '-D_WIN32_WINNT=0x0601','-shared','-I',str(directory),'-I',str(ROOT/'native'),'-I',str(vendor/'include'),
        *map(str,sources),'-o',str(directory/name),'-lbcrypt','-lpsapi']
    result=subprocess.run(command,capture_output=True,creationflags=subprocess.CREATE_NO_WINDOW)
    (directory/'compiler.log').write_bytes(result.stdout+result.stderr)
    if result.returncode: raise RuntimeError((result.stdout+result.stderr).decode(errors='replace'))
    test=directory/'test-read-dispatch.exe'
    subprocess.run([executable_setting('RE_WECHAT_GCC', 'gcc'),'-std=c11','-Wall','-Wextra','-Werror','-static-libgcc',
        str(ROOT/'native/test_read_dispatch.c'),'-o',str(test)],check=True,capture_output=True)
    subprocess.run([str(test)],check=True,timeout=5)
    pins={'directory':str(directory),'dll_name':name,'dll_sha256':hashlib.sha256((directory/name).read_bytes()).hexdigest(),
        'core_sha256':SHA,'abi_fixture_passed':True,'loaded':False}
    (directory/'pins.json').write_text(json.dumps(pins,indent=2))
    (ROOT/'build/read-current.json').write_text(json.dumps(pins,indent=2))
    print(json.dumps(pins))

if __name__=='__main__':main()
