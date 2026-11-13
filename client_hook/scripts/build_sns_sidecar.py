"""构建固定版本 SNS 侧车；不加载 DLL、不执行微信操作。"""
from pathlib import Path
import sys

MODULE_DIR = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(MODULE_DIR.parent), str(MODULE_DIR)]
import hashlib
import json
import shutil
import subprocess
import time
from build_resource_sidecar import CORE, SHA, ROOT
from hook_runtime.pe_local import PE
from hook_runtime.service import current_build
from tool.settings import executable_setting


def main():
    pe = PE(str(CORE))
    if hashlib.sha256(pe.data).hexdigest() != SHA:
        raise ValueError('微信核心摘要不符')
    directory = ROOT / 'build' / ('sns-' + str(time.time_ns()))
    directory.mkdir()
    prior, _ = current_build()
    for name in ('queue-real-profile.h', 'queue-fixture-profile.h', 'runtime_pins.h'):
        shutil.copyfile(prior / name, directory / name)
    entries = []
    for rva in (0x4c478f0, 0x4cf6970, 0x4cf7f20, 0x4cfa2c0, 0x4cfb300, 0x4cfb520,
                0x2c17030, 0x4c79580, 0x4c795f0, 0x4c78790, 0x4c75ef0,
                0x4c47260, 0x5c72a0, 0x4d30a50, 0x4d31ab0, 0x4c5e3d0, 0x4c5e620, 0x5c8b10):
        raw = pe.data[pe.rva_to_offset(rva):][:16]
        entries.append(' {0x%xu,{%s}},' % (rva, ','.join('0x%02x' % b for b in raw)))
    (directory / 'sns-profile.h').write_text('static const struct profile_entry sns_functions[]={\n' +
                                            '\n'.join(entries) + '\n};\n')
    vendor = ROOT / 'vendor/minhook'
    sources = [ROOT / 'native/sns_sidecar.c', ROOT / 'native/stream_snapshot.c'] + [
        vendor / p for p in ('src/buffer.c', 'src/hook.c', 'src/trampoline.c', 'src/hde/hde64.c')]
    name = 'client-hook-sns.dll'
    command = [executable_setting('RE_WECHAT_GCC', 'gcc'), '-std=c11', '-O2', '-Wall', '-Wextra',
               '-Werror=format', '-static-libgcc', '-D_WIN32_WINNT=0x0601', '-shared',
               '-I', str(directory), '-I', str(ROOT / 'native'), '-I', str(vendor / 'include'),
               *map(str, sources), '-o', str(directory / name), '-lbcrypt', '-lpsapi']
    result = subprocess.run(command, capture_output=True, creationflags=subprocess.CREATE_NO_WINDOW)
    (directory / 'compiler.log').write_bytes(result.stdout + result.stderr)
    if result.returncode:
        raise RuntimeError((result.stdout + result.stderr).decode(errors='replace'))
    for source in ('test_sns_abi.c', 'test_sns_comment_source.c', 'test_sns_publish_source.c', 'test_sns_publish_xml.c'):
        test = directory / (Path(source).stem + '.exe')
        subprocess.run([executable_setting('RE_WECHAT_GCC', 'gcc'), '-std=c11', '-Wall', '-Wextra',
                        '-static-libgcc', str(ROOT / 'native' / source), '-o', str(test)], check=True)
        subprocess.run([str(test)], check=True, timeout=5)
    pins = {'directory': str(directory), 'dll_name': name,
            'dll_sha256': hashlib.sha256((directory / name).read_bytes()).hexdigest(),
            'core_sha256': SHA, 'abi_fixture_passed': True, 'rpc_version': 3,
            'supported_actions': ['unlike', 'like', 'comment', 'publish'],
            **{'live_' + action + '_verified': False for action in ('unlike', 'like', 'comment', 'publish')}}
    (directory / 'pins.json').write_text(json.dumps(pins, indent=2))
    # 验证与正式操作均绑定此独立构建，不覆盖已加载 DLL。
    (ROOT / 'build/sns-current.json').write_text(json.dumps(pins, indent=2))
    print(json.dumps({'built': True, 'abi_fixture_passed': True, 'directory': str(directory)}))


if __name__ == '__main__':
    main()
