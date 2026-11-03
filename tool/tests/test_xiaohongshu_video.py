from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from tool.content_reading import xiaohongshu, download as downloader, media, platforms, video as video_tool


SRT = '1\n00:00:00,100 --> 00:00:01,500\n第一句话\n\n2\n00:00:02,000 --> 00:00:03,000\n第二句话\n'


def video():
    return {'capa': {'duration': 15}, 'media': {'stream': {'h264': [
        {'format': 'mp4', 'masterUrl': 'https://video.example/signed.mp4', 'size': 1000}]}}}


class VideoTests(unittest.TestCase):
    def test_platform_layer_passes_only_media_url_to_video_tool(self):
        with patch.object(video_tool, 'read_link', return_value={
                'state': 'read', 'method': 'local_video', 'images': ['frame.jpg']}) as read:
            result = xiaohongshu.prepare(video(), output_dir='out', model_dir='model')
            read.assert_called_once_with('https://video.example/signed.mp4', output_dir='out', model_dir='model')
            self.assertEqual(result['tool'], 'read-video-link')
            self.assertEqual(result['method'], 'xiaohongshu_local_video')

    def test_srt_timestamps_and_invalid_response(self):
        result = xiaohongshu.parse_subtitles(SRT, 15)
        self.assertEqual(result['segment_count'], 2)
        self.assertEqual(result['time_range_seconds'], [.1, 3.0])
        self.assertIn('第二句话', result['text'])
        self.assertNotIn('\n2\n', result['text'])
        for text in ['<html>login required</html>', SRT.replace('00:00:03,000', '00:20:03,000')]:
            with self.subTest(text=text), self.assertRaises(ValueError):
                xiaohongshu.parse_subtitles(text, 15)

    def test_subtitles_precede_download_and_local_decoding(self):
        resource = video()
        resource['subtitles'] = {'zh-CN': [{'url': 'https://video.example/subtitle.srt'}]}
        def subtitle(url, path, **options):
            Path(path).write_text(SRT, encoding='utf-8')
            return {'bytes': len(SRT.encode())}
        with tempfile.TemporaryDirectory() as directory, \
                patch.object(video_tool, 'download', side_effect=subtitle) as fetch, \
                patch.object(media, 'prepare_content') as prepare, \
                patch.object(video_tool, 'read_link') as read:
            result = xiaohongshu.prepare(resource, directory)
            self.assertEqual(result['method'], 'xiaohongshu_subtitles')
            self.assertEqual(result['state'], 'read')
            self.assertEqual(fetch.call_count, 1)
            prepare.assert_not_called()
            read.assert_not_called()

    def test_missing_or_failed_subtitles_use_download_and_keep_frames_when_audio_fails(self):
        for subtitles in [None, {'zh': [{'url': 'https://video.example/expired.srt'}]}]:
            resource = video()
            resource['subtitles'] = subtitles
            def fetch(url, path, **options):
                if url.endswith('.srt'):
                    raise ValueError('资源返回HTTP 403')
                Path(path).write_bytes(b'\x00\x00\x00\x18ftypisom' + b'media')
                return {'bytes': 17}
            with self.subTest(subtitles=subtitles), tempfile.TemporaryDirectory() as directory, \
                    patch.object(video_tool, 'download', side_effect=fetch), \
                    patch.object(media, 'prepare_content', return_value={
                        'images': ['sample.jpg'], 'audio_error': '本地语音转写模型尚未安装',
                        'duration_seconds': 15}) as prepare:
                result = xiaohongshu.prepare(resource, directory)
                self.assertEqual(result['state'], 'read')
                self.assertEqual(result['method'], 'xiaohongshu_local_video')
                self.assertEqual(result['images'], ['sample.jpg'])
                self.assertIn('audio_error', result)
                self.assertEqual(result['subtitle_attempt']['state'], 'unavailable')
                prepare.assert_called_once()
                self.assertTrue(Path(prepare.call_args.args[0]).is_relative_to(Path(directory)))

    def test_oversized_long_and_non_mp4_resources_do_not_decode(self):
        resources = [video(), video(), video()]
        resources[0]['capa']['duration'] = 601
        resources[1]['media']['stream']['h264'][0]['size'] = 65 * 1024 * 1024
        def bad_download(url, path, **options):
            Path(path).write_bytes(b'#EXTM3U\nhttp://127.0.0.1/private')
            return {'bytes': 40}
        with patch.object(media, 'prepare_content') as decode:
            for resource in resources:
                with self.subTest(resource=resource), tempfile.TemporaryDirectory() as directory, \
                        patch.object(video_tool, 'download', side_effect=bad_download):
                    self.assertEqual(xiaohongshu.prepare(resource, directory)['state'], 'unavailable')
            decode.assert_not_called()

    def test_empty_transcription_is_explicit_and_visuals_remain_usable(self):
        def fetch(url, path, **options):
            Path(path).write_bytes(b'\x00\x00\x00\x18ftypisom')
            return {'bytes': 12}
        with tempfile.TemporaryDirectory() as directory, \
                patch.object(video_tool, 'download', side_effect=fetch), \
                patch.object(media, 'prepare_content', return_value={
                    'images': ['sample.jpg'], 'transcript': '', 'transcription_segments': []}):
            result = xiaohongshu.prepare(video(), directory)
            self.assertEqual(result['state'], 'read')
            self.assertEqual(result['transcription_state'], 'empty')
            self.assertIn('未识别', result['transcription_reason'])

    def test_platform_processing_preserves_note_on_video_failure(self):
        import json
        from types import SimpleNamespace
        payload = {'state': 'read', 'method': 'xiaohongshu-mcp', 'text': '文案和评论',
                   'video_resources': video()}
        with patch.object(platforms.subprocess, 'run', return_value=SimpleNamespace(
                returncode=0, stdout=json.dumps(payload))), patch.object(xiaohongshu, 'prepare',
                return_value={'state': 'unavailable'}) as prepare:
            result = platforms.read('https://xiaohongshu.com/explore/' + 'a'*24, output_dir='parent')
            self.assertEqual(result['text'], '文案和评论')
            self.assertEqual(result['video_content']['state'], 'unavailable')
            self.assertNotIn('video_resources', result)
            self.assertEqual(prepare.call_args.kwargs['output_dir'], 'parent')

    def test_nested_video_frames_are_delivered_as_visual_input(self):
        with tempfile.TemporaryDirectory() as directory:
            frame = Path(directory) / 'video-01.jpg'
            frame.write_bytes(b'jpeg')
            attachment = {'prepared': {'source_contents': [{'video_content': {
                'state': 'read', 'method': 'xiaohongshu_local_video', 'images': [str(frame)]}}]}}
            result = media.external_content('external data', attachment)
            self.assertEqual(result[1]['type'], 'input_image')
            self.assertTrue(result[1]['image_url'].startswith('data:image/jpeg;base64,'))


class DownloadTests(unittest.TestCase):
    def test_private_redirect_is_blocked_before_second_request(self):
        response = Mock(status=302)
        response.getheader.return_value = 'http://127.0.0.1/private'
        connection = Mock()
        connection.getresponse.return_value = response
        with tempfile.TemporaryDirectory() as directory, \
                patch.object(downloader.web, 'resolve_public', side_effect=['93.184.216.34', ValueError('private')]), \
                patch.object(downloader.socket, 'create_connection'), \
                patch.object(downloader.ssl, 'create_default_context'), \
                patch.object(downloader.http.client, 'HTTPConnection', return_value=connection) as connect:
            with self.assertRaises(ValueError):
                downloader.download('https://video.example/redirect', Path(directory)/'video.mp4', max_bytes=100)
            connect.assert_called_once()
            response.read1.assert_not_called()

    def test_stream_size_limit_removes_partial_file(self):
        response = Mock(status=200)
        response.getheader.return_value = None
        response.read1.return_value = b'x' * 11
        connection = Mock()
        connection.getresponse.return_value = response
        with tempfile.TemporaryDirectory() as directory, \
                patch.object(downloader.web, 'resolve_public', return_value='93.184.216.34'), \
                patch.object(downloader.socket, 'create_connection'), \
                patch.object(downloader.ssl, 'create_default_context'), \
                patch.object(downloader.http.client, 'HTTPConnection', return_value=connection):
            path = Path(directory)/'video.mp4'
            with self.assertRaisesRegex(ValueError, '下载上限'):
                downloader.download('https://video.example/large', path, max_bytes=10)
            self.assertFalse(path.exists())
            self.assertFalse(path.with_suffix('.mp4.part').exists())
