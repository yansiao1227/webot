"""项目移动目录后校验并修正构建记录；不加载 DLL，不修改运行注册或消息数据。"""
import argparse
import hashlib
import json
from pathlib import Path
import sys
import uuid

MODULE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(MODULE))
from hook_runtime.service import WORK, atomic_json


def relocation_plan(root):
    root = Path(root).resolve(strict=True)
    changes = []
    for kind in ('runtime', 'resource', 'read'):
        current = root / (kind + '-current.json')
        if not current.exists():
            continue
        selected = json.loads(current.read_text(encoding='utf-8-sig'))
        original = Path(selected['directory'])
        directory = (root / original.name).resolve(strict=True)
        if directory.parent != root or not directory.name.startswith(kind + '-'):
            raise ValueError('构建目录不在指定产物根目录内')
        manifest = directory / 'pins.json'
        pinned = json.loads(manifest.read_text(encoding='utf-8-sig'))
        if selected != pinned:
            raise ValueError('当前构建记录与目录内 pins.json 不一致')
        if original.resolve() != directory and original.exists():
            raise ValueError('旧构建目录仍存在，不能自动选择迁移副本')
        files = pinned['files'] if kind == 'runtime' else {pinned['dll_name']: pinned['dll_sha256']}
        if not files:
            raise ValueError('构建记录缺少文件摘要')
        for name, digest in files.items():
            artifact = (directory / name).resolve(strict=True)
            if artifact.parent != directory or Path(name).name != name:
                raise ValueError('构建文件越出目录边界')
            if hashlib.sha256(artifact.read_bytes()).hexdigest() != digest:
                raise ValueError('构建文件摘要不符，未修改任何路径')
        if original.resolve() != directory:
            updated = {**pinned, 'directory': str(directory)}
            changes.extend((path, updated) for path in (manifest, current))
    return changes


def relocate(root, apply=False):
    # Validate every selected build before changing any manifest.
    changes = relocation_plan(root)
    if apply and changes:
        suffix = '.before-relocation-' + uuid.uuid4().hex + '.json'
        for path, _ in changes:
            with path.with_name(path.name + suffix).open('xb') as backup:
                backup.write(path.read_bytes())
        for path, updated in changes:
            atomic_json(path, updated)
    return {'applied': apply, 'manifest_count': len(changes), 'native_loads': 0}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--build-root', type=Path, default=WORK)
    parser.add_argument('--apply', action='store_true', help='校验全部选中构建后备份并修改路径；默认只检查')
    args = parser.parse_args()
    try:
        print(json.dumps(relocate(args.build_root, args.apply), ensure_ascii=False))
    except (OSError, ValueError, KeyError) as error:
        print(json.dumps({'error': type(error).__name__, 'message': '迁移检查失败，未完成路径修正；请核对构建记录及文件摘要。'}, ensure_ascii=False))
        raise SystemExit(1)


if __name__ == '__main__':
    main()
