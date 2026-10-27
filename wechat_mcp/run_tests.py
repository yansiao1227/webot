"""运行无网络外部依赖的回归并保存结果；所有网络仅本机模拟 HTTP。"""
import json
from pathlib import Path
import time
import unittest

ROOT = Path(__file__).resolve().parent


def main():
    started = time.time()
    suite = unittest.defaultTestLoader.discover(str(ROOT / 'tests'))
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    report = {'passed': result.wasSuccessful(), 'tests': result.testsRun, 'failures': len(result.failures),
              'errors': len(result.errors), 'skipped': len(result.skipped), 'seconds': round(time.time()-started, 3),
              'native_sends': 0, 'dot_verified': False}
    (ROOT / 'state').mkdir(exist_ok=True)
    (ROOT / 'state/test-results.json').write_text(json.dumps(report, indent=2), encoding='utf-8')
    raise SystemExit(0 if result.wasSuccessful() else 1)


if __name__ == '__main__':
    main()
