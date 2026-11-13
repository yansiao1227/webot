import asyncio
from copy import deepcopy
from hashlib import sha256
from io import BytesIO
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from PIL import Image
from daily_digest.tests import test_generation
from daily_digest.images import prepare_images, validate_freshness
from tool.news_images import cache_image
from tool.digest_sources import read_bytes


class Backend:
    def __init__(self, kind='event', reject=False, timeout=False):
        self.kind, self.reject, self.timeout = kind, reject, timeout
        self.calls = []
    async def create_image_thread(self): return 'images-thread'
    async def image_task(self, thread, prompt, schema, timeout, on_event, image_paths=()):
        self.calls.append(prompt)
        if self.timeout: raise TimeoutError()
        if '联网搜索为' in prompt:
            items = [{'source_id': 'ai-1', 'url': 'https://example.org/image.webp',
                      'page_url': 'https://example.org/page', 'credit': 'Example', 'kind': self.kind, 'reason': '核实图片'}]
        else:
            data = json.loads(prompt.split('图片与新闻数据：')[1])
            assert len(image_paths) == len(data)
            items = [{'source_id': c['source_id'], 'asset_id': c['image']['asset_id'], 'status': 'reject' if self.reject else 'accept',
                'kind': c['image']['kind'], 'reason': '已查看图片并核对出处'} for c in data]
        return {'items': items}, {'turn_id': 'image-turn'}


class ImageTests(unittest.IsolatedAsyncioTestCase):
    def packet(self):
        packet, _ = test_generation.GenerationTests().data()
        # fixture IDs follow the source records
        self.sid = packet['news']['ai'][0]['source_id']
        return packet
    def download(self, url, directory, **kwargs):
        raw = BytesIO(); Image.new('RGB', (1800, 900), 'blue').save(raw, 'PNG')
        return cache_image(url, directory, reader=lambda _: raw.getvalue())
    async def run_case(self, backend, original=False, fail=False):
        packet = self.packet()
        if original:
            packet['sources'][0]['image'] = {'url': 'https://example.org/original.png', 'credit': 'Example', 'caption': '新闻原图'}
        # Make search fixture target actual first source.
        old = backend.image_task
        async def task(*args):
            value, meta = await old(*args)
            for item in value['items']:
                if item['source_id'] == 'ai-1': item['source_id'] = self.sid
            return value, meta
        backend.image_task = task
        with tempfile.TemporaryDirectory() as root:
            downloader = (lambda *a, **k: (_ for _ in ()).throw(ValueError('download_failed'))) if fail else self.download
            result = await prepare_images(packet, backend, root, downloader=downloader)
            count = len(backend.calls)
            again = await prepare_images(packet, backend, root, downloader=downloader)
            self.assertEqual(result, again); self.assertEqual(count, len(backend.calls))
            state = json.loads((Path(root) / 'images.json').read_text())
            self.assertTrue(all(s.get('image') for s in result['sources']))
            return result['sources'][0]['image'], state
    async def test_original_and_resize(self):
        image, _ = await self.run_case(Backend(), original=True)
        self.assertEqual(image['kind'], 'original'); self.assertEqual(image['width'], 1600)
    async def test_event_and_theme(self):
        for kind in ('event', 'theme'):
            image, _ = await self.run_case(Backend(kind))
            self.assertEqual(image['kind'], kind)
    async def test_wrong_image_no_results_and_download_failure(self):
        for backend, fail in ((Backend(reject=True), False), (Backend(), True)):
            image, _ = await self.run_case(backend, fail=fail)
            self.assertEqual(image['kind'], 'illustration')
    async def test_timeout_preserves_identity_without_replay(self):
        image, state = await self.run_case(Backend(timeout=True))
        self.assertEqual(image['kind'], 'illustration')
        self.assertEqual(state['calls'][0]['state'], 'unknown')
        self.assertEqual(state['calls'][0]['thread_id'], 'images-thread')
    async def test_deadline_and_expiry_do_not_move_capture(self):
        packet = self.packet(); before = deepcopy(packet)
        with tempfile.TemporaryDirectory() as root:
            backend = Backend()
            await prepare_images(packet, backend, root, budget=0)
            self.assertEqual(backend.calls, [])
        self.assertEqual(packet, before)
        with self.assertRaisesRegex(ValueError, 'expired'):
            validate_freshness(packet, lambda: 2_000_000_000)
    async def test_empty_search_and_partial_resume(self):
        packet = self.packet()
        backend = Backend()
        async def empty(*args):
            backend.calls.append('empty')
            return {'items': []}, {'turn_id': 'empty-turn'}
        backend.image_task = empty
        with tempfile.TemporaryDirectory() as root:
            result = await prepare_images(packet, backend, root)
            self.assertTrue(all(s['image']['kind'] == 'illustration' for s in result['sources']))
            journal = Path(root) / 'images.json'
            state = json.loads(journal.read_text())
            # 模拟搜索结果已落盘，最后绑定保存之前进程退出。
            state.pop('state'); state['results'] = {}
            journal.write_text(json.dumps(state))
            await prepare_images(packet, backend, root)
            self.assertEqual(len(backend.calls), 1)

    async def test_review_omission_is_explicit_and_never_replayed(self):
        packet = self.packet()
        packet['sources'][0]['image'] = {'url': 'https://example.org/original.png', 'credit': 'Example', 'caption': '原图'}
        backend = Backend()
        async def omitted(*args):
            backend.calls.append('review')
            return {'items': []}, {'turn_id': 'completed-but-incomplete'}
        backend.image_task = omitted
        with tempfile.TemporaryDirectory() as root:
            await prepare_images(packet, backend, root, search=False, downloader=self.download)
            state = json.loads((Path(root) / 'images.json').read_text())
            self.assertTrue(any(e['reason'] == 'review_incomplete' for e in state['errors']))
            self.assertEqual(state['calls'][0]['state'], 'completed')
            await prepare_images(packet, backend, root, search=False, downloader=self.download)
            self.assertEqual(len(backend.calls), 1)

    def test_private_redirect_and_bad_image(self):
        with patch('tool.digest_sources.resolve_public', return_value='93.184.216.34'), \
             patch('tool.digest_sources.socket.create_connection'), \
             patch('tool.digest_sources.http.client.HTTPConnection') as conn:
            response = conn.return_value.getresponse.return_value
            response.status = 302; response.getheader.return_value = 'http://127.0.0.1/private'
            with self.assertRaises(ValueError): read_bytes('http://example.org/image')
            self.assertEqual(conn.call_count, 1)
        with tempfile.TemporaryDirectory() as root:
            with self.assertRaises(Exception): cache_image('https://example.org/x', root, reader=lambda _: b'not image')

if __name__ == '__main__': unittest.main()
