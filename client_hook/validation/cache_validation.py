"""仅供操作员进行真实未缓存资源验证的可恢复缓存隔离。

生产 MCP 或 Codex 服务从不导入此模块。Hook 负责其数据库和原生文件布局；
收发栈验证器仅在测试准备阶段使用此辅助模块。
"""

from pathlib import Path
import sys

MODULE_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(MODULE_DIR.parent))
sys.path.insert(0, str(MODULE_DIR))

from contextlib import contextmanager
import hashlib
import json
import sqlite3
import threading
from types import SimpleNamespace
import uuid

from api.errors import ApiError
from api.native_assets import NativeAssets, account_directory

ROOT = Path(__file__).resolve().parents[1]


def _save(path, value):
    temporary = path.with_suffix('.tmp')
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding='utf-8')
    temporary.replace(path)


@contextmanager
def isolated_native_cache(targets):
    db_path = ROOT / 'state/mcp-live-validation/messages.sqlite3'
    db = sqlite3.connect(db_path.as_uri() + '?mode=ro', uri=True)
    db.row_factory = sqlite3.Row
    journal = ROOT / 'state/native-download-validation' / ('restore-' + uuid.uuid4().hex + '.json')
    journal.parent.mkdir(exist_ok=True)
    moves, results = [], []
    try:
        for event in targets:
            assets = NativeAssets(SimpleNamespace(db=db, condition=threading.RLock()), event['account'])
            paths = []
            try:
                assets.fetch(event, allow_download=False, cached_paths=paths)
            except ApiError as error:
                if error.code not in {'native_download_required', 'native_image_decryption_failed'}:
                    raise
            account_root = account_directory(event['account']).resolve()
            backup_root = account_root / 'msg/temp/client_hook_validation_backup' / journal.stem
            for source in paths:
                source = source.resolve(strict=True)
                if not source.is_relative_to(account_root) or source.is_symlink() or not source.is_file():
                    raise ValueError('缓存隔离源路径不符合当前账号边界')
                backup_root.mkdir(parents=True, exist_ok=True)
                backup = backup_root / hashlib.sha256(str(source).encode()).hexdigest()
                if not backup.resolve().is_relative_to(backup_root.resolve()) or backup.exists():
                    raise ValueError('缓存隔离目标路径无效')
                moves.append({'source': str(source), 'backup': str(backup), 'account_root': str(account_root),
                              'sha256': hashlib.sha256(source.read_bytes()).hexdigest()})
                _save(journal, moves)
                source.replace(backup)
            try:
                assets.fetch(event, allow_download=False)
            except ApiError:
                results.append({'event_id': event['event_id'], 'cached_before': bool(paths),
                                'isolated_exact_content_files': len(paths), 'cache_missing_before_acquire': True})
            else:
                raise ValueError('未能建立资源缓存缺失的对照条件')
        yield results
    finally:
        try:
            for item in reversed(moves):
                source, backup, account_root = (Path(item[k]) for k in ('source', 'backup', 'account_root'))
                if not source.resolve().is_relative_to(account_root.resolve()) or not backup.resolve().is_relative_to(
                        account_root / 'msg/temp/client_hook_validation_backup'):
                    raise ValueError('恢复路径超出已校验账号目录')
                if backup.exists():
                    if hashlib.sha256(backup.read_bytes()).hexdigest() != item['sha256']:
                        raise ValueError('缓存备份摘要已改变，保留恢复清单')
                    if source.exists():
                        if hashlib.sha256(source.read_bytes()).hexdigest() != item['sha256']:
                            raise ValueError('微信已写入不同缓存，保留原件及恢复清单')
                        backup.unlink()
                    else:
                        backup.replace(source)
            journal.unlink(missing_ok=True)
        finally:
            db.close()
