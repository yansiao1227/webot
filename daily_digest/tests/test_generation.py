from copy import deepcopy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import AsyncMock

from daily_digest.examples.build_preview import sample
from daily_digest.generation import generate_local
from wechat_codex.local_agent.codex_backend import ModelTimeout


class GenerationTests(unittest.IsolatedAsyncioTestCase):
    def data(self):
        edition = sample()
        edition.pop('content_hash')
        edition['simulation'] = False
        packet = {key: deepcopy(edition[key]) for key in ('date', 'cutoff_at', 'sources', 'markets', 'warnings')}
        packet['news'] = {key: [{'source_id': item['source_ids'][0]} for item in items]
                          for key, items in edition['articles'].items()}
        backend = AsyncMock()
        backend.create_image_thread.return_value = 'image-thread'
        backend.image_task.return_value = {'items': []}, {'turn_id': 'image-turn'}
        backend.create_report_thread.return_value = 'thread-local'
        async def report(thread, enriched, **kwargs):
            value = deepcopy(edition)
            value['sources'] = enriched['sources']
            return value, {'turn_id': 'turn-local'}
        backend.generate_report.side_effect = report
        return packet, backend

    async def test_generated_edition_saved_and_existing_attempt_never_overwritten(self):
        packet, backend = self.data()
        with tempfile.TemporaryDirectory() as root:
            directory = Path(root) / 'run'
            edition = await generate_local(directory, backend, packet=packet)
            self.assertFalse(edition['simulation'])
            self.assertEqual(json.loads((directory / 'edition.json').read_text(encoding='utf-8')), edition)
            record = json.loads((directory / 'generation.json').read_text())
            self.assertEqual(record['state'], 'validated')
            self.assertEqual(record['website_writes'], 0)
            with self.assertRaises(FileExistsError):
                await generate_local(directory, backend, packet=packet)
            backend.generate_report.assert_awaited_once()
            backend.close.assert_awaited_once()

    async def test_timeout_preserves_original_identity_and_stage_without_retry(self):
        packet, backend = self.data()
        backend.generate_report.side_effect = ModelTimeout('thread-local', 'turn-original', True, 'generation')
        with tempfile.TemporaryDirectory() as root:
            directory = Path(root) / 'run'
            with self.assertRaises(ModelTimeout):
                await generate_local(directory, backend, packet=packet)
            record = json.loads((directory / 'generation.json').read_text())
            self.assertEqual(record['state'], 'generation_unknown')
            self.assertEqual(record['turn_id'], 'turn-original')
            self.assertEqual(record['timeout_stage'], 'generation')
            self.assertTrue(record['interruption_confirmed'])
            self.assertFalse((directory / 'edition.json').exists())
            backend.generate_report.assert_awaited_once()

    async def test_insufficient_collection_never_starts_model(self):
        packet, backend = self.data()
        packet['news']['ai'] = []
        with tempfile.TemporaryDirectory() as root:
            with self.assertRaisesRegex(ValueError, 'insufficient'):
                await generate_local(Path(root) / 'run', backend, packet=packet)
            backend.start.assert_not_awaited()

    async def test_expired_snapshot_is_rejected_before_model(self):
        packet, backend = self.data()
        packet['cutoff_at'] = '2030-01-02T23:59:59+08:00'
        packet['sources'][0]['published_at'] = '2030-01-01T23:59:30+08:00'
        with tempfile.TemporaryDirectory() as root:
            with self.assertRaisesRegex(ValueError, '来源过期'):
                await generate_local(Path(root) / 'run', backend, packet=packet)
            backend.start.assert_not_awaited()
