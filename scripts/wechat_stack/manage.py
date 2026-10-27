"""本机微信收发栈管理；不启动 Dot、不操作微信 UI、不发送测试消息。"""
import argparse
import ctypes
import http.client
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[2]
STATE = Path(__file__).resolve().parent / 'state'
sys.path.insert(0, str(ROOT / 'wechat_codex'))
from local_agent.runtime import StateLock, read_config
from local_agent.vault import unseal
from tool.settings import path_setting, local_host, hook_port, mcp_port, mcp_url

SPECS = {
    'client_hook': (ROOT / 'client_hook/client_hook.py', path_setting('RE_WECHAT_HOOK_STATE_DIR', 'client_hook/state/mcp-live-validation'), hook_port()),
    'mcp': (ROOT / 'wechat_mcp/bridge.py', path_setting('RE_WECHAT_MCP_STATE_DIR', 'wechat_mcp/state/live'), mcp_port()),
    'codex': (ROOT / 'wechat_codex/agent.py', path_setting('RE_WECHAT_CODEX_STATE_DIR', 'wechat_codex/state/live'), None),
}
PYTHON = path_setting('RE_WECHAT_PYTHON', 'wechat_codex/state/sdk-env/Scripts/python.exe')


def read(path):
    return json.loads(Path(path).read_text(encoding='utf-8-sig'))


def say(message):
    print(message, flush=True)


def argv(command):
    from ctypes import wintypes
    count = ctypes.c_int()
    function = ctypes.windll.shell32.CommandLineToArgvW
    function.argtypes = [wintypes.LPCWSTR, ctypes.POINTER(ctypes.c_int)]
    function.restype = ctypes.POINTER(wintypes.LPWSTR)
    pointer = function(command, ctypes.byref(count))
    if not pointer:
        return []
    try:
        return [pointer[i] for i in range(count.value)]
    finally:
        ctypes.windll.kernel32.LocalFree.argtypes = [ctypes.c_void_p]
        ctypes.windll.kernel32.LocalFree(pointer)


def normalized(value):
    return os.path.normcase(os.path.abspath(value))


def matching(command, name):
    args = argv(command)
    entry, directory, _ = SPECS[name]
    paths = [normalized(a) for a in args]
    action = 'run' if name == 'codex' else 'serve'
    if normalized(entry) not in paths or '--state-dir' not in args or action not in args:
        return False
    i = args.index('--state-dir') + 1
    return i < len(args) and normalized(args[i]) == normalized(directory)


def processes(name):
    # Never print command lines: another process could have a token in its args.
    script = "[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false)\nGet-CimInstance Win32_Process | Where-Object { $_.Name -match '^python(w)?(3([.][0-9]+)?)?[.]exe$' } | Select-Object ProcessId,CommandLine | ConvertTo-Json -Compress"
    result = subprocess.run(['powershell.exe', '-NoProfile', '-NonInteractive', '-Command', script],
        capture_output=True, timeout=20, creationflags=subprocess.CREATE_NO_WINDOW)
    if result.returncode:
        raise RuntimeError('无法查询进程，已取消管理操作')
    value = json.loads(result.stdout.decode('utf-8-sig') or '[]')
    if isinstance(value, dict):
        value = [value]
    return [p for p in value if matching(p.get('CommandLine') or '', name)]


def occupied(port):
    if port is None:
        return False
    with socket.socket() as connection:
        connection.settimeout(.3)
        return connection.connect_ex((local_host(), port)) == 0


def request(port, path, token):
    connection = http.client.HTTPConnection(local_host(), port, timeout=2)
    try:
        connection.request('GET', path, headers={'Authorization': 'Bearer ' + token})
        response = connection.getresponse()
        if response.status != 200:
            raise RuntimeError('服务认证或健康检查失败')
        return json.loads(response.read(1048576))
    finally:
        connection.close()


def configuration():
    config = read_config(SPECS['codex'][1])
    hook = read(SPECS['mcp'][1] / 'config.json')
    credentials = read(SPECS['mcp'][1] / 'credentials.json')
    if (config['account'] != hook['account'] or
            set(config['conversations']) != set(hook['conversations']) or
            config.get('simulation') is not False or
            config['mcp_url'] != mcp_url()):
        raise RuntimeError('现有账号、白名单或本机地址配置不一致，拒绝启动')
    token = unseal(credentials['mcp_token'])
    if unseal(read(SPECS['codex'][1] / 'credentials.json')['mcp_token']) != token:
        raise RuntimeError('Codex 与 MCP 的凭据不一致，拒绝启动')
    return config, unseal(credentials['client_hook_token']), token


def healthy(name, config, hook_token, mcp_token):
    if name == 'client_hook':
        value = request(hook_port(), '/client_hook/status', hook_token)
        capture = value.get('capture') or {}
        return (value.get('backend') == 'native' and capture.get('account') == config['account'] and
            capture.get('state') == 'running' and not value.get('background_error'))
    if name == 'mcp':
        value = request(mcp_port(), '/health', mcp_token)
        return (value.get('simulation') is False and value.get('account') == config['account'] and
            set(value.get('allowed_conversations', [])) == set(config['conversations']) and
            value.get('subscriptions_active') == 0 and not value.get('worker_errors') and
            value.get('group_reply_policy', {}).get('require_explicit_self_mention') is True and
            value.get('source', {}).get('state') == 'running')
    value = read(SPECS[name][1] / 'process.json')
    return (value.get('running') and value.get('ready') and time.time() - value.get('heartbeat', 0) < 5
        and not value.get('errors') and not value.get('blocked'))


def marker(name):
    return SPECS[name][1] / 'stop.request' if name == 'codex' else STATE / (name + '.stop')


def managed(name, items):
    # Agent has its own stop protocol. Older hook/MCP processes need an explicit
    # restart with --stop-file before this manager may claim it can stop them.
    if name == 'codex':
        return True
    for process in items:
        args = argv(process['CommandLine'])
        if '--stop-file' not in args:
            return False
        i = args.index('--stop-file') + 1
        if i >= len(args) or normalized(args[i]) != normalized(marker(name)):
            return False
    return True


def spawn(name, config, hook_token, mcp_token):
    entry, directory, _ = SPECS[name]
    directory.mkdir(parents=True, exist_ok=True)
    marker(name).unlink(missing_ok=True)
    args = [str(PYTHON), '-B', '-X', 'utf8', str(entry)]
    if name == 'codex':
        args += ['--state-dir', str(directory), 'run']
    else:
        args += ['serve', '--state-dir', str(directory), '--stop-file', str(marker(name))]
        args += ['--host', local_host(), '--port', str(SPECS[name][2])]
    if name == 'mcp':
        args += ['--account', config['account']]
        for conversation in config['conversations']:
            args += ['--allow-conversation', conversation]
    env = os.environ.copy()
    # Do not pass unrelated keys or stale parent credentials to the local stack.
    for key in list(env):
        if any(word in key.upper() for word in ('KEY', 'TOKEN', 'SECRET', 'PASSWORD')):
            env.pop(key)
    if name != 'codex':
        env['CLIENT_HOOK_TOKEN'] = hook_token
    if name == 'mcp':
        env['MCP_BRIDGE_TOKEN'] = mcp_token
    with (STATE / (name + '.log')).open('ab') as log:
        return subprocess.Popen(args, cwd=ROOT, env=env, stdin=subprocess.DEVNULL,
            stdout=log, stderr=log, creationflags=subprocess.CREATE_NO_WINDOW)


def wait_ready(name, config, hook_token, mcp_token, process=None):
    until = time.monotonic() + (50 if name == 'client_hook' else 35)
    while time.monotonic() < until:
        if process is not None and process.poll() is not None:
            raise RuntimeError(name + ' 启动进程已退出，请查看日志')
        try:
            if healthy(name, config, hook_token, mcp_token):
                return
        except (OSError, ValueError, KeyError, RuntimeError):
            pass
        time.sleep(.5)
    raise RuntimeError(name + ' 未就绪，请查看日志；服务可能仍在启动')


def stop_one(name):
    items = processes(name)
    if not items:
        if occupied(SPECS[name][2]):
            raise RuntimeError(name + ' 端口被其他进程占用，未操作该进程')
        say(name + '：已停止')
        return
    if not managed(name, items):
        raise RuntimeError(name + ' 是旧方式启动的服务，请先在原窗口 Ctrl+C 停止，再使用本脚本')
    marker(name).parent.mkdir(parents=True, exist_ok=True)
    marker(name).write_text('stop\n', encoding='ascii')
    say(name + '：已请求正常退出，等待清理……')
    until = time.monotonic() + (220 if name == 'codex' else 65)
    while time.monotonic() < until:
        time.sleep(1)
        if not processes(name):
            if occupied(SPECS[name][2]):
                raise RuntimeError(name + ' 已退出，但端口被其他进程占用')
            say(name + '：停止完成')
            return
    raise RuntimeError(name + ' 退出超时；保留上游服务，请查看日志，不强杀进程')


def start():
    if not PYTHON.is_file():
        raise RuntimeError('未找到 Codex Python 环境，请按 wechat_codex/docs/README.md 安装')
    config, hook_token, mcp_token = configuration()
    # Preflight all ports before loading the DLL or launching anything.
    for name, (_, _, port) in SPECS.items():
        items = processes(name)
        if items and not managed(name, items):
            raise RuntimeError(name + ' 使用旧启动方式，请先停止旧服务后再统一启动')
        if occupied(port) and not items:
            raise RuntimeError(str(port) + ' 被其他服务占用，拒绝启动')
    launched = []
    try:
        for name in SPECS:
            process = None
            if not processes(name):
                say(name + '：后台启动中……')
                process = spawn(name, config, hook_token, mcp_token)
                launched.append(name)
            wait_ready(name, config, hook_token, mcp_token, process)
            say(name + '：就绪')
    except BaseException:
        say('启动未完成，正在回收本次新启动的服务……')
        for name in reversed(launched):
            try:
                stop_one(name)
            except Exception:
                say('回收未完成；保留其上游服务，请执行 status 并查看日志。')
                break
        raise
    say('全部就绪：按已配置白名单自动回复，群聊需真实 @。')


def status():
    try:
        config, hook_token, mcp_token = configuration()
        credentials_ok = True
    except Exception:
        credentials_ok = False
    for name in SPECS:
        items = processes(name)
        if not items:
            state = '端口被其他进程占用' if occupied(SPECS[name][2]) else '已停止'
        else:
            try:
                ok = credentials_ok and healthy(name, config, hook_token, mcp_token)
            except Exception:
                ok = False
            state = ('就绪' if ok else '运行中但未就绪/有错误') + ('；旧启动方式' if not managed(name, items) else '')
        say(name + '：' + state)
    say('日志目录：' + str(STATE))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=['start', 'stop', 'status'])
    args = parser.parse_args()
    if os.name != 'nt':
        raise RuntimeError('本管理脚本仅支持 Windows')
    STATE.mkdir(parents=True, exist_ok=True)
    lock = StateLock(STATE)
    try:
        if args.command == 'start':
            start()
        elif args.command == 'stop':
            for name in reversed(SPECS):
                stop_one(name)
            say('全部停止；微信仍保持打开，消息与会话数据保留。')
        else:
            status()
    finally:
        lock.close()


if __name__ == '__main__':
    try:
        main()
    except KeyboardInterrupt:
        say('管理操作被中断；后台服务可能仍在运行，请执行 status。')
        sys.exit(130)
    except Exception as error:
        # Provider/OS errors may contain secrets: print only our fixed messages.
        say(str(error) if type(error) is RuntimeError else '操作失败（' + type(error).__name__ + '），请检查现有配置与日志。')
        sys.exit(1)
