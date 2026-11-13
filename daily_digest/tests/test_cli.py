from contextlib import redirect_stdout
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from types import SimpleNamespace

from daily_digest.cli import main
from daily_digest.store import DigestStore


class CliTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.directory = Path(self.temp.name)
        def factory(config, directory, validate_only=False):
            if config.get('publisher', 'http_api') == 'http_api' and not validate_only and not (Path(directory) / 'credentials.json').exists():
                raise ValueError('missing API credential')
            return SimpleNamespace(status=lambda: {'publisher': config.get('publisher', 'http_api'),
                'site_url': config['site_url'], 'latest_release': {'release_id': 'stable-release', 'state': 'confirmed'}})
        self.factory = patch('daily_digest.publisher.create_publisher', create=True, side_effect=factory)
        self.publisher_factory = self.factory.start()

    def tearDown(self):
        self.factory.stop()
        self.temp.cleanup()

    def invoke(self, *args):
        output = io.StringIO()
        with patch('sys.argv', ['daily_digest', '--state-dir', self.temp.name, *args]), redirect_stdout(output):
            main()
        return json.loads(output.getvalue())

    def configure(self, site='https://daily.example.org'):
        return self.invoke('configure', '--account', 'wxid_self', '--site-url', site)

    def test_configuration_requires_separate_mobile_verification(self):
        self.assertFalse(self.configure()['activated'])
        self.assertFalse(self.invoke('status')['activated'])
        with self.assertRaises(ValueError):
            self.invoke('activate')
        (self.directory / 'credentials.json').write_text('{"publish_token":"encrypted-placeholder"}', encoding='utf-8')
        self.assertTrue(self.invoke('activate')['activated'])
        self.assertTrue(self.invoke('status')['activated'])
        self.configure('https://new.example.org')
        self.assertFalse(self.invoke('status')['activated'])

    def test_pause_resume_persist_without_deleting_runs(self):
        self.configure()
        self.assertTrue(self.invoke('pause')['paused'])
        self.assertFalse(self.invoke('resume')['paused'])
        self.assertEqual(self.invoke('runs'), [])
        with self.assertRaises(ValueError):
            self.invoke('configure', '--account', 'wxid_other', '--site-url', 'https://daily.example.org')

    def test_ssh_mode_activates_without_http_secret_and_preserves_publisher_choice(self):
        self.invoke('configure', '--account', 'wxid_self', '--site-url', 'http://public.example.org', '--publisher', 'ssh_snapshot')
        self.assertFalse((self.directory / 'credentials.json').exists())
        self.assertEqual(self.invoke('status')['publisher'], 'ssh_snapshot')
        self.assertTrue(self.invoke('activate')['activated'])
        self.assertFalse(self.publisher_factory.call_args.kwargs.get('validate_only', False))
        self.invoke('configure', '--account', 'wxid_self', '--site-url', 'http://changed.example.org')
        status = self.invoke('status')
        self.assertFalse(status['activated'])
        self.assertEqual(status['publisher'], 'ssh_snapshot')
        with self.assertRaisesRegex(ValueError, '不需要'):
            self.invoke('set-token')

    def test_legacy_config_without_publisher_remains_http_api(self):
        self.directory.joinpath('config.json').write_text(json.dumps({'account': 'wxid_self', 'site_url': 'https://daily.example.org'}))
        self.assertEqual(self.invoke('status')['publisher'], 'http_api')

    def test_status_reads_publisher_without_requiring_credentials_and_contains_failures(self):
        self.configure()
        self.publisher_factory.reset_mock()
        result = self.invoke('status')
        self.assertEqual(result['publisher_status']['latest_release']['state'], 'confirmed')
        self.assertTrue(self.publisher_factory.call_args.kwargs['validate_only'])
        self.assertFalse((self.directory / 'credentials.json').exists())
        self.publisher_factory.side_effect = ValueError('secret details must not leak')
        failed = self.invoke('status')
        self.assertEqual(failed['publisher_status'], {'state': 'unavailable', 'error': 'ValueError'})
        self.assertNotIn('secret details', json.dumps(failed))
