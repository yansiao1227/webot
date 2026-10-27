"""运行本期测试并写入可复核报告；只操作自有 host，不附加真实微信。"""
import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import subprocess
import sys
import unittest


def main():
    module = Path(__file__).resolve().parent
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--with-lifecycle', action='store_true')
    parser.add_argument('--report', type=Path, default=module / 'build/test-results.json')
    args = parser.parse_args()
    suite = unittest.defaultTestLoader.discover(str(module / 'tests'))
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    lifecycle = []
    if args.with_lifecycle:
        for script in ('test_runtime.py', 'test_resident.py'):
            process = subprocess.run([sys.executable, '-B', '-X', 'utf8', str(module / 'tests/lifecycle' / script)],
                                     capture_output=True, text=True, encoding='utf-8', timeout=90)
            lifecycle.append({'script': script, 'passed': process.returncode == 0,
                              'stdout': process.stdout.strip(), 'stderr': process.stderr.strip()})
    from hook_runtime.service import WORK
    pin_file = WORK / 'runtime-current.json'
    report = {'date_utc': datetime.now(timezone.utc).isoformat(),
              'passed': result.wasSuccessful() and all(case['passed'] for case in lifecycle),
              'test_count': result.testsRun, 'failures': len(result.failures), 'errors': len(result.errors),
              'lifecycle': lifecycle, 'scope': 'owned_host_and_local_http_only_no_live_weixin_calls',
              'live_verified': False,
              'build': json.loads(pin_file.read_text(encoding='utf-8-sig')) if pin_file.exists() else None}
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    print(json.dumps({'passed': report['passed'], 'tests': result.testsRun, 'report': str(args.report)}))
    return 0 if report['passed'] else 1


if __name__ == '__main__':
    sys.exit(main())
