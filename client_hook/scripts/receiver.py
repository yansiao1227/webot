"""兼容入口和可复用的驻留接收命令行接口。"""

from pathlib import Path
import sys

MODULE_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(MODULE_DIR.parent))
sys.path.insert(0, str(MODULE_DIR))

from hook_runtime.loader import ROOT, Win, normalized, PE, export_rva
from hook_runtime.service import Config, main

if __name__ == "__main__":
    raise SystemExit(main())
