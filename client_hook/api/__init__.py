"""本地 HTTP 与持久消息消费。仅依赖 Python 标准库。"""

from pathlib import Path
import sys
_repository = str(Path(__file__).resolve().parents[2])
if _repository not in sys.path:
    sys.path.insert(0, _repository)
