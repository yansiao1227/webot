"""独立的语音转文本 JSON 命令行工具，不导入应用模块。"""
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from tool.content_reading.__main__ import main


if __name__ == '__main__':
    raise SystemExit(main(['transcribe-audio', *sys.argv[1:]]))
