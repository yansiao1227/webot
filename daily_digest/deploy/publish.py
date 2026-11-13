"""固定SSH快照发布；在仓库根目录运行，无密码交互或公网写入接口。"""
import argparse
import json
from pathlib import Path
import sys
ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from daily_digest.deploy.engine import SnapshotDeployer

def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--replace-service', default='daily-digest')
    parser.add_argument('--prepare-only', action='store_true')
    parser.add_argument('--release', help='继续核查/发布原冻结release')
    parser.add_argument('--status', nargs='?', const='', help='只读查询本地或指定release状态')
    parser.add_argument('--rollback', help='指定release的旧代码配合最新快照恢复')
    args = parser.parse_args()
    deployer = SnapshotDeployer.from_environment(replace_service=args.replace_service)
    if args.status is not None:
        result = deployer.status(args.status or None)
    elif args.rollback:
        result = deployer.rollback(args.rollback)
    else:
        release = deployer.load_release(args.release) if args.release else deployer.prepare()
        result = {'state': 'prepared', 'release_id': release.release_id, 'bundle': str(release.bundle)} if args.prepare_only else deployer.deploy(release)
    print(json.dumps(result, ensure_ascii=False))

if __name__ == '__main__':
    try:
        main()
    except Exception as error:
        print(str(error) if isinstance(error, (RuntimeError, ValueError)) else type(error).__name__, file=sys.stderr)
        raise SystemExit(1)
