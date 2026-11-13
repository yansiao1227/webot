"""监控器复用手动管理器的锁、身份检查及正常启停协议。"""
import ctypes
import sys

import manage as manager

sys.path.insert(0, str(manager.ROOT / 'client_hook'))


def probe():
    """只读检查固定版本及登录身份；不加载 DLL、不读取聊天数据。"""
    from hook_runtime.loader import Win, same_user, normalized
    from hook_runtime.target import discover
    from hook_runtime.login_identity import read_identity, LoginUnavailable
    api = Win()
    if not api.named_processes('Weixin.exe'):
        return {'state': 'waiting_wechat'}
    target = discover(api)
    process = api.api.OpenProcess(0x1000 | 0x10, False, target['pid'])
    if not process:
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        same_user(api.api, process)
        created = int(target['creation_filetime_100ns'])
        if api.creation(process)[0] != created:
            raise RuntimeError('微信实例已变化')
        base = next(base for path, base, _ in api.modules(target['pid'])
                    if path == normalized(target['core_path']))
        result = {'pid': target['pid'], 'created': created}
        try:
            account = read_identity(api, process, base)
        except LoginUnavailable:
            return dict(result, state='waiting_login')
        if api.creation(process)[0] != created:
            raise RuntimeError('微信实例已变化')
        return dict(result, state='ready', account=account)
    finally:
        api.api.CloseHandle(process)


class Lifecycle:
    def __enter__(self):
        self.lock = manager.StateLock(manager.STATE)
        return self

    def __exit__(self, *args):
        self.lock.close()

    def paused(self):
        return (manager.STATE / 'supervisor.pause').exists()

    def configured(self):
        return manager.configuration()

    def present(self):
        return {name: bool(manager.processes(name)) for name in manager.SPECS}

    def stop(self):
        # 超时会抛出异常，保留上游，不强杀，不继续启动。
        for name in reversed(manager.SPECS):
            manager.stop_one(name)

    def start(self, target):
        def verify():
            if self.paused() or probe() != target:
                raise RuntimeError('启动期间微信身份或控制状态发生变化')
        manager.start(target=target, verify=verify)

    def health(self, credentials):
        return all(manager.healthy(name, *credentials) for name in manager.SPECS)
