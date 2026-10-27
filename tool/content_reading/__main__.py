"""JSON 工具命令行入口：python -m tool.content_reading --help。"""
import argparse
import json

from .api import TOOLS, invoke


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('tool', nargs='?', choices=TOOLS)
    parser.add_argument('--list-tools', action='store_true')
    parser.add_argument('--path')
    parser.add_argument('--url')
    parser.add_argument('--kind', choices=('image', 'sticker', 'audio', 'voice', 'video'))
    parser.add_argument('--name', help='文件格式识别使用的名称，默认使用输入文件名')
    parser.add_argument('--output-dir', help='派生图片/音频目录；省略时在CONTENT_CACHE_DIR下创建独立目录')
    parser.add_argument('--model-dir', help='已安装的faster-whisper-small模型目录，不自动下载')
    parser.add_argument('--browser', action='store_true')
    parser.add_argument('--profile-dir')
    parser.add_argument('--channel', default=None)
    parser.add_argument('--public-only', action='store_true', help='可选调用方策略：仅允许公网网页来源')
    args = vars(parser.parse_args(argv))
    if args.pop('list_tools'):
        print(json.dumps(TOOLS, ensure_ascii=False))
        return 0
    tool = args.pop('tool')
    if not tool:
        parser.error('需要工具名或--list-tools')
    result = invoke(tool, **args)
    print(json.dumps(result, ensure_ascii=False))
    return 0 if result['state'] == 'read' else 1


if __name__ == '__main__':
    raise SystemExit(main())
