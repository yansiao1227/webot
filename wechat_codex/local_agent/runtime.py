import json
import os
from pathlib import Path
import time
from tool.settings import path_setting, mcp_url


class StateLock:
    def __init__(self, directory):
        self.file = (Path(directory) / 'agent.lock').open('a+b')
        if self.file.tell() == 0:
            self.file.write(b'0'); self.file.flush()
        self.file.seek(0)
        try:
            if os.name == 'nt':
                import msvcrt
                msvcrt.locking(self.file.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BaseException:
            self.file.close()
            raise RuntimeError('同一agent状态目录已运行') from None

    def close(self):
        self.file.close()


def atomic_json(path, value):
    path = Path(path)
    temporary = path.with_suffix(path.suffix + '.tmp')
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding='utf-8')
    try:
        for attempt in range(10):
            try:
                temporary.replace(path)
                break
            except PermissionError:
                # Windows readers can briefly deny rename/delete sharing.
                if attempt == 9:
                    raise
                time.sleep(.02)
    finally:
        temporary.unlink(missing_ok=True)


def read_config(directory):
    config = json.loads((Path(directory) / 'config.json').read_text(encoding='utf-8'))
    config.setdefault('mcp_url', mcp_url())
    if any(os.environ.get(key) for key in ('RE_WECHAT_HOST', 'RE_WECHAT_MCP_PORT')):
        config['mcp_url'] = mcp_url()
    for name, key in (('codex_bin', 'RE_WECHAT_CODEX_BIN'),
                      ('browser_profile_dir', 'CONTENT_BROWSER_PROFILE_DIR')):
        if os.environ.get(key):
            config[name] = str(path_setting(key, ''))
    if os.environ.get('CONTENT_BROWSER_CHANNEL'):
        config['browser_channel'] = os.environ['CONTENT_BROWSER_CHANNEL']
    if 'codex_bin' in config and (not isinstance(config['codex_bin'],str) or
            not Path(config['codex_bin']).is_absolute() or not Path(config['codex_bin']).is_file()):
        raise ValueError('codex_bin 必须为存在的绝对可执行文件路径')
    if (not isinstance(config.get('account'), str) or not config['account'] or
            not isinstance(config.get('conversations'), list) or not config['conversations'] or
            any(not isinstance(x, str) or not x for x in config['conversations']) or
            not 1 <= config.get('max_concurrency', 2) <= 4):
        raise ValueError('配置不完整或并发数超限')
    limits = config.get('model_timeouts', {})
    text_limit = config.get('text_model_timeout', 30)
    for key, default in (('decision_timeout',30),('tool_timeout',180)):
        value=config.get(key,default)
        if isinstance(value,bool) or not isinstance(value,(int,float)) or not .05<=value<=600:
            raise ValueError(key+' 必须为0.05至600秒')
    if not isinstance(config.get('mark_read_on_process', True), bool):
        raise ValueError('mark_read_on_process 必须为布尔值')
    if not isinstance(config.get('adaptive_context', True), bool):
        raise ValueError('adaptive_context 必须为布尔值')
    if config.get('browser_channel', 'msedge') not in ('msedge', 'chrome', 'chromium'):
        raise ValueError('browser_channel 必须为msedge、chrome或chromium')
    if 'browser_profile_dir' in config and (not isinstance(config['browser_profile_dir'], str) or
            not Path(config['browser_profile_dir']).is_absolute()):
        raise ValueError('browser_profile_dir 必须为绝对目录')
    if (isinstance(text_limit, bool) or not isinstance(text_limit, (int, float)) or
            not .05 <= text_limit <= 600):
        raise ValueError('普通文字处理时限必须为0.05至600秒')
    if (not isinstance(limits, dict) or any(cid not in config['conversations'] or
            isinstance(value, bool) or not isinstance(value,(int,float)) or not .05 <= value <= 600
            for cid,value in limits.items())):
        raise ValueError('会话处理时限必须属于白名单会话，且为0.05至600秒')
    return config


def identity(config):
    return {k: config[k] for k in ('account', 'mcp_url', 'consumer_id')}
