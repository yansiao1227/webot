"""当前 Windows 用户登录自启动；只管理本仓库的独立注册项。"""
import argparse
import hashlib
import os
from pathlib import Path
import subprocess

import manage

KEY = r'Software\Microsoft\Windows\CurrentVersion\Run'
NAME = 'WebotSupervisor-' + hashlib.sha256(str(manage.ROOT).lower().encode()).hexdigest()[:12]


def command():
    pythonw = manage.PYTHON.with_name('pythonw.exe')
    if not pythonw.is_file():
        raise RuntimeError('未找到配置 Python 环境的 pythonw.exe')
    return subprocess.list2cmdline([str(pythonw), '-B', '-X', 'utf8',
                                   str(Path(__file__).with_name('supervisor.py').resolve()), 'run'])


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=['install', 'uninstall', 'status', 'start'])
    args = parser.parse_args()
    if os.name != 'nt':
        raise RuntimeError('仅支持 Windows')
    import winreg
    if args.command == 'start':
        subprocess.Popen(command(), cwd=manage.ROOT, stdin=subprocess.DEVNULL,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                         creationflags=subprocess.CREATE_NO_WINDOW)
        print('已请求后台启动监控器；请使用 manage status 确认状态。')
        return
    if args.command == 'install':
        value = command()
        with winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, KEY, 0, winreg.KEY_SET_VALUE) as key:
            winreg.SetValueEx(key, NAME, 0, winreg.REG_SZ, value)
        print('已注册当前用户登录自启动；下次登录生效，或执行 autostart.py start。')
    elif args.command == 'uninstall':
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, KEY, 0, winreg.KEY_SET_VALUE) as key:
                winreg.DeleteValue(key, NAME)
        except FileNotFoundError:
            pass
        print('已取消自启动；正在运行的监控器与服务保持现状。')
    else:
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, KEY) as key:
                value, _ = winreg.QueryValueEx(key, NAME)
            print('已注册' if value == command() else '注册命令与当前环境不一致，请重新安装')
        except FileNotFoundError:
            print('未注册')


if __name__ == '__main__':
    main()
