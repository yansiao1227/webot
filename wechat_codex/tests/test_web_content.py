import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch, Mock

from local_agent import media, web_content
from tool.content_reading import media as content_media, web as content_web
from local_agent.cards import extract


class WebContentTests(unittest.TestCase):
    def test_doh_fallback_and_short_cache(self):
        failed = Mock()
        failed.request.side_effect = TimeoutError()
        success = Mock()
        response = success.getresponse.return_value
        response.status = 200
        response.read.return_value = b'{"Answer":[{"type":1,"data":"93.184.216.34"}]}'
        web_content._dns_cache.clear()
        try:
            with patch.object(content_web.http.client, 'HTTPSConnection', side_effect=[failed, success]) as connection:
                self.assertEqual(web_content._public_dns('dns-test.example'), '93.184.216.34')
                self.assertEqual(web_content._public_dns('dns-test.example'), '93.184.216.34')
                self.assertEqual(connection.call_count, 2)
            failed.close.assert_called_once()
            success.close.assert_called_once()
        finally:
            web_content._dns_cache.clear()

    def test_doh_never_accepts_private_answer(self):
        connection = Mock()
        response = connection.getresponse.return_value
        response.status = 200
        response.read.return_value = b'{"Answer":[{"type":1,"data":"127.0.0.1"}]}'
        with patch.object(content_web.http.client, 'HTTPSConnection', return_value=connection):
            with self.assertRaises(ValueError):
                web_content._public_dns('private-answer.example')

    def test_fake_ip_requires_separate_public_dns_verification(self):
        addresses = [(2, 1, 6, '', ('198.18.0.107', 443))]
        with patch.object(content_web.socket, 'getaddrinfo', return_value=addresses), \
                patch.object(content_web, '_public_dns', return_value='93.184.216.34') as verify:
            self.assertEqual(web_content.resolve_public('https://example.com/'), '93.184.216.34')
            verify.assert_called_once_with('example.com')
        with patch.object(content_web.socket, 'getaddrinfo', return_value=addresses), \
                patch.object(content_web, '_public_dns', side_effect=ValueError('denied')):
            with self.assertRaises(ValueError):
                web_content.resolve_public('https://example.com/')

    def test_private_dns_and_redirect_target_are_rejected(self):
        addresses = [(2, 1, 6, '', ('127.0.0.1', 443))]
        with patch.object(content_web.socket, 'getaddrinfo', return_value=addresses):
            with self.assertRaises(ValueError):
                web_content.resolve_public('https://example.com/')
        connection = Mock()
        response = Mock(status=302)
        response.getheader.return_value = 'http://127.0.0.1/secret'
        connection.getresponse.return_value = response
        with patch.object(content_web, 'resolve_public', side_effect=['93.184.216.34', ValueError('blocked')]), \
                patch.object(content_web.socket, 'create_connection'), \
                patch.object(content_web.http.client, 'HTTPConnection', return_value=connection):
            with self.assertRaises(ValueError):
                web_content.fetch('http://example.com/')
        self.assertEqual(connection.request.call_count, 1)
        connection.close.assert_called_once()

    def test_html_script_is_excluded(self):
        parser = web_content._Text()
        parser.feed('<article>正文<script>secret()</script><style>hidden</style><p>下一段&amp;引用</p></article>')
        self.assertEqual(parser.parts, ['正文', '下一段&引用'])

    def test_app_message_uses_profile_and_regular_link_uses_fetch(self):
        card = extract('<msg><appmsg><url>https://example.com/</url></appmsg></msg>')
        value = {'text': '实际正文', 'method': 'playwright'}
        with patch.object(content_web, 'browser', return_value=value) as browser, \
                patch.object(content_web, 'fetch', return_value=value) as fetch:
            result = web_content.retrieve(card, {'profile_dir': 'profile'})
            self.assertEqual(result[0]['state'], 'read')
            browser.assert_called_once_with('https://example.com/',
                {'profile_dir': 'profile', 'browser': True, 'public_only': True})
            fetch.assert_not_called()
            web_content.retrieve({'source_urls': ['https://example.com/']}, {'profile_dir': 'profile'})
            fetch.assert_called_once()
            self.assertTrue(fetch.call_args.kwargs['public_only'])

    def test_failure_retains_metadata_without_claiming_read(self):
        with patch.object(content_web, 'browser', side_effect=RuntimeError('secret cookie')):
            result = web_content.retrieve({'card_kind': 'app-message', 'source_urls': ['https://example.com/']},
                                          {'profile_dir': 'profile'})
        self.assertEqual(result[0]['state'], 'unavailable')
        self.assertNotIn('secret cookie', str(result))

    def test_metadata_only_does_not_connect_and_source_read_is_explicit(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'card.xml'
            path.write_text('<msg><appmsg><title>标题</title><url>https://example.com/</url></appmsg></msg>', encoding='utf-8')
            attachment = {'path': str(path), 'message_type': 'card'}
            with patch.object(web_content, 'retrieve', return_value=[{'text': '正文'}]) as read:
                media.prepare(attachment)
                media.prepare(attachment, {'read_source': False})
                read.assert_not_called()
                prepared = media.prepare(attachment, {'read_source': True})['prepared']
                self.assertEqual(prepared['source_contents'][0]['text'], '正文')

    def test_video_keeps_visuals_when_audio_fails_and_sends_jpeg(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory)
            frame = output / 'video-01.jpg'
            frame.write_bytes(b'jpeg-bytes')
            probe = Mock(stderr=b'Duration: 00:00:12.00 Audio: aac')
            with patch.object(content_media, '_ffmpeg', return_value='ffmpeg'), \
                    patch.object(media.subprocess, 'run', return_value=probe), patch.object(content_media, '_run'), \
                    patch.object(content_media, '_audio', side_effect=ValueError('无音轨')):
                prepared = media._video(output / 'clip.mp4', output)
            self.assertEqual(prepared['duration_seconds'], 12)
            self.assertEqual(prepared['visual_bytes'], 10)
            self.assertIn('audio_error', prepared)
            content = media.external_content('external', {'prepared': prepared})
            self.assertTrue(content[1]['image_url'].startswith('data:image/jpeg;base64,'))
