"""固定版本原生运行时、目标校验与 RPC 控制。"""
from pathlib import Path
import sys

_repository = str(Path(__file__).resolve().parents[2])
if _repository not in sys.path:
    sys.path.insert(0, _repository)
