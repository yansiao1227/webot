"""将本机模块位置注入复制的构建配置，不加载模块。"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from tool.settings import executable_setting


def main():
    target = Path(sys.argv[1])
    lines = target.read_text(encoding='utf-8').splitlines()
    if not lines or not lines[0].startswith('#define QUEUE_REAL_MODULE_PATH '):
        raise ValueError('不是预期的原生profile')
    lines[0] = '#define QUEUE_REAL_MODULE_PATH L' + json.dumps(
        executable_setting('RE_WECHAT_WEIXIN_DLL', 'Weixin.dll'), ensure_ascii=False)
    target.write_text('\n'.join(lines) + '\n', encoding='utf-8')


if __name__ == '__main__':
    main()
