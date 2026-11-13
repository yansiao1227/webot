"""日报本机入口；generate调用真实模型，本地管理和预览不发布朋友圈。"""
import argparse
from datetime import datetime, timezone
import getpass
import json
from pathlib import Path

from tool.settings import path_setting
from .store import DigestStore


def atomic_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + '.tmp')
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding='utf-8')
    temporary.replace(path)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--state-dir', type=Path,
                        default=path_setting('DAILY_DIGEST_STATE_DIR', 'daily_digest/state/live'))
    sub = parser.add_subparsers(dest='command', required=True)
    configure = sub.add_parser('configure', help='配置账号与站点，不启用自动发布')
    configure.add_argument('--account', required=True)
    configure.add_argument('--site-url', required=True)
    configure.add_argument('--publisher', choices=('ssh_snapshot', 'http_api'))
    sub.add_parser('set-token', help='交互输入站点发布密钥并使用DPAPI保存')
    sub.add_parser('activate', help='确认已经用国内手机微信验证站点访问后启用任务')
    sub.add_parser('status')
    sub.add_parser('runs')
    sub.add_parser('pause')
    sub.add_parser('resume')
    preview = sub.add_parser('preview', help='读取已生成日报或明确的模拟样本，不创建发布意图')
    preview.add_argument('--date')
    preview.add_argument('--sample', action='store_true')
    generate = sub.add_parser('generate', help='真实采集并生成到独立本地目录，不上传、不发朋友圈')
    generate.add_argument('--output-dir', type=Path, required=True, help='新生成使用尚不存在的目录；--resume指定原目录')
    generate.add_argument('--packet', type=Path, help='使用已保存采集快照，保留其原日期与截止时间')
    generate.add_argument('--resume', action='store_true', help='恢复已有配图阶段，不重放未知正文轮次')
    args = parser.parse_args()
    directory = args.state_dir.resolve()
    file = directory / 'config.json'
    if args.command == 'generate':
        import asyncio
        from .generation import generate_local
        from wechat_codex.local_agent.codex_backend import CodexBackend
        agent_file = path_setting('RE_WECHAT_CODEX_STATE_DIR', 'wechat_codex/state/live') / 'config.json'
        agent = json.loads(agent_file.read_text(encoding='utf-8-sig')) if agent_file.exists() else {}
        config = json.loads(file.read_text(encoding='utf-8-sig')) if file.exists() else {}
        packet = json.loads(args.packet.read_text(encoding='utf-8-sig')) if args.packet else None
        backend = CodexBackend(directory / 'generation-workspace', agent.get('model'), codex_bin=agent.get('codex_bin'))
        edition = asyncio.run(generate_local(args.output_dir, backend, packet=packet, sources=config.get('sources'), image_budget=config.get('image_search_budget_seconds', 300), resume=args.resume))
        print(json.dumps({'state': 'validated', 'date': edition['date'],
                          'edition_path': str((args.output_dir / 'edition.json').resolve())}, ensure_ascii=False))
        return
    if args.command == 'configure':
        from .publisher import create_publisher
        old = json.loads(file.read_text(encoding='utf-8')) if file.exists() else {}
        if old and old.get('account') != args.account:
            raise ValueError('已有日报账号不可切换；请使用独立目录')
        # 更换站点需要重新进行手机验收，不沿用旧站点的验收证据。
        config = {**old, 'account': args.account, 'site_url': args.site_url.rstrip('/'),
                  'task_id': 'daily-observer', 'publisher': args.publisher or old.get('publisher', 'http_api')}
        create_publisher(config, directory, validate_only=True)
        if old.get('site_url') != config['site_url'] or old.get('publisher', 'http_api') != config['publisher']:
            config['mobile_verified_at'] = None
        atomic_json(file, config)
        print(json.dumps({'configured': True, 'activated': bool(config.get('mobile_verified_at'))}))
        return
    if args.command == 'preview' and args.sample:
        print((Path(__file__).parent / 'examples/edition.preview.json').read_text(encoding='utf-8'))
        return
    config = json.loads(file.read_text(encoding='utf-8-sig'))
    if args.command == 'set-token':
        if config.get('publisher', 'http_api') == 'ssh_snapshot':
            raise ValueError('SSH快照发布使用SSH身份认证，不需要站点发布密钥')
        from wechat_codex.local_agent.vault import seal
        token = getpass.getpass('站点发布密钥（不会回显）：')
        from .publisher import SitePublisher
        SitePublisher(config['site_url'], token)
        atomic_json(directory / 'credentials.json', {'publish_token': seal(token)})
        print('{"credentials_saved":true}')
        return
    if args.command == 'activate':
        if not config.get('site_url'):
            raise ValueError('请先配置站点')
        from .publisher import create_publisher
        create_publisher(config, directory)
        config['mobile_verified_at'] = datetime.now(timezone.utc).isoformat()
        atomic_json(file, config)
        print('{"activated":true,"restart_codex_required":true}')
        return
    store = DigestStore(directory, config['account'])
    try:
        if args.command in ('pause', 'resume'):
            store.meta('paused', args.command == 'pause')
        if args.command == 'runs':
            output = [{k: row[k] for k in ('date', 'state', 'planned_at', 'intent_id', 'error', 'thread_id', 'turn_id')}
                      for row in store.runs()]
        elif args.command == 'preview':
            row = store.run(args.date) if args.date else next(iter(store.runs(1)), None)
            output = row['edition'] if row else None
        else:
            try:
                from .publisher import create_publisher
                publisher_status = create_publisher(config, directory, validate_only=True).status()
                if not isinstance(publisher_status, dict):
                    raise ValueError('invalid_publisher_status')
            except Exception as error:
                publisher_status = {'state': 'unavailable', 'error': type(error).__name__}
            output = {**store.status(), 'site_url': config.get('site_url'),
                      'publisher': config.get('publisher', 'http_api'),
                      'publisher_status': publisher_status,
                      'preparation_time': '16:30', 'publish_window': '17:00-18:00', 'timezone': 'Asia/Shanghai',
                      'mobile_verified_at': config.get('mobile_verified_at'),
                      'activated': bool(config.get('mobile_verified_at'))}
        print(json.dumps(output, ensure_ascii=False, indent=2))
    finally:
        store.close()


if __name__ == '__main__':
    main()
