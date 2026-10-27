"""本地 Codex 回复代理：configure / run / status / stop / reset。"""
import argparse
import asyncio
import getpass
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import time

from local_agent.runtime import StateLock, atomic_json, read_config, identity
from local_agent.storage import Store
from tool.settings import mcp_url, path_setting

ROOT = Path(__file__).resolve().parents[1]


async def serve(directory, config):
    from local_agent.mcp import MCP
    from local_agent.codex_backend import CodexBackend
    from local_agent.service import Agent
    from local_agent.vault import unseal
    token = os.getenv('MCP_BRIDGE_TOKEN')
    if not token:
        token = unseal(json.loads((directory / 'credentials.json').read_text(encoding='utf-8'))['mcp_token'])
    lock = StateLock(directory)
    store = Store(directory, identity(config))
    backend = CodexBackend(directory / 'workspace', config.get('model'), config.get('model_timeout', 180),
                           codex_bin=config.get('codex_bin'))
    agent = Agent(store, MCP(config['mcp_url'], token), backend, config)
    stop_file = directory / 'stop.request'
    stop_file.unlink(missing_ok=True)
    started = time.time()
    async def heartbeat():
        while not agent.stop.is_set():
            try:
                atomic_json(directory / 'process.json', {'pid': os.getpid(), 'running': True, 'ready': agent.ready.is_set(),
                    'started_at': started, 'heartbeat': time.time(), 'errors': agent.errors,
                    'active_conversations': list(agent.active), **store.status()})
            except Exception as error:
                agent.errors['heartbeat'] = type(error).__name__
                agent.stop.set()  # 控制或停止监视器退出时，不能静默忽略。
                return
            if stop_file.exists():
                agent.stop.set()
                break
            await asyncio.sleep(.5)
    monitor = asyncio.create_task(heartbeat())
    try:
        await agent.run()
    finally:
        agent.stop.set()
        monitor.cancel()
        await asyncio.gather(monitor, return_exceptions=True)
        atomic_json(directory / 'process.json', {'pid': os.getpid(), 'running': False, 'ready': False,
            'heartbeat': time.time(), 'started_at': started, 'errors': agent.errors, **store.status()})
        store.close(); lock.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--state-dir', type=Path, default=path_setting('RE_WECHAT_CODEX_STATE_DIR', ROOT / 'state/live'))
    sub = parser.add_subparsers(dest='command', required=True)
    from local_agent.schedule_cli import add_parser
    add_parser(sub)
    configure = sub.add_parser('configure')
    configure.add_argument('--account', required=True)
    configure.add_argument('--conversation', action='append', required=True)
    configure.add_argument('--mcp-url', default=mcp_url())
    configure.add_argument('--consumer-id', default='codex-local-v1')
    configure.add_argument('--model')
    sub.add_parser('run'); sub.add_parser('start'); sub.add_parser('status'); sub.add_parser('stop')
    sub.add_parser('check-dependencies')
    reset = sub.add_parser('reset'); reset.add_argument('--conversation', required=True)
    args = parser.parse_args()
    directory = args.state_dir.resolve()
    directory.mkdir(parents=True, exist_ok=True)
    if args.command == 'schedules':
        from local_agent.schedule_cli import execute
        result = execute(directory, args)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        if result.get('ok') is False:
            raise SystemExit(1)
    elif args.command == 'check-dependencies':
        from local_agent.media import dependencies
        result = dependencies()
        print(json.dumps({'ready': all(result.values()), 'media': result}, ensure_ascii=False, indent=2))
        if not all(result.values()):
            raise SystemExit(1)
    elif args.command == 'configure':
        from local_agent.vault import seal
        token = os.getenv('MCP_BRIDGE_TOKEN') or getpass.getpass('MCP bearer（不回显）: ')
        if len(token) < 24:
            raise ValueError('MCP bearer至少24字符')
        config = {'account': args.account, 'conversations': sorted(set(args.conversation)),
                  'mcp_url': args.mcp_url, 'consumer_id': args.consumer_id, 'model': args.model,
                  'max_concurrency': 2, 'model_timeout': 180, 'text_model_timeout': 30,
                  'decision_timeout': 30, 'tool_timeout': 180,
                  'startup_timeout': 60, 'generation_idle_timeout': 30, 'turn_timeout': 600,
                  'adaptive_context': True, 'mark_read_on_process': True, 'simulation': False}
        atomic_json(directory / 'credentials.json', {'mcp_token': seal(token)})
        atomic_json(directory / 'config.json', config)
        print(json.dumps({'configured': True, 'credentials': 'Windows DPAPI'}, ensure_ascii=False))
    elif args.command == 'status':
        path = directory / 'process.json'
        value = json.loads(path.read_text(encoding='utf-8')) if path.exists() else {'running': False}
        value['heartbeat_fresh'] = time.time() - value.get('heartbeat', 0) < 5
        print(json.dumps(value, ensure_ascii=False, indent=2))
    elif args.command == 'start':
        path = directory / 'process.json'
        prior = json.loads(path.read_text(encoding='utf-8')) if path.exists() else {}
        if prior.get('running') and time.time() - prior.get('heartbeat', 0) < 5:
            print(json.dumps({'already_running': True, 'pid': prior['pid'], 'ready': prior.get('ready')}))
            return
        with (directory / 'agent.log').open('ab') as log:
            process = subprocess.Popen([sys.executable, '-B', '-X', 'utf8', str(ROOT / 'agent.py'),
                '--state-dir', str(directory), 'run'], stdin=subprocess.DEVNULL, stdout=log, stderr=log,
                creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0)
        until = time.monotonic() + 25
        while time.monotonic() < until:
            if process.poll() is not None:
                raise RuntimeError('后台进程启动失败')
            if path.exists():
                current = json.loads(path.read_text(encoding='utf-8'))
                if current.get('running') and current.get('ready') and time.time() - current.get('heartbeat', 0) < 3:
                    print(json.dumps({'started': True, 'ready': True, 'pid': current['pid']}))
                    return
            time.sleep(.2)
        print(json.dumps({'started': True, 'ready': False, 'message': '后台仍在启动，请查询status'}))
    elif args.command == 'stop':
        (directory / 'stop.request').write_text('stop\n', encoding='ascii')
        print(json.dumps({'stop_requested': True}))
    elif args.command == 'reset':
        store = Store(directory, identity(read_config(directory)))
        try:
            store.reset(args.conversation)
        finally:
            store.close()
        print(json.dumps({'reset': args.conversation, 'old_messages_replayed': False}))
    else:
        asyncio.run(serve(directory, read_config(directory)))


def entrypoint():
    try:
        main()
    except KeyboardInterrupt:
        pass
    except Exception as error:
        # SDK 或提供方异常可能包含请求载荷或凭据。
        print(json.dumps({'error': type(error).__name__, 'message': '操作失败，请核对配置、登录与本机状态。'}, ensure_ascii=False))
        raise SystemExit(1)
