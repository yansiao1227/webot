"""朋友圈缓存原生合约回归；仅内存 SQLite，不读取本机微信或附加进程。"""
from pathlib import Path
import sqlite3
import sys
import unittest
from unittest.mock import Mock, patch
from xml.sax.saxutils import escape

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from api.backend import NativeBackend
from api.errors import ApiError
from api.native_sns import parse_item, read_timeline


def xml_item(identifier, author='wxid_peer', text='测试正文'):
    return ('<SnsDataItem><TimelineObject><id>' + str(identifier) + '</id><username>' +
            escape(author) + '</username><createTime>123</createTime><contentDesc>' +
            escape(text) + '</contentDesc><ContentObject><type>1</type></ContentObject>' +
            '</TimelineObject><LocalExtraInfo><like_flag>1</like_flag></LocalExtraInfo></SnsDataItem>')


def signed(identifier):
    return identifier if identifier < 1 << 63 else identifier - (1 << 64)


class Snapshot:
    def __init__(self, rows):
        self.db = sqlite3.connect(':memory:')
        self.db.execute('CREATE TABLE SnsTimeLine(tid INTEGER PRIMARY KEY,user_name TEXT,content TEXT)')
        self.db.executemany('INSERT INTO SnsTimeLine VALUES(?,?,?)', rows)
        self.closed = False

    def execute(self, *args):
        return self.db.execute(*args)

    def close(self):
        self.closed = True
        self.db.close()


class NativeSnsTests(unittest.TestCase):
    def test_uint64_pagination_crosses_signed_boundary_without_duplicates(self):
        identifiers = [2**64 - 1, 2**64 - 2, 2**63, 2**63 - 1, 2, 1]
        rows = [(signed(value), 'wxid_peer', xml_item(value)) for value in identifiers]
        snapshots = []

        def snapshot(path, account):
            self.assertEqual(path, Path('fixture-account/db_storage/sns/sns.db'))
            self.assertEqual(account, 'wxid_self')
            result = Snapshot(rows)
            snapshots.append(result)
            return result

        with patch('api.native_assets.account_directory', return_value=Path('fixture-account')) as directory, \
                patch('api.native_media.media_snapshot', side_effect=snapshot):
            cursor, found = '0', []
            for _ in identifiers:
                result = read_timeline('wxid_self', {'max_id': cursor, 'limit': 1})
                self.assertEqual(result['source'], 'local_cache')
                self.assertFalse(result['network_refreshed'])
                self.assertFalse(result['server_confirmed'])
                self.assertEqual(len(result['items']), 1)
                found.append(int(result['items'][0]['post_id']))
                if result['has_more']:
                    self.assertEqual(result['next_max_id'], result['items'][-1]['post_id'])
                    cursor = result['next_max_id']
                else:
                    self.assertIsNone(result['next_max_id'])
            self.assertEqual(found, identifiers)
            self.assertEqual(directory.call_count, len(identifiers))
        self.assertTrue(all(item.closed for item in snapshots))

    def test_author_filter_is_exact_and_snapshot_is_account_bound(self):
        rows = [(2, 'wxid_peer', xml_item(2)), (1, 'wxid_other', xml_item(1, 'wxid_other'))]
        snapshot = Snapshot(rows)
        with patch('api.native_assets.account_directory', return_value=Path('account-root')) as directory, \
                patch('api.native_media.media_snapshot', return_value=snapshot) as acquire:
            result = read_timeline('wxid_self', {'author_id': 'wxid_peer', 'limit': 100})
        directory.assert_called_once_with('wxid_self')
        acquire.assert_called_once_with(Path('account-root/db_storage/sns/sns.db'), 'wxid_self')
        self.assertEqual([item['post_id'] for item in result['items']], ['2'])
        self.assertEqual(result['items'][0]['author_id'], 'wxid_peer')
        self.assertEqual(result['items'][0]['text'], '测试正文')
        self.assertEqual(result['items'][0]['local_like_flag'], '1')
        self.assertFalse(result['has_more'])
        self.assertTrue(snapshot.closed)

    def test_invalid_limit_cursor_or_author_never_opens_snapshot(self):
        invalid = [{'limit': value} for value in (0, 101, True, False, 1.5, '20', None)]
        invalid += [{'max_id': value} for value in (0, True, '01', '-1', str(2**64), '')]
        invalid += [{'author_id': value} for value in ('peer', "wxid_peer' OR 1=1--", 1)]
        with patch('api.native_media.media_snapshot') as snapshot:
            for params in invalid:
                with self.subTest(params=params), self.assertRaises(ApiError) as caught:
                    read_timeline('wxid_self', params)
                self.assertEqual(caught.exception.status, 400)
        snapshot.assert_not_called()

    def test_xml_identity_structure_and_declarations_are_rejected(self):
        invalid = [xml_item(2), xml_item(1, 'wxid_other'),
                   '<TimelineObject><id>1</id></TimelineObject>', '<broken',
                   '<!DOCTYPE SnsDataItem [<!ENTITY x "value">]>' + xml_item(1),
                   '<!ENTITY external SYSTEM "file:///unread">' + xml_item(1),
                   xml_item(1).replace('<createTime>123</createTime>', '<createTime>-1</createTime>')]
        for xml in invalid:
            with self.subTest(xml=xml[:70]), self.assertRaises(ApiError) as caught:
                parse_item(1, 'wxid_peer', xml)
            self.assertEqual(caught.exception.code, 'sns_cache_item_invalid')
            self.assertEqual(caught.exception.status, 503)

    def test_xml_failure_closes_snapshot(self):
        snapshot = Snapshot([(1, 'wxid_peer', xml_item(1, 'wxid_other'))])
        with patch('api.native_assets.account_directory', return_value=Path('account-root')), \
                patch('api.native_media.media_snapshot', return_value=snapshot), self.assertRaises(ApiError):
            read_timeline('wxid_self', {'limit': 1})
        self.assertTrue(snapshot.closed)

    def test_native_backend_checks_account_before_and_after_read(self):
        backend = NativeBackend()
        backend.profile = Mock(return_value={'wxid': 'wxid_self'})
        with patch('api.native_sns.read_timeline', return_value={'source': 'local_cache'}) as read:
            with self.assertRaises(ApiError) as caught:
                backend.sns_read('wxid_other', {'limit': 1})
            self.assertEqual(caught.exception.code, 'account_changed')
            read.assert_not_called()
            backend.profile.side_effect = [{'wxid': 'wxid_self'}, {'wxid': 'wxid_other'}]
            with self.assertRaises(ApiError) as caught:
                backend.sns_read('wxid_self', {'limit': 1})
            self.assertEqual(caught.exception.code, 'account_changed')
            read.assert_called_once_with('wxid_self', {'limit': 1})

    def test_fixture_target_cannot_claim_native_timeline_or_write(self):
        backend = NativeBackend(fixture_pid=123)
        backend.profile = Mock(return_value={'wxid': 'wxid_self'})
        self.assertFalse(any(backend.sns_capabilities().values()))
        with patch('api.native_sns.read_timeline') as read, self.assertRaises(ApiError):
            backend.sns_read('wxid_self', {})
        read.assert_not_called()
        with self.assertRaises(ApiError) as caught:
            backend.sns_execute('wxid_self', 'like', {}, 'request')
        self.assertEqual(caught.exception.code, 'sns_native_unavailable')


if __name__ == '__main__':
    unittest.main()
