"""跟随微信生命周期；不启动微信，不重新提交未知业务操作。"""
import argparse
import os
import sys
import threading
import time

from lifecycle import Lifecycle, probe, manager
from local_agent.runtime import atomic_json


class Supervisor:
    def __init__(self, lifecycle=Lifecycle, inspect=probe, clock=time.monotonic):
        self.lifecycle, self.inspect, self.clock = lifecycle, inspect, clock
        self.target = None
        self.candidate = None
        self.failures = 0
        self.retry_at = 0
        self.state = 'starting'

    def tick(self):
        if self.clock() < self.retry_at:
            return self.state
        try:
            with self.lifecycle() as services:
                if services.paused():
                    self.candidate = None
                    self.state = 'paused'
                    return self.state
                try:
                    credentials = services.configured()
                    target = self.inspect()
                except Exception:
                    # 身份/版本/配置无法确认时先停止业务，不能带病继续收发。
                    self.state = 'stopping'
                    services.stop()
                    self.target = self.candidate = None
                    raise
                present = services.present()
                if target['state'] != 'ready' or target.get('account') != credentials[0]['account']:
                    if any(present.values()):
                        self.state = 'stopping'
                        services.stop()
                    self.target = self.candidate = None
                    self.state = target['state'] if target['state'] != 'ready' else 'waiting_account'
                    return self.state
                # 重启监控器时先正常收回既有服务，避免误认旧微信实例。
                if any(present.values()) and (target != self.target or not all(present.values())):
                    self.state = 'stopping'
                    services.stop()
                    self.target = None
                    present = dict.fromkeys(present, False)
                if all(present.values()):
                    # 健康异常可能是未知任务/授权门禁，不以重启掩盖。
                    self.state = 'running' if services.health(credentials) else 'degraded'
                    return self.state
                if self.candidate != target:
                    self.candidate = target
                    self.state = 'stabilizing'
                    return self.state
                self.state = 'starting_services'
                services.start(target)
                self.target = target
                self.failures = 0
                self.state = 'running'
        except Exception:
            # 不记录异常正文，避免泄露账号、凭据、进程参数。
            self.failures += 1
            self.retry_at = self.clock() + min(300, 5 * 2 ** min(self.failures, 6))
            self.state = 'retry_wait'
        return self.state


def run():
    manager.STATE.mkdir(parents=True, exist_ok=True)
    directory = manager.STATE / 'supervisor'
    directory.mkdir(exist_ok=True)
    lock = manager.StateLock(directory)
    stop = directory / 'stop.request'
    stop.unlink(missing_ok=True)
    watcher = Supervisor()
    done = threading.Event()

    def heartbeat():
        while not done.is_set():
            atomic_json(manager.STATE / 'supervisor.json', {
                'pid': os.getpid(), 'heartbeat': time.time(), 'state': watcher.state,
                'failures': watcher.failures})
            done.wait(2)

    thread = threading.Thread(target=heartbeat, daemon=True)
    thread.start()
    try:
        while not stop.exists():
            watcher.tick()
            if stop.exists():
                break
            done.wait(5)
    except KeyboardInterrupt:
        pass
    finally:
        # 退出监控器不暗中停止手动服务；需要停止整栈时使用 manage stop。
        done.set()
        thread.join()
        atomic_json(manager.STATE / 'supervisor.json', {
            'pid': os.getpid(), 'heartbeat': time.time(), 'state': 'stopped'})
        lock.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=['run', 'stop', 'resume'])
    args = parser.parse_args()
    if os.name != 'nt':
        raise RuntimeError('仅支持 Windows')
    manager.STATE.mkdir(parents=True, exist_ok=True)
    if args.command == 'stop':
        directory = manager.STATE / 'supervisor'
        directory.mkdir(exist_ok=True)
        (directory / 'stop.request').write_text('stop\n', encoding='ascii')
        print('已请求退出监控器；服务保持现状，完成状态请查询 manage status。')
    elif args.command == 'resume':
        with Lifecycle():
            (manager.STATE / 'supervisor.pause').unlink(missing_ok=True)
        print('已恢复自动跟随；需有运行中的监控器。')
    else:
        with (manager.STATE / 'supervisor.log').open('a', encoding='utf-8', buffering=1) as log:
            sys.stdout = sys.stderr = log
            try:
                run()
            finally:
                sys.stdout, sys.stderr = sys.__stdout__, sys.__stderr__


if __name__ == '__main__':
    main()
