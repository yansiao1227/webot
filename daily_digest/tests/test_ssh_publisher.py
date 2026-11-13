import asyncio
from copy import deepcopy
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from daily_digest.publisher import create_publisher
from daily_digest.ssh_publisher import SSHSnapshotPublisher, validate_origin


class SSHPublisherTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.directory = Path(self.temp.name)
        self.release = SimpleNamespace(release_id='release-original')
        self.deployer = Mock()
        self.deployer.prepare.return_value = self.release
        self.deployer.load_release.return_value = self.release
        self.deployer.deploy.return_value = {'state': 'confirmed'}
        self.deployer.verify.return_value = {'state': 'confirmed'}
        self.command = Mock(return_value=SimpleNamespace(returncode=0))
        self.edition = {'date': '2026-11-11', 'content_hash': 'a' * 64, 'sources': []}
        self.reader = Mock(side_effect=lambda path: deepcopy(self.edition) if path.startswith('/api/editions/') else b'page')
        self.publisher = SSHSnapshotPublisher({'site_url': 'http://daily.example.org', 'ssh_host': 'daily.example.org'},
            self.directory, deployer=self.deployer, command=self.command, reader=self.reader)
        self.publisher.database = self.directory / 'editions.sqlite'
        self.validation = patch('daily_digest.content.validate_edition', side_effect=lambda value, **_: deepcopy(value))
        self.validation.start()

    def tearDown(self):
        self.validation.stop()
        self.temp.cleanup()

    async def test_import_and_release_are_frozen_before_remote_effect(self):
        await self.publisher.upload_media(self.edition, self.directory / 'media')
        await self.publisher.upload(self.edition)
        self.assertTrue(self.publisher._mapping(self.edition).exists())
        args = self.command.call_args.args[0]
        self.assertIsInstance(args, list)
        self.assertIn('--db', args)
        self.assertIn('--media', args)
        self.assertFalse(self.command.call_args.kwargs.get('shell', False))
        await self.publisher.upload(self.edition)
        self.command.assert_called_once()
        self.deployer.prepare.assert_called_once()
        self.assertEqual(await self.publisher.verify(self.edition), 'http://daily.example.org/daily/2026-11-11')
        self.deployer.verify.assert_called_once_with(self.release)

    async def test_lost_deployment_response_only_verifies_original_after_restart(self):
        self.deployer.deploy.side_effect = TimeoutError
        with self.assertRaises(TimeoutError):
            await self.publisher.upload(self.edition)
        restarted = SSHSnapshotPublisher({'site_url': 'http://daily.example.org', 'ssh_host': 'daily.example.org'},
            self.directory, deployer=self.deployer, command=self.command, reader=self.reader)
        await restarted.verify(self.edition)
        self.deployer.deploy.assert_called_once()
        self.deployer.prepare.assert_called_once()
        self.deployer.load_release.assert_called_with('release-original')

    async def test_manual_edition_can_be_verified_without_creating_deployment(self):
        await self.publisher.verify(self.edition)
        self.deployer.prepare.assert_not_called()
        self.deployer.deploy.assert_not_called()

    async def test_readback_hash_mismatch_refuses_publication_confirmation(self):
        self.reader.side_effect = None
        self.reader.return_value = {**self.edition, 'content_hash': 'b' * 64}
        with self.assertRaisesRegex(ValueError, '摘要'):
            await self.publisher.verify(self.edition)

    async def test_remote_deployment_must_be_confirmed_before_moment(self):
        await self.publisher.upload(self.edition)
        self.deployer.verify.return_value = {'state': 'activated'}
        with self.assertRaisesRegex(RuntimeError, '完整确认'):
            await self.publisher.verify(self.edition)

    async def test_bound_media_must_match_local_and_public_hash(self):
        value = {**self.edition, 'sources': [{'image': {'asset_id': 'c' * 64}}]}
        media = self.directory / 'media'
        media.mkdir()
        (media / ('c' * 64 + '.webp')).write_bytes(b'wrong-media')
        with self.assertRaisesRegex(ValueError, '媒体'):
            await self.publisher.upload_media(value, media)
        self.reader.side_effect = lambda path: value if path.startswith('/api/') else b'wrong-media'
        with self.assertRaisesRegex(ValueError, '媒体'):
            await self.publisher.verify(value)

    async def test_import_failure_does_not_create_or_submit_release(self):
        self.command.return_value = SimpleNamespace(returncode=3)
        with self.assertRaisesRegex(RuntimeError, '导入失败'):
            await self.publisher.upload(self.edition)
        self.deployer.prepare.assert_not_called()
        self.deployer.deploy.assert_not_called()

    async def test_history_validates_index_and_exact_edition(self):
        self.reader.side_effect = lambda path: {'items': [{'date': self.edition['date'],
            'content_hash': self.edition['content_hash']}], 'next_cursor': None} if '?' in path else (
                self.edition if path.startswith('/api/') else b'page')
        self.assertEqual(await self.publisher.history(), [self.edition])

    async def test_status_reads_local_mapping_and_state_without_remote_or_key(self):
        mapping = self.publisher._mapping(self.edition)
        mapping.parent.mkdir(parents=True)
        mapping.write_text(json.dumps({**self.edition, 'release_id': 'd-release'}), encoding='utf-8')
        directory = self.publisher.directory / 'releases/d-release'
        directory.mkdir(parents=True)
        record = {'release_id': 'd-release', 'target': ['daily.example.org', 'root'],
                  'state': 'confirmed', 'error': None, 'updated_at': 123, 'private': 'not for status'}
        (directory / 'state.json').write_text(json.dumps(record), encoding='utf-8')
        result = self.publisher.status()['latest_release']
        self.assertEqual(result['release_id'], 'd-release')
        self.assertEqual(result['state'], 'confirmed')
        self.assertTrue(result['server_confirmed'])
        self.assertNotIn('private', json.dumps(result))
        self.deployer.status.assert_not_called()
        self.reader.assert_not_called()
        self.command.assert_not_called()

    async def test_status_rejects_malformed_local_record_without_leaking_error(self):
        mapping = self.publisher._mapping(self.edition)
        mapping.parent.mkdir(parents=True)
        mapping.write_text('{bad private token', encoding='utf-8')
        value = self.publisher.status()
        self.assertEqual(value['state'], 'unavailable')
        self.assertNotIn('private token', json.dumps(value))


class PublisherConfigurationTests(unittest.TestCase):
    def test_ssh_http_origin_is_bound_to_configured_host(self):
        for site in ('http://other.example.org', 'http://daily.example.org:8080',
                     'http://user:pass@daily.example.org', 'http://daily.example.org/path'):
            with self.assertRaises(ValueError):
                validate_origin(site, 'daily.example.org')
        self.assertEqual(validate_origin('http://daily.example.org/', 'daily.example.org'), 'http://daily.example.org')

    def test_legacy_api_is_still_https_only(self):
        with tempfile.TemporaryDirectory() as state:
            with self.assertRaises(ValueError):
                create_publisher({'site_url': 'http://daily.example.org'}, state, validate_only=True)
            publisher = create_publisher({'publisher': 'ssh_snapshot', 'site_url': 'http://daily.example.org',
                'ssh_host': 'daily.example.org'}, state, validate_only=True)
            self.assertEqual(publisher.status()['publisher'], 'ssh_snapshot')


if __name__ == '__main__':
    unittest.main()
