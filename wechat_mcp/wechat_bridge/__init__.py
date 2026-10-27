from pathlib import Path
import sys

_repository = str(Path(__file__).resolve().parents[2])
if _repository not in sys.path:
    sys.path.insert(0, _repository)

"""独立的 client_hook HTTP → MCP 工具和事件桥。"""

VERSION = '0.1.0'
