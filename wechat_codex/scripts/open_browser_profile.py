"""Manually sign into the same persistent browser profile used by card reads."""
import argparse
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from local_agent.runtime import read_config
from tool.settings import path_setting


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--state-dir', type=Path, default=path_setting('RE_WECHAT_CODEX_STATE_DIR', ROOT / 'state/live'))
    args = parser.parse_args()
    from playwright.sync_api import sync_playwright
    config = read_config(args.state_dir)
    directory = config.get('browser_profile_dir', str(args.state_dir.resolve() / 'browser-profile'))
    with sync_playwright() as playwright:
        context = playwright.chromium.launch_persistent_context(
            directory, channel=config.get('browser_channel', 'msedge'), headless=False)
        try:
            page = context.new_page()
            page.goto('about:blank')
            print('请在浏览器中登录需要读取的站点；完成后关闭浏览器窗口，profile会保留。')
            context.wait_for_event('close', timeout=0)
        finally:
            context.close()


if __name__ == '__main__':
    main()
