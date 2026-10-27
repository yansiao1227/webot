import json
from pathlib import Path
import sys
import unittest

root = Path(__file__).resolve().parent
sys.path.insert(0, str(root))
suite = unittest.defaultTestLoader.discover(str(root / 'tests'))
result = unittest.TextTestRunner(verbosity=2).run(suite)
directory = root / 'state'
directory.mkdir(exist_ok=True)
(directory / 'test-results.json').write_text(json.dumps({'passed': result.wasSuccessful(),
    'tests': result.testsRun, 'scope': 'owned_fakes_no_wechat_or_model_calls'}), encoding='utf-8')
raise SystemExit(0 if result.wasSuccessful() else 1)
