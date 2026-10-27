"""本地微信调度器及可恢复的 Codex 会话。"""
from pathlib import Path
import sys

_repository = str(Path(__file__).resolve().parents[2])
if _repository not in sys.path:
    sys.path.insert(0, _repository)
