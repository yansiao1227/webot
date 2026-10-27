import json
import importlib.util
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from local_agent.media import prepare, external_content
from local_agent.codex_backend import SCHEMA
from local_agent.cards import extract, public_url


class MediaTests(unittest.TestCase):
    def test_card_primary_source_is_not_overwritten_by_nested_urls(self):
        result = extract('<msg><appmsg><title>原文</title><type>4</type>'
                         '<url>https://b23.tv/example</url><refermsg><title>其他</title>'
                         '<url>https://example.org/other</url></refermsg></appmsg></msg>')
        self.assertEqual(result['source_urls'], ['https://b23.tv/example'])
        self.assertEqual(result['card_metadata']['title'], '原文')

    def test_finder_media_and_upgrade_url_are_not_article_sources(self):
        result = extract('<msg><appmsg><type>51</type><url>https://support.weixin.qq.com/</url>'
                         '<finderFeed><desc>视频说明</desc><mediaList><media>'
                         '<url>https://example.com/video.mp4</url><duration>15</duration>'
                         '</media></mediaList></finderFeed></appmsg></msg>')
        self.assertEqual(result['source_urls'], [])
        self.assertEqual(result['card_metadata']['finder']['desc'], '视频说明')
        self.assertEqual(result['media_urls'][0]['duration'], '15')

    def test_card_source_requires_public_http_url(self):
        for value in ('file:///C:/secret', 'javascript:alert(1)', 'http://127.0.0.1/x',
                      'http://[::1]/', 'https://user:pass@example.com', 'http://a.local/x',
                      'https://example.com:invalid/', 'http://10.0.0.2/', 'http://localhost/'):
            self.assertIsNone(public_url(value), value)
        self.assertEqual(extract('<msg nickname="名片"/>')['source_urls'], [])

    @unittest.skipUnless(importlib.util.find_spec('pypdfium2') and importlib.util.find_spec('PIL'),
                         'optional PDF rendering dependencies absent')
    def test_scan_pdf_produces_actual_visible_pages(self):
        from PIL import Image, ImageDraw
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'scan.pdf'
            image = Image.new('RGB', (300, 150), 'white')
            ImageDraw.Draw(image).text((20, 30), 'SCANNED 48271', fill='black')
            image.save(path, 'PDF')
            result = prepare({'path': str(path), 'name': 'scan.pdf', 'message_type': 'file'})
            self.assertEqual(result['prepared']['sampled_pages'], [1])
            self.assertFalse(result['prepared']['truncated'])
            content = external_content('scan', result)
            self.assertEqual(content[1]['type'], 'input_image')
            self.assertTrue(content[1]['image_url'].startswith('data:image/png;base64,'))

    def test_strict_model_schema_requires_all_properties(self):
        self.assertEqual(set(SCHEMA['required']), set(SCHEMA['properties']))

    def test_document_and_card_actual_bytes_and_external_authority(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'resource'
            path.write_bytes('文件验证码48271'.encode('gb18030'))
            attachment = {'path': str(path), 'name': 'sample.txt', 'message_type': 'file'}
            result = prepare(attachment)
            self.assertEqual(result['prepared']['text'], '文件验证码48271')
            self.assertEqual(external_content('untrusted', result), [{'type': 'input_text', 'text': 'untrusted'}])
            path.write_text('<msg><appmsg><title>验证码48271</title><url>https://example.com/</url></appmsg></msg>', encoding='utf-8')
            attachment['message_type'] = 'card'
            fields = json.loads(prepare(attachment)['prepared']['text'])
            self.assertEqual(fields['title'], '验证码48271')
            path.write_text('<!DOCTYPE x><msg/>', encoding='utf-8')
            with self.assertRaises(ValueError):
                prepare(attachment)

    def test_binary_file_is_never_executed(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'resource'
            path.write_bytes(b'MZ\0\0binary')
            with self.assertRaises(ValueError):
                prepare({'path': str(path), 'name': 'sample.exe', 'message_type': 'file'})
