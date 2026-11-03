from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from tool.content_reading import video
from tool.content_reading.api import invoke


class VideoLinkTests(unittest.TestCase):
    def test_direct_link_uses_shared_processor_and_preserves_model_override(self):
        with patch.object(video, 'prepare', return_value={'state': 'read', 'images': ['frame.jpg']}) as prepare:
            result = video.read_link('https://video.example/short', subtitle_url='https://video.example/a.srt',
                                     output_dir='parent', model_dir='model')
            self.assertEqual(result['state'], 'read')
            self.assertEqual(prepare.call_args.args[0], [{'url': 'https://video.example/short'}])
            self.assertEqual(prepare.call_args.kwargs['subtitles'], [{'url': 'https://video.example/a.srt'}])
            self.assertEqual(prepare.call_args.kwargs['model_dir'], 'model')



    def test_private_source_is_rejected_before_download(self):
        with patch.object(video, 'download') as download:
            result = invoke('read-video-link', url='http://127.0.0.1/video.mp4')
            self.assertEqual(result['state'], 'unavailable')
            download.assert_not_called()

    def test_generic_nested_frames_reach_visual_input(self):
        with tempfile.TemporaryDirectory() as directory:
            frame = Path(directory) / 'frame.jpg'
            frame.write_bytes(b'jpeg')
            content = video.media.external_content('external', {'prepared': {'source_contents': [
                {'video_content': {'state': 'read', 'method': 'local_video', 'images': [str(frame)]}}]}})
            self.assertEqual(content[1]['type'], 'input_image')

    def test_direct_mp4_does_not_require_platform_duration(self):
        def download(url, path, **options):
            Path(path).write_bytes(b'\x00\x00\x00\x18ftypisom')
            return {'bytes': 12}
        with tempfile.TemporaryDirectory() as directory, patch.object(video, 'download', side_effect=download), \
                patch.object(video.media, 'prepare_content', return_value={'images': ['frame.jpg'], 'duration_seconds': 10}):
            result = invoke('read-video-link', url='https://video.example/short', output_dir=directory)
            self.assertEqual(result['state'], 'read')
            self.assertEqual(result['result']['duration_seconds'], 10)
            self.assertEqual(result['result']['method'], 'local_video')
