"""仓库 .env 配置，不执行 shell 表达式，也不依赖第三方库。"""
import ipaddress
import os
from pathlib import Path
import re
import tempfile

ROOT = Path(__file__).resolve().parents[1]


def load_env(path=None, environ=None):
    """已有进程环境变量优先；相对 .env 路径以仓库根目录为基准。

    支持 KEY=value、可选的 export 或引号，以及 ${VAR}。Windows 反斜杠
    保持字面含义，不执行命令、Python 转义或可执行表达式。
    """
    env = os.environ if environ is None else environ
    path = Path(path or env.get('RE_WECHAT_ENV_FILE', ROOT / '.env'))
    if not path.is_absolute():
        path = ROOT / path
    if not path.exists():
        return
    values = {}
    for number, line in enumerate(path.read_text(encoding='utf-8-sig').splitlines(), 1):
        line = line.strip()
        if not line or line.startswith('#'):
            continue
        if line.startswith('export '):
            line = line[7:].lstrip()
        key, separator, value = line.partition('=')
        key, value = key.strip(), value.strip()
        if not separator or not re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]*', key):
            raise ValueError(f'.env 第{number}行格式无效')
        if value.startswith(('"', "'")):
            quote = value[0]
            end = value.find(quote, 1)
            if end < 0 or (value[end + 1:].strip() and not value[end + 1:].lstrip().startswith('#')):
                raise ValueError(f'.env 第{number}行引号无效')
            value = value[1:end]
        else:
            value = re.split(r'\s+#', value, maxsplit=1)[0].rstrip()
        values[key] = value

    def resolve(key, chain=()):
        if key in env:
            return env[key]
        if key in chain:
            raise ValueError('.env 变量存在循环引用')
        if key not in values:
            raise ValueError('.env 引用了未定义的变量：' + key)
        return re.sub(r'\$\{([A-Za-z_][A-Za-z0-9_]*)\}',
                      lambda match: resolve(match[1], (*chain, key)), values[key])
    resolved = {key: resolve(key) for key in values if key not in env}
    env.update(resolved)


load_env()
os.environ.setdefault('RE_WECHAT_ROOT', str(ROOT))


def path_setting(name, default):
    value = Path(os.path.expanduser(os.environ.get(name) or str(default)))
    return (value if value.is_absolute() else ROOT / value).resolve()


def port_setting(name, default):
    value = int(os.environ.get(name) or default)
    if not 1 <= value <= 65535:
        raise ValueError(name + ' 必须为1至65535的端口')
    return value


def executable_setting(name, default):
    return str(path_setting(name, default)) if os.environ.get(name) else default


def local_host():
    host = os.environ.get('RE_WECHAT_HOST') or '127.0.0.1'
    # 保留现有回环地址边界，修改 .env 不得向外暴露微信服务。
    if host != 'localhost' and not ipaddress.ip_address(host).is_loopback:
        raise ValueError('RE_WECHAT_HOST 必须为回环地址')
    if ':' in host:
        raise ValueError('当前HTTP服务仅支持IPv4回环地址或localhost')
    return host


def hook_port():
    return port_setting('RE_WECHAT_HOOK_PORT', 30001)


def mcp_port():
    return port_setting('RE_WECHAT_MCP_PORT', 30002)


def hook_url():
    return f'http://{local_host()}:{hook_port()}'


def mcp_url():
    return f'http://{local_host()}:{mcp_port()}/mcp'


def cache_directory(prefix):
    root = path_setting('CONTENT_CACHE_DIR', ROOT / 'tool/state/cache')
    root.mkdir(parents=True, exist_ok=True)
    return Path(tempfile.mkdtemp(prefix=prefix, dir=root))


if __name__ == '__main__':
    import argparse
    parser = argparse.ArgumentParser(description='仅解析本机非密钥路径配置，供PowerShell入口使用')
    parser.add_argument('--get', required=True, choices=(
        'RE_WECHAT_PYTHON', 'RE_WECHAT_GCC', 'RE_WECHAT_OBJDUMP', 'RE_WECHAT_NM', 'RE_WECHAT_WEIXIN_DLL'))
    args = parser.parse_args()
    defaults = {'RE_WECHAT_PYTHON': 'wechat_codex/state/sdk-env/Scripts/python.exe',
                'RE_WECHAT_GCC': 'gcc', 'RE_WECHAT_OBJDUMP': 'objdump', 'RE_WECHAT_NM': 'nm'}
    if args.get == 'RE_WECHAT_PYTHON':
        print(path_setting(args.get, defaults[args.get]))
    else:
        if args.get not in defaults and not os.environ.get(args.get):
            parser.error('请在.env中配置' + args.get)
        print(executable_setting(args.get, defaults.get(args.get, '')))
