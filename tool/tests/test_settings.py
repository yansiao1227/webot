import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from tool import settings
from tool.content_reading.api import invoke


class SettingsTests(unittest.TestCase):
    def test_dotenv_quotes_windows_paths_expansion_and_process_precedence(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / '.env'
            path.write_text('BASE="D:\\Apps With Spaces"\nBIN=${BASE}/python.exe\n'
                            'export PORT=33333 # comment\nLITERAL=$(do-not-run)\n', encoding='utf-8')
            env = {'BASE': 'E:/override'}
            settings.load_env(path, env)
            self.assertEqual(env['BIN'], 'E:/override/python.exe')
            self.assertEqual(env['PORT'], '33333')
            self.assertEqual(env['LITERAL'], '$(do-not-run)')
            env = {}
            settings.load_env(path, env)
            self.assertEqual(env['BASE'], 'D:\\Apps With Spaces')

    def test_dotenv_invalid_or_circular_variables_fail_before_mutating_environment(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / '.env'
            for text in ('A=${B}\nB=${A}', 'A=${MISSING}', 'not a setting', 'A="unclosed'):
                path.write_text(text, encoding='utf-8')
                env = {}
                with self.assertRaises(ValueError):
                    settings.load_env(path, env)
                self.assertEqual(env, {})

    def test_configured_addresses_keep_loopback_boundary(self):
        with patch.dict(os.environ, {'RE_WECHAT_HOST': 'localhost', 'RE_WECHAT_HOOK_PORT': '32101',
                                     'RE_WECHAT_MCP_PORT': '32102'}):
            self.assertEqual(settings.hook_url(), 'http://localhost:32101')
            self.assertEqual(settings.mcp_url(), 'http://localhost:32102/mcp')
        for host in ('0.0.0.0', '192.168.1.2', 'example.com', '127.0.0.1/path'):
            with patch.dict(os.environ, {'RE_WECHAT_HOST': host}), self.assertRaises(ValueError):
                settings.local_host()
        with patch.dict(os.environ, {'RE_WECHAT_MCP_PORT': '65536'}), self.assertRaises(ValueError):
            settings.mcp_port()

    def test_cache_location_is_injected_and_explicit_output_wins(self):
        from PIL import Image
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / 'source.png'
            Image.new('RGB', (64, 48), 'green').save(source)
            with patch.dict(os.environ, {'CONTENT_CACHE_DIR': str(root / 'cache')}):
                result = invoke('inspect-media', path=source, kind='image')
                target = Path(result['result']['images'][0])
                self.assertTrue(target.is_relative_to(root / 'cache'))
                result = invoke('inspect-media', path=source, kind='image', output_dir=root / 'explicit')
                self.assertTrue(Path(result['result']['images'][0]).is_relative_to(root / 'explicit'))
            self.assertTrue(source.exists())

    def test_cli_outside_repository_loads_selected_env_file(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            env_file = root / '.env'
            env_file.write_text('RE_WECHAT_GCC="C:/Custom Compiler/gcc.exe"\n', encoding='utf-8')
            env = os.environ.copy()
            env['RE_WECHAT_ENV_FILE'] = str(env_file)
            env.pop('RE_WECHAT_GCC', None)
            result = subprocess.run([sys.executable, '-B', '-X', 'utf8', str(Path(settings.__file__)),
                                     '--get', 'RE_WECHAT_GCC'], cwd=root, env=env,
                                    capture_output=True, text=True, timeout=10)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout.strip().replace('\\', '/'), 'C:/Custom Compiler/gcc.exe')
