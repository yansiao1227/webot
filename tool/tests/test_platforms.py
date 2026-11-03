import json
import subprocess
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from tool.content_reading import platforms, platform_worker, web


class PlatformTests(unittest.TestCase):
    def test_short_link_stops_before_video_page_or_body_is_requested(self):
        final = 'https://www.bilibili.com/video/BV1234567890/?share_source=WEIXIN'
        response = Mock(status=302)
        response.getheader.return_value = final
        connection = Mock()
        connection.getresponse.return_value = response
        with patch.object(web, 'resolve_public', return_value='93.184.216.34'), \
                patch.object(web.socket, 'create_connection'), \
                patch.object(web.ssl, 'create_default_context'), \
                patch.object(web.http.client, 'HTTPConnection', return_value=connection) as connect:
            self.assertEqual(platforms.resolve_bilibili('https://b23.tv/sample'), final)
        # 即使视频网页会返回412，也完全不会对它发起HTTP请求。
        connect.assert_called_once()
        self.assertEqual(connection.request.call_args.args[:2], ('GET', '/sample'))
        response.read1.assert_not_called()
        connection.close.assert_called_once()

    def test_short_link_follows_multiple_hops_without_reading_bodies(self):
        responses = [Mock(status=302), Mock(status=307)]
        responses[0].getheader.return_value = '/next'
        responses[1].getheader.return_value = 'https://www.bilibili.com/video/BV1234567890'
        connection = Mock()
        connection.getresponse.side_effect = responses
        with patch.object(web, 'resolve_public', return_value='93.184.216.34'), \
                patch.object(web.socket, 'create_connection'), \
                patch.object(web.ssl, 'create_default_context'), \
                patch.object(web.http.client, 'HTTPConnection', return_value=connection):
            self.assertEqual(platforms.resolve_bilibili('https://b23.tv/sample'),
                'https://www.bilibili.com/video/BV1234567890')
        self.assertEqual(connection.request.call_count, 2)
        for response in responses:
            response.read1.assert_not_called()

    def test_short_link_rejects_other_platform_private_target_and_other_page(self):
        for final in ['https://evil.example/video/BV1234567890',
                'http://127.0.0.1/private',
                'https://www.bilibili.com/video/BV1234567890?p=2']:
            response = Mock(status=302)
            response.getheader.return_value = final
            connection = Mock()
            connection.getresponse.return_value = response
            def public(value):
                if '127.0.0.1' in value:
                    raise ValueError('非公网目标')
                return '93.184.216.34'
            with self.subTest(final=final), patch.object(web, 'resolve_public', side_effect=public), \
                    patch.object(web.socket, 'create_connection'), \
                    patch.object(web.ssl, 'create_default_context'), \
                    patch.object(web.http.client, 'HTTPConnection', return_value=connection) as connect:
                with self.assertRaises(ValueError):
                    platforms.resolve_bilibili('https://b23.tv/sample')
                connect.assert_called_once()

    def test_short_link_redirect_limit_is_retained(self):
        response = Mock(status=302)
        response.getheader.return_value = '/loop'
        connection = Mock()
        connection.getresponse.return_value = response
        with patch.object(web, 'resolve_public', return_value='93.184.216.34'), \
                patch.object(web.socket, 'create_connection'), \
                patch.object(web.ssl, 'create_default_context'), \
                patch.object(web.http.client, 'HTTPConnection', return_value=connection):
            with self.assertRaisesRegex(ValueError, '重定向次数超限'):
                platforms.resolve_bilibili('https://b23.tv/sample')
            self.assertEqual(connection.request.call_count, 6)

    def test_domain_boundary_and_public_urls(self):
        for url, expected in [
                ('https://www.bilibili.com/video/BV1234567890', 'bilibili'),
                ('https://b23.tv/abc', 'bilibili'),
                ('https://www.xiaohongshu.com/explore/a', 'xiaohongshu'),
                ('https://xhslink.com/a/abc', 'xiaohongshu'),
                ('https://bilibili.com.evil.example/a', None),
                ('https://evilbilibili.com/a', None),
                ('https://bilibili.com@evil.example/a', None),
                ('http://127.0.0.1/a', None),
                ('https://bilibili.com:8888/a', None)]:
            with self.subTest(url=url):
                self.assertEqual(platforms.platform(url), expected)

    def test_ids_are_from_exact_source_and_page_is_preserved(self):
        self.assertEqual(platforms.target('https://www.bilibili.com/video/BV1234567890',
            'bilibili'), {'bvid': 'BV1234567890'})
        url = 'https://www.xiaohongshu.com/discovery/item/' + 'a' * 24 + '?xsec_token=sample%2Bvalue'
        self.assertEqual(platforms.target(url, 'xiaohongshu')['xsec_token'], 'sample+value')
        for url, provider in [('https://b23.tv/abc', 'bilibili'),
                ('https://www.bilibili.com/video/BV1234567890?p=2', 'bilibili'),
                ('https://evil.example/video/BV1234567890', 'bilibili'),
                ('https://xiaohongshu.com/explore/' + 'a' * 24, 'xiaohongshu')]:
            with self.subTest(url=url), self.assertRaises(ValueError):
                platforms.target(url, provider)

    def test_worker_timeout_and_errors_do_not_leak(self):
        for effect in [OSError('private path'), subprocess.TimeoutExpired('secret', 50)]:
            with patch.object(platforms.subprocess, 'run', side_effect=effect):
                with self.assertRaises(ValueError) as caught:
                    platforms.read('https://b23.tv/abc')
                self.assertNotIn('secret', str(caught.exception))
        with patch.object(platforms.subprocess, 'run', return_value=SimpleNamespace(
                returncode=0, stdout=json.dumps({'state': 'read'}))) as run:
            self.assertEqual(platforms.read('https://b23.tv/abc')['state'], 'read')
            self.assertNotIn('shell', run.call_args.kwargs)
            self.assertEqual(run.call_args.kwargs['timeout'], 50)


class MCPResultTests(unittest.IsolatedAsyncioTestCase):
    async def test_error_or_non_json_is_not_source_content(self):
        for text, failed in [('login required', False), ('{"error":"private token"}', False), ('{}', True)]:
            session = SimpleNamespace(call_tool=AsyncMock(return_value=SimpleNamespace(
                isError=failed, content=[SimpleNamespace(type='text', text=text)])))
            with self.assertRaises(ValueError) as caught:
                await platform_worker.call(session, 'bili_subtitle', {'bvid': 'BV1234567890'})
            self.assertNotIn('private token', str(caught.exception))
