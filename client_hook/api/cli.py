"""client_hook HTTP 服务、发送与持久消息消费入口。"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sys

from client import Client, ClientError
from tool.settings import hook_url, hook_port, local_host


def output(value):
    print(json.dumps(value, ensure_ascii=False), flush=True)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--url', default=hook_url())
    parser.add_argument('--token', default=os.environ.get('CLIENT_HOOK_TOKEN'))
    sub = parser.add_subparsers(dest='command', required=True)
    serve = sub.add_parser('serve')
    serve.add_argument('--host', default=local_host())
    serve.add_argument('--port', '--StartPort', type=int, default=hook_port())
    serve.add_argument('--state-dir', type=Path, default=Path(__file__).resolve().parents[1] / 'state')
    serve.add_argument('--pid', type=int)
    serve.add_argument('--stop-file', type=Path, help='本机管理脚本使用的正常退出标记')
    serve.add_argument('--fixture-pid', type=int, help='仅自有原生 host 测试')
    serve.add_argument('--simulate', action='store_true', help='仅模拟 HTTP 测试，不加载微信或 DLL')
    serve.add_argument('--endpoint', default='*')
    serve.add_argument('--callback-url', '--CallBackURL')
    serve.add_argument('--retention-days', type=float, default=7)
    serve.add_argument('--max-bytes', type=int, default=1024**3)
    sub.add_parser('status')
    send = sub.add_parser('send')
    send.add_argument('--to', required=True)
    send.add_argument('--text', required=True)
    send.add_argument('--request-id')
    listen = sub.add_parser('listen')
    listen.add_argument('--consumer-id', required=True)
    listen.add_argument('--start', choices=('earliest', 'latest'), default='earliest')
    operation = sub.add_parser('request')
    operation.add_argument('request_id')
    args = parser.parse_args(argv)
    try:
        if args.command == 'serve':
            if not 0 < args.port < 65536 or args.retention_days <= 0 or args.max_bytes <= 0:
                parser.error('端口、保留期限与容量必须为正数且有效')
            if args.simulate and (args.pid or args.fixture_pid):
                parser.error('simulate 不能指定目标进程')
            # Validate listener before any target mutation.
            import ipaddress
            from urllib.parse import urlsplit
            local = args.host == 'localhost'
            try:
                local = local or ipaddress.ip_address(args.host).is_loopback
            except ValueError:
                pass
            if not local and not args.token:
                parser.error('非回环监听必须配置 token')
            if args.callback_url and urlsplit(args.callback_url).scheme not in ('http', 'https'):
                parser.error('回调必须为 HTTP 或 HTTPS URL')
            from api.backend import NativeBackend, FixtureBackend
            from api.server import Application, Server
            backend = FixtureBackend() if args.simulate else NativeBackend(args.pid, args.fixture_pid, args.endpoint)
            app = Application(args.state_dir, backend, args.retention_days * 86400, args.max_bytes,
                              args.callback_url, args.port)
            server = None
            try:
                server = Server((args.host, args.port), app, args.token)
                output({'type': 'http_ready', 'host': args.host, 'port': server.server_port,
                        'backend': 'simulation' if args.simulate else 'native', 'account': backend.account})
                if args.stop_file:
                    import threading
                    def watch_stop():
                        while not app.stop_event.wait(.25):
                            if args.stop_file.exists():
                                server.shutdown()
                                return
                    threading.Thread(target=watch_stop, daemon=True).start()
                server.serve_forever(poll_interval=.2)
            except KeyboardInterrupt:
                pass
            finally:
                # Wake HTTP long-polls before waiting for request threads.
                app.store.capture('stopped', account=backend.account, reason='service_shutdown')
                if server:
                    server.server_close()
                app.close()
        else:
            client = Client(args.url, args.token)
            if args.command == 'status':
                output(client.status())
            elif args.command == 'send':
                output(client.send_text(args.to, args.text, args.request_id))
            elif args.command == 'request':
                output(client.operation(args.request_id))
            elif args.command == 'listen':
                client.listen(args.consumer_id, output, args.start)
        return 0
    except KeyboardInterrupt:
        return 130
    except (ClientError, OSError, RuntimeError, ValueError) as error:
        output({'type': 'error', 'error': str(error)})
        return 1


