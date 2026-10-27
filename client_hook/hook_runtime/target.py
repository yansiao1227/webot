"""通过本模块的 Win32 元数据及固定磁盘指纹选择目标。"""
import hashlib
from pathlib import Path

CORE_SHA256 = '89fc940377a779babbd64c9cd4abe6256663681d92f12802aa2df97a8db18494'


def discover(api, pid=None):
    matches = []
    failures = []
    pids = [pid] if pid else api.named_processes('Weixin.exe')
    for candidate in pids:
        try:
            modules = api.modules(candidate)
            cores = [(path, base, size) for path, base, size in modules if Path(path).name.casefold() == 'weixin.dll']
            exes = [path for path, _, _ in modules if Path(path).name.casefold() == 'weixin.exe']
            if len(cores) != 1 or len(exes) != 1:
                continue
            meta = api.inspect(candidate, Path(exes[0]))
            if meta and hashlib.sha256(Path(cores[0][0]).read_bytes()).hexdigest() == CORE_SHA256:
                matches.append(dict(meta, core_path=cores[0][0], disk_matches_profile=True, unique_target=True))
        except (OSError, RuntimeError) as error:
            failures.append(str(error))
    if len(matches) != 1 or failures:
        raise RuntimeError(f'需要唯一且版本指纹匹配的微信进程；匹配数={len(matches)}，检查错误={failures}')
    return matches[0]
