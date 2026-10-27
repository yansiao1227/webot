"""Observe fixed-build download arguments for five explicitly authorized samples."""
import argparse
import json
from pathlib import Path
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def main():
    raise SystemExit('已禁用：2026-10-24真机发生frida-agent.dll 0xc0000409崩溃，禁止再次附加微信。仅保留脚本供离线审查。')
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--seconds', type=int, default=600)
    parser.add_argument('--paths-only', action='store_true')
    args = parser.parse_args()
    from hook_runtime.loader import Win
    from hook_runtime.target import discover
    import frida
    api = Win()
    target = discover(api)
    from hook_runtime.pe_local import PE
    artifact = PE(target['core_path'])
    base = next(base for path, base, _ in api.modules(target['pid'])
                if Path(path).name.lower() == 'weixin.dll')
    process = api.api.OpenProcess(0x1010, False, target['pid'])
    if not process:
        raise RuntimeError('cannot read target')
    try:
        for rva in ((0x9bea70,) if args.paths_only else (0x9da310, 0x16c84d0)):
            at = artifact.rva_to_offset(rva)
            if api.read(process, base + rva, 16) != artifact.data[at:at + 16]:
                raise RuntimeError('observer entry already changed; do not overlap hooks')
    finally:
        api.api.CloseHandle(process)
    output = ROOT / 'state/mcp-live-validation/native-resource-observation.ndjson'
    with output.open('a', encoding='utf-8') as stream:
        session = frida.attach(target['pid'])
        def message(value, data):
            # Never log JS errors containing intercepted payloads or script source.
            result = value.get('payload') if value.get('type') == 'send' else {'type': 'observer_error'}
            result = {'observed_at': time.time(), **result}
            stream.write(json.dumps(result, ensure_ascii=False) + '\n'); stream.flush()
            print(json.dumps(result, ensure_ascii=False), flush=True)
        source = (ROOT / 'analysis/observe_resources.js').read_text(encoding='utf-8')
        if args.paths_only:
            source = source.split('Interceptor.attach(base.add(0x9da310)')[0]
            source += (ROOT / 'analysis/observe_resource_paths.js').read_text(encoding='utf-8')
        script = session.create_script(source)
        script.on('message', message)
        try:
            script.load()
            until = time.monotonic() + args.seconds
            while time.monotonic() < until:
                time.sleep(.5)
        finally:
            script.unload()
            session.detach()


if __name__ == '__main__':
    main()
