import json
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

from tool.content_reading.api import invoke
from tool.content_reading import web, media


class Page(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path in ('/image', '/fake-image', '/large-image'):
            from PIL import Image
            from io import BytesIO
            data = BytesIO()
            Image.new('RGB', (120, 80), 'red').save(data, format='PNG')
            body = data.getvalue() if self.path == '/image' else (
                b'x' * (20 * 1024 * 1024 + 1) if self.path == '/large-image' else b'not an image')
            self.send_response(200)
            self.send_header('Content-Type', 'image/png')
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if self.path == '/image-redirect':
            self.send_response(302)
            self.send_header('Location', '/image')
            self.end_headers()
            return
        if self.path == '/redirect':
            self.send_response(302)
            self.send_header('Location', '/')
            self.end_headers()
            return
        body = '<article>本机内容48271<script>不可见内容</script></article>'.encode('utf-8')
        self.send_response(200)
        self.send_header('Content-Type', 'text/html; charset=utf-8')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


class ContentToolsTests(unittest.TestCase):
    def test_image_download_redirect_and_visual_paths(self):
        with tempfile.TemporaryDirectory() as directory:
            for endpoint in ('/image', '/image-redirect'):
                value = invoke('read-image-content', url=self.url + endpoint, output_dir=directory)
                self.assertEqual(value['state'], 'read', value)
                self.assertEqual(value['result']['dimensions'], [120, 80])
                self.assertTrue(Path(value['result']['images'][0]).is_file())
                self.assertEqual(value['result']['final_url'], self.url + '/image')
            value = web.read_sources([self.url + '/image'])
            self.assertEqual(value[0]['state'], 'read', value)
            self.assertTrue(value[0]['images'])

    def test_image_rejects_html_corrupt_data_and_private_targets(self):
        for endpoint in ('/', '/fake-image', '/large-image'):
            with tempfile.TemporaryDirectory() as directory:
                value = invoke('read-image-content', url=self.url + endpoint, output_dir=directory)
                self.assertEqual(value['state'], 'unavailable', value)
                self.assertEqual(list(Path(directory).iterdir()), [])
        value = invoke('read-image-content', url=self.url + '/image', public_only=True)
        self.assertEqual(value['state'], 'unavailable')

    def test_html_extracts_lazy_images_without_hidden_script_images(self):
        parser = web._Text()
        parser.feed('<img data-src="/photo.png" src="placeholder"><script><img src="hidden"></script>')
        self.assertEqual(parser.images, ['/photo.png'])

    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(('127.0.0.1', 0), Page)
        cls.worker = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.worker.start()
        cls.url = 'http://127.0.0.1:' + str(cls.server.server_port)

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.worker.join(timeout=5)

    def test_default_localhost_read_and_relative_redirect(self):
        for url in (self.url, self.url + '/redirect'):
            value = invoke('read-web-content', url=url)
            self.assertEqual(value['state'], 'read', value)
            self.assertEqual(value['result']['text'], '本机内容48271')
            self.assertFalse(value['result']['truncated'])

    def test_lan_is_resolved_normally_without_public_dns_policy(self):
        original = web.socket.create_connection
        def connect(address, **kwargs):
            self.assertEqual(address, ('192.168.1.20', self.server.server_port))
            return original(('127.0.0.1', self.server.server_port), **kwargs)
        with patch.object(web.socket, 'create_connection', side_effect=connect), \
                patch.object(web, 'resolve_public', side_effect=AssertionError('must not apply')):
            value = invoke('read-web-content', url=f'http://192.168.1.20:{self.server.server_port}/')
        self.assertEqual(value['state'], 'read', value)

    def test_explicit_public_policy_rejects_loopback(self):
        with self.assertRaises(ValueError):
            web.fetch('http://127.0.0.1/', public_only=True)
        value = invoke('read-web-content', url='http://127.0.0.1/', public_only=True)
        self.assertEqual(value['state'], 'unavailable')

    def test_read_local_file_and_cli_outside_repository(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / '本机文档.txt'
            path.write_text('本机文件76543', encoding='utf-8')
            output = Path(directory) / 'derived'
            value = invoke('read-document', path=path, output_dir=output)
            self.assertEqual(value['result']['text'], '本机文件76543')
            entry = Path(__file__).resolve().parents[1] / 'read_content.py'
            result = subprocess.run([sys.executable, '-B', '-X', 'utf8', str(entry), 'read-document',
                '--path', str(path), '--output-dir', str(output)], cwd=directory,
                capture_output=True, encoding='utf-8', timeout=20)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(json.loads(result.stdout)['result']['text'], '本机文件76543')

    def test_real_image_and_video_sampling(self):
        from PIL import Image
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            image = root / 'sample.png'
            Image.new('RGB', (120, 80), 'red').save(image)
            value = invoke('inspect-media', path=image, kind='image', output_dir=root / 'images')
            self.assertEqual(value['result']['dimensions'], [120, 80])
            self.assertTrue(Path(value['result']['images'][0]).is_file())
            video = root / 'clip.mp4'
            media._run([media._ffmpeg(), '-nostdin', '-y', '-v', 'error', '-loop', '1',
                        '-i', str(image), '-t', '1', '-pix_fmt', 'yuv420p', str(video)])
            value = invoke('inspect-media', path=video, kind='video', output_dir=root / 'video')
            self.assertEqual(value['state'], 'read', value)
            self.assertGreater(len(value['result']['images']), 0)
            self.assertGreater(value['result']['visual_bytes'], 0)

    def test_model_path_is_explicit_and_never_auto_downloaded(self):
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(ValueError, '模型尚未安装'):
                media.transcribe('unused.wav', model_dir=directory)

    def test_browser_profile_and_missing_video_evidence(self):
        with patch.dict(os.environ, {'CONTENT_BROWSER_PROFILE_DIR': ''}):
            value = invoke('read-video-content', url='https://www.bilibili.com/video/BVtest/')
        self.assertEqual(value['state'], 'unavailable')
        self.assertIn('profile_dir', value['reason'])
        page = {'state': 'read', 'text': '只有标题', 'content_scope': '页面文本'}
        with patch.object(web, 'read_sources', return_value=[page]):
            value = invoke('read-video-content', url='https://example.com/', profile_dir='profile')
        self.assertEqual(value['state'], 'unavailable')
        self.assertEqual(value['result']['text'], '只有标题')

    def test_web_failures_do_not_disclose_browser_secrets(self):
        with patch.object(web, 'browser', side_effect=RuntimeError('cookie=secret')):
            value = invoke('read-web-content', url='https://example.com/', browser=True, profile_dir='profile')
        self.assertEqual(value['state'], 'unavailable')
        self.assertNotIn('cookie=secret', json.dumps(value))

    def test_unsupported_binary_is_not_read_as_text(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'sample.exe'
            path.write_bytes(b'MZ\0binary')
            value = invoke('read-document', path=path, output_dir=Path(directory) / 'out')
        self.assertEqual(value['state'], 'unavailable')


if __name__ == '__main__':
    unittest.main()
