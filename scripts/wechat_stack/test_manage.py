"""管理边界与真实模拟HTTP进程退出测试；不连接微信或Codex。"""
import importlib.util
import json
import os
from pathlib import Path
import secrets
import socket
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

spec = importlib.util.spec_from_file_location('manage', Path(__file__).with_name('manage.py'))
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)


class Boundaries(unittest.TestCase):
    def test_local_configuration_checks_allowlist_and_preserved_credentials(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            mcp, codex = root / 'mcp', root / 'codex'
            mcp.mkdir(); codex.mkdir()
            local = {'account': 'self', 'conversations': ['peer']}
            (mcp / 'config.json').write_text(json.dumps(local))
            (mcp / 'credentials.json').write_text(json.dumps({'client_hook_token': 'sealed-hook', 'mcp_token': 'sealed-mcp'}))
            (codex / 'credentials.json').write_text(json.dumps({'mcp_token': 'sealed-mcp'}))
            config = {**local, 'simulation': False, 'mcp_url': m.mcp_url()}
            with patch.dict(m.SPECS, {'mcp': (root / 'bridge.py', mcp, 30002),
                                      'codex': (root / 'agent.py', codex, None)}), \
                 patch.object(m, 'read_config', return_value=config), \
                 patch.object(m, 'unseal', side_effect=lambda value: value.removeprefix('sealed-')):
                self.assertEqual(m.configuration(), (config, 'hook', 'mcp'))
                (codex / 'credentials.json').write_text(json.dumps({'mcp_token': 'sealed-other'}))
                with self.assertRaisesRegex(RuntimeError, '凭据不一致'):
                    m.configuration()
                local['conversations'] = ['another-peer']
                (mcp / 'config.json').write_text(json.dumps(local))
                with self.assertRaisesRegex(RuntimeError, '白名单'):
                    m.configuration()

    def test_exact_script_and_state_required(self):
        entry, directory, _ = m.SPECS['mcp']
        cmd = subprocess.list2cmdline([sys.executable, str(entry), 'serve', '--state-dir', str(directory)])
        self.assertTrue(m.matching(cmd, 'mcp'))
        self.assertFalse(m.matching(cmd.replace(str(directory), str(directory) + '-other'), 'mcp'))
        self.assertFalse(m.matching(cmd.replace(str(entry), str(entry) + '.bak'), 'mcp'))
        self.assertFalse(m.managed('mcp', [{'CommandLine': cmd}]))
        cmd += ' --stop-file ' + subprocess.list2cmdline([str(m.marker('mcp'))])
        self.assertTrue(m.managed('mcp', [{'CommandLine': cmd}]))

    def test_occupied_foreign_port_never_launches(self):
        with patch.object(m, 'configuration', return_value=({}, 'hook', 'mcp')), \
             patch.object(m, 'processes', return_value=[]), patch.object(m, 'occupied', return_value=True), \
             patch.object(m, 'spawn') as spawn:
            with self.assertRaisesRegex(RuntimeError, '其他服务'):
                m.start()
            spawn.assert_not_called()

    def test_failure_rolls_back_only_new_services(self):
        calls = []
        # Existing hook must survive failure of the newly launched bridge.
        with patch.object(m, 'configuration', return_value=({}, 'hook', 'mcp')), \
             patch.object(m, 'processes', side_effect=lambda n: [{}] if n == 'client_hook' else []), \
             patch.object(m, 'managed', return_value=True), patch.object(m, 'occupied', return_value=False), \
             patch.object(m, 'spawn', return_value=object()), \
             patch.object(m, 'wait_ready', side_effect=[None, RuntimeError('failed')]), \
             patch.object(m, 'stop_one', side_effect=calls.append):
            with self.assertRaises(RuntimeError):
                m.start()
        self.assertEqual(calls, ['mcp'])

    def test_idempotent_start_does_not_launch_again(self):
        with patch.object(m, 'configuration', return_value=({}, 'hook', 'mcp')), \
             patch.object(m, 'processes', return_value=[{}]), patch.object(m, 'managed', return_value=True), \
             patch.object(m, 'occupied', return_value=True), patch.object(m, 'wait_ready'), patch.object(m, 'spawn') as spawn:
            m.start()
            spawn.assert_not_called()

    def test_bridge_health_rejects_active_cloud_or_wrong_group_policy(self):
        config = {'account': 'self', 'conversations': ['peer']}
        value = {'simulation': False, 'account': 'self', 'allowed_conversations': ['peer'],
            'subscriptions_active': 0, 'worker_errors': {}, 'source': {'state': 'running'},
            'group_reply_policy': {'require_explicit_self_mention': True}}
        with patch.object(m, 'request', return_value=value):
            self.assertTrue(m.healthy('mcp', config, 'h', 'm'))
            value['subscriptions_active'] = 1
            self.assertFalse(m.healthy('mcp', config, 'h', 'm'))
            value['subscriptions_active'] = 0
            value['group_reply_policy']['require_explicit_self_mention'] = False
            self.assertFalse(m.healthy('mcp', config, 'h', 'm'))

    def test_hook_requires_running_capture_for_configured_account(self):
        value = {'backend': 'native', 'capture': {'account': 'self', 'state': 'running'}, 'background_error': None}
        with patch.object(m, 'request', return_value=value):
            self.assertTrue(m.healthy('client_hook', {'account': 'self'}, 'h', 'm'))
            value['capture']['account'] = 'other'
            self.assertFalse(m.healthy('client_hook', {'account': 'self'}, 'h', 'm'))
            value['capture'].update(account='self', state='stopped')
            self.assertFalse(m.healthy('client_hook', {'account': 'self'}, 'h', 'm'))


class RealProcessLifecycle(unittest.TestCase):
    def check_service(self, name, from_env=False):
        with tempfile.TemporaryDirectory(prefix='wechat-stack-test-') as temporary:
            root = Path(temporary)
            with socket.socket() as sock:
                sock.bind(('127.0.0.1', 0))
                port = sock.getsockname()[1]
            token = secrets.token_urlsafe(32)
            env = os.environ.copy()
            env['CLIENT_HOOK_TOKEN'] = env['MCP_BRIDGE_TOKEN'] = token
            if from_env:
                env['RE_WECHAT_MCP_PORT'] = str(port)
            entry = m.SPECS[name][0]
            args = [sys.executable, '-B', '-X', 'utf8', str(entry), 'serve', '--simulate',
                '--state-dir', str(root / 'data'), '--stop-file', str(root / 'stop')]
            if not from_env:
                args += ['--port', str(port)]
            with (root / 'log').open('wb') as log:
                process = subprocess.Popen(args, env=env, stdout=log, stderr=log,
                    creationflags=subprocess.CREATE_NO_WINDOW)
                try:
                    deadline = time.monotonic() + 15
                    while True:
                        try:
                            result = m.request(port, '/health' if name == 'mcp' else '/client_hook/status', token)
                            break
                        except (OSError, RuntimeError):
                            if process.poll() is not None or time.monotonic() >= deadline:
                                self.fail('模拟服务未就绪')
                            time.sleep(.1)
                    self.assertTrue(result.get('simulation') if name == 'mcp' else result['backend'] == 'simulation')
                    # Exercise the actual manager's CIM identity check and stop
                    # protocol against our isolated child, not a live account.
                    with patch.dict(m.SPECS, {name: (entry, root / 'data', port)}), \
                         patch.object(m, 'marker', return_value=root / 'stop'):
                        self.assertTrue(m.processes(name))
                        m.stop_one(name)
                    self.assertEqual(process.wait(timeout=25), 0)
                    self.assertFalse(m.occupied(port))
                finally:
                    if process.poll() is None:
                        # Only this owned simulated child; never kill live services.
                        process.terminate()
                        process.wait(10)
            self.assertTrue((root / 'data').is_dir())

    def test_hook_graceful_stop_and_port_release(self):
        self.check_service('client_hook')

    def test_mcp_graceful_stop_and_port_release(self):
        self.check_service('mcp')

    def test_mcp_port_injected_from_environment(self):
        self.check_service('mcp', from_env=True)


if __name__ == '__main__':
    unittest.main(verbosity=2)
