"""读取单个公开链接；公众号及 --browser 使用已配置的浏览器配置。"""
import argparse
import json
from pathlib import Path
import sys
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from local_agent.web_content import retrieve
from tool.settings import path_setting


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('url')
    parser.add_argument('--browser', action='store_true')
    parser.add_argument('--state-dir', type=Path, default=path_setting('RE_WECHAT_CODEX_STATE_DIR', ROOT / 'state/live'))
    args = parser.parse_args()
    options = {}
    card = {'source_urls': [args.url]}
    if args.browser or urlsplit(args.url).hostname in (
            'mp.weixin.qq.com', 'b23.tv', 'www.bilibili.com', 'm.bilibili.com', 'bilibili.com'):
        from local_agent.runtime import read_config
        config = read_config(args.state_dir)
        options = {'profile_dir': config.get('browser_profile_dir', str(args.state_dir.resolve() / 'browser-profile')),
                   'channel': config.get('browser_channel', 'msedge')}
        card['card_kind'] = 'app-message'
    print(json.dumps(retrieve(card, options), ensure_ascii=False))


if __name__ == '__main__':
    main()
