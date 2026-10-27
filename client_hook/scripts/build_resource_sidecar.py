"""构建固定版本、通过所属队列下载的旁路模块，构建过程不加载模块。"""

from pathlib import Path
import sys

MODULE_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(MODULE_DIR.parent))
sys.path.insert(0, str(MODULE_DIR))

from tool.settings import executable_setting, path_setting

import hashlib
import argparse
import json
import shutil
import subprocess
import time

from hook_runtime.pe_local import PE
from hook_runtime.service import current_build

ROOT = Path(__file__).resolve().parents[1]
CORE = path_setting('RE_WECHAT_WEIXIN_DLL', 'client_hook/state/Weixin.dll')
SHA = '89fc940377a779babbd64c9cd4abe6256663681d92f12802aa2df97a8db18494'


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--task-observer', action='store_true', help='启用固定版本发送任务构造观测（不执行发送）')
    args=parser.parse_args()
    pe = PE(str(CORE))
    if hashlib.sha256(pe.data).hexdigest() != SHA:
        raise ValueError('核心版本不匹配')
    output = ROOT / 'build' / ('resource-' + str(time.time_ns()))
    output.mkdir()
    source_digest = hashlib.sha256(bytes([args.task_observer])+b''.join(p.name.encode()+b'\0'+p.read_bytes()
        for p in sorted((ROOT / 'native').glob('*')) if p.is_file())).hexdigest()[:12]
    dll_name = 'client-hook-resource-' + source_digest + '.dll'
    prior, _ = current_build()
    for name in ('queue-real-profile.h', 'queue-fixture-profile.h', 'runtime_pins.h'):
        shutil.copyfile(prior / name, output / name)
    entries = []
    for rva in (0x18f430, 0x18eab0, 0x191cd0, 0x954c00, 0x952010, 0x31d990,
                0x96bbc0, 0x20c1bb0, 0x20c1d80, 0x96d8d0, 0x94d640):
        raw = pe.data[pe.rva_to_offset(rva):][:16]
        entries.append(' {0x%xu,{%s}},' % (rva, ','.join('0x%02x' % b for b in raw)))
    (output / 'resource-profile.h').write_text('static const struct profile_entry resource_functions[]={\n' +
                                              '\n'.join(entries) + '\n};\n')
    vendor = ROOT / 'vendor/minhook'
    sources = [ROOT / 'native/resource_sidecar.c', ROOT / 'native/stream_snapshot.c'] + [
        vendor / path for path in ('src/buffer.c', 'src/hook.c', 'src/trampoline.c', 'src/hde/hde64.c')]
    command = [executable_setting('RE_WECHAT_GCC', 'gcc'), '-std=c11', '-O2', '-Wall', '-Wextra', '-Werror=format',
               '-static-libgcc', '-D_WIN32_WINNT=0x0601', '-shared', '-I', str(output),
               '-I', str(ROOT / 'native'), '-I', str(vendor / 'include'), *map(str, sources),
               '-o', str(output / dll_name), '-lbcrypt', '-lpsapi']
    if args.task_observer:
        command.insert(1,'-DCH_SEND_TASK_OBSERVER')
    result = subprocess.run(command, capture_output=True, creationflags=subprocess.CREATE_NO_WINDOW)
    (output / 'compiler.log').write_bytes(result.stdout + result.stderr)
    if result.returncode:
        raise RuntimeError('编译失败：' + (result.stdout + result.stderr).decode('utf-8', errors='replace'))
    pins = {'core_sha256': SHA, 'directory': str(output),
            'task_observer': args.task_observer,
            'dll_name': dll_name, 'dll_sha256': hashlib.sha256((output / dll_name).read_bytes()).hexdigest(),
            'sources': {str(p.relative_to(ROOT)): hashlib.sha256(p.read_bytes()).hexdigest()
                        for p in (ROOT / 'native').glob('*') if p.is_file()}}
    (output / 'pins.json').write_text(json.dumps(pins, indent=2))
    (ROOT / 'build/resource-current.json').write_text(json.dumps(pins, indent=2))
    print(json.dumps({'built': str(output), 'loaded': False}))


if __name__ == '__main__':
    main()
