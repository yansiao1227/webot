import io
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

from local_agent import cli


class CLITests(unittest.TestCase):
    def test_background_start_uses_public_entry_and_explicit_state(self):
        with tempfile.TemporaryDirectory() as directory:
            process = Mock()
            process.poll.return_value = 1
            with patch.object(sys, 'argv', ['agent.py', '--state-dir', directory, 'start']), \
                    patch.object(cli.subprocess, 'Popen', return_value=process) as popen:
                with self.assertRaisesRegex(RuntimeError, '后台进程启动失败'):
                    cli.main()
            command = popen.call_args.args[0]
            self.assertEqual(Path(command[4]), Path(__file__).resolve().parents[1] / 'agent.py')
            self.assertEqual(command[5:], ['--state-dir', str(Path(directory).resolve()), 'run'])

    def test_entrypoint_does_not_print_sensitive_exception_details(self):
        output = io.StringIO()
        with patch.object(cli, 'main', side_effect=RuntimeError('private request detail')), \
                patch('sys.stdout', output):
            with self.assertRaises(SystemExit) as raised:
                cli.entrypoint()
        self.assertEqual(raised.exception.code, 1)
        self.assertIn('RuntimeError', output.getvalue())
        self.assertNotIn('private request detail', output.getvalue())
