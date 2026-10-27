from pathlib import Path
import sys

_repository = str(Path(__file__).resolve().parents[2])
if _repository not in sys.path:
    sys.path.insert(0, _repository)

from .sdk import Client, ClientError

__all__ = ['Client', 'ClientError']
