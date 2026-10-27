import hashlib
from pathlib import Path
import sys
import unittest
from unittest.mock import patch, MagicMock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from api.cdn import sticker_bytes
from api.errors import ApiError


class CdnTests(unittest.TestCase):
    def test_only_native_sticker_endpoint_is_fetchable(self):
        for url in ('http://127.0.0.1/110/20401/stodownload',
                    'file:///test', 'https://vweixinf.tc.qq.com/private',
                    'http://vweixinf.tc.qq.com:30001/110/20401/stodownload',
                    'http://user@vweixinf.tc.qq.com/110/20401/stodownload'):
            with self.assertRaises(ApiError):
                sticker_bytes(url, '0' * 32)

    def test_content_hash_and_length_are_enforced(self):
        conn = MagicMock()
        response = conn.getresponse.return_value
        response.status = 200
        response.getheader.return_value = '5'
        with patch('api.cdn.http.client.HTTPConnection', return_value=conn):
            response.read.side_effect = [b'hello', b'']
            self.assertEqual(sticker_bytes('http://vweixinf.tc.qq.com/110/20401/stodownload',
                                          hashlib.md5(b'hello').hexdigest()), b'hello')
            response.read.side_effect = [b'wrong', b'']
            with self.assertRaises(ApiError):
                sticker_bytes('http://vweixinf.tc.qq.com/110/20401/stodownload',
                              hashlib.md5(b'hello').hexdigest())
            response.getheader.return_value = str(64 * 1024 * 1024 + 1)
            with self.assertRaises(ApiError):
                sticker_bytes('http://vweixinf.tc.qq.com/110/20401/stodownload', '0' * 32)

    def test_redirect_is_never_followed(self):
        conn = MagicMock()
        conn.getresponse.return_value.status = 302
        with patch('api.cdn.http.client.HTTPSConnection', return_value=conn):
            with self.assertRaises(ApiError):
                sticker_bytes('https://vweixinf.tc.qq.com/110/20401/stodownload', '0' * 32)
        conn.close.assert_called_once()
